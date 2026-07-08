#!/usr/bin/env python3
"""
discdb.py - TheDiscDb read-path integration (lookup only).

Given a scanned disc, this module:
  1. Computes TheDiscDb "ContentHash" from the disc's stream-file sizes
     (no decryption, no MakeMKV - just os.stat over the mounted drive),
  2. Queries https://thediscdb.com/graphql/ for a disc with that hash,
  3. Joins the community's per-title season/episode mapping onto our own
     scanned titles (via the intrinsic sourceFile + segmentMap fields),
  4. Returns a normalized match the wizard can pre-fill from.

Everything here is *advisory*: any failure (offline, unknown disc, API change)
returns None and the caller falls back to the normal identify-by-TMDB flow. The
feature only ever adds signal.

Stdlib only. Can also be run standalone to validate the hash port against a real
disc (Phase 0):  py discdb.py E:        or   py discdb.py --hash 289B58...
"""

import hashlib
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENDPOINT = "https://thediscdb.com/graphql/"
DEFAULT_TIMEOUT = 4          # seconds - hobby API, keep it snappy and non-blocking
MISS_TTL = 7 * 24 * 3600     # re-query a "not found" after a week (their db grows)

# The verified query (fields confirmed against a live call in the integration
# report). The where-filter is at mediaItems level, so *all* releases of a
# matching media item come back; we select the release/disc whose contentHash
# equals ours.
_QUERY = """
query($hash: String) {
  mediaItems(where: { releases: { some: { discs: { some: {
      contentHash: { eq: $hash } } } } } }) {
    nodes {
      title year type
      externalids { tmdb imdb }
      releases {
        slug title upc year
        discs(order: { index: ASC }) {
          index name format contentHash
          titles(order: { index: ASC }) {
            sourceFile segmentMap duration size
            item { title season episode type }
          }
        }
      }
    }
  }
}
""".strip()


# ---------------------------------------------------------------------------
# ContentHash - MD5 over 8-byte little-endian stream-file sizes
# ---------------------------------------------------------------------------
def _drive_root(device):
    """Turn a MakeMKV device field ('E:', 'E:\\', 'E') into a 'E:\\' root."""
    m = re.search(r"[A-Za-z]", device or "")
    if not m:
        return None
    return Path(f"{m.group(0)}:\\")


def content_hash(device):
    """TheDiscDb ContentHash for the disc mounted at `device`, or None.

    Blu-ray: MD5 over every BDMV\\STREAM\\*.m2ts file size (sorted by name).
    DVD:     MD5 over every VIDEO_TS\\* file size (sorted by name).
    Only the sizes enter the hash - names/timestamps do not. File sizes live in
    the UDF directory, which neither CSS nor AACS encrypts, so this needs no
    keys and takes milliseconds."""
    root = _drive_root(device)
    if root is None:
        return None
    stream, video_ts = root / "BDMV" / "STREAM", root / "VIDEO_TS"
    try:
        if stream.is_dir():
            files = sorted(stream.glob("*.m2ts"), key=lambda p: p.name)
        elif video_ts.is_dir():
            files = sorted(video_ts.iterdir(), key=lambda p: p.name)
        else:
            return None
        if not files:
            return None
        h = hashlib.md5()
        for f in files:
            h.update(struct.pack("<q", f.stat().st_size))
    except OSError:
        return None
    return h.hexdigest().upper()


# ---------------------------------------------------------------------------
# GraphQL query (+ local cache keyed by hash)
# ---------------------------------------------------------------------------
def _cache_dir(explicit=None):
    if explicit:
        d = Path(explicit)
    else:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") \
            or str(Path.home())
        d = Path(base) / "Disk-Rip" / "discdb-cache"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d


def _cache_read(cdir, disc_hash):
    if not cdir:
        return None
    f = cdir / f"{disc_hash}.json"
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # a positive result is cached forever; a "miss" expires so newly-added
    # discs get picked up on a later scan
    if rec.get("match") is None and (time.time() - rec.get("fetched", 0)) > MISS_TTL:
        return None
    return rec


def _cache_write(cdir, disc_hash, match):
    if not cdir:
        return
    try:
        (cdir / f"{disc_hash}.json").write_text(
            json.dumps({"hash": disc_hash, "fetched": time.time(), "match": match}),
            encoding="utf-8")
    except OSError:
        pass


def query_api(disc_hash, endpoint=DEFAULT_ENDPOINT, timeout=DEFAULT_TIMEOUT):
    """POST the lookup query. Returns the raw GraphQL `data` dict, or None on
    any failure (network, timeout, HTTP error, bad JSON)."""
    payload = json.dumps({"query": _QUERY, "variables": {"hash": disc_hash}}).encode()
    req = urllib.request.Request(
        endpoint, data=payload,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "Disk-Rip/1.0 (+https://github.com/; disc lookup)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    return body.get("data") if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Selecting the matching disc + joining to our scanned titles
# ---------------------------------------------------------------------------
def _find_disc(data, disc_hash):
    """From the GraphQL data, return (node, release, disc) whose contentHash
    matches ours - or None. Two releases can carry the same pressing (a box set
    reissue); either is correct, so the first match wins."""
    nodes = (((data or {}).get("mediaItems") or {}).get("nodes")) or []
    want = (disc_hash or "").upper()
    for node in nodes:
        for rel in node.get("releases") or []:
            for disc in rel.get("discs") or []:
                if (disc.get("contentHash") or "").upper() == want:
                    return node, rel, disc
    return None


def _norm_source(s):
    return (s or "").strip().lower()


def _norm_seg(s):
    return ",".join(x.strip() for x in (s or "").split(",") if x.strip())


def _parse_hms(text):
    """'0:23:39' -> 1419 seconds. 0 if unparseable."""
    parts = (text or "").strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]


def _as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _parse_episode(val):
    """Parse a TheDiscDb episode field that may be '12', a bare int, or a range
    like '12-13' (a double-length playlist covering two episodes). Returns
    (start, end) with end==start for a single episode, or (None, None) if it
    can't be parsed or spans too many episodes to be a real multi-part - a
    'Play All' is stored as e.g. '9-16' and must NOT be treated as an episode."""
    if isinstance(val, bool):
        return None, None
    if isinstance(val, int):
        return val, val
    s = str(val or "").strip()
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", s)   # "12-13" or "12–13"
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b < a:
            a, b = b, a
        if b - a + 1 > 3:            # wider than a 3-parter -> Play All, skip
            return None, None
        return a, b
    if s.isdigit():
        return int(s), int(s)
    return None, None


def join_titles(our_titles, their_disc):
    """Map our scanned titles onto TheDiscDb's episode assignments.

    Join on what's intrinsic to the mastering and present on both sides:
    (sourceFile, segmentMap) first; a unique sourceFile as a fallback for minor
    formatting drift, sanity-checked by duration. Returns a list of
    {title_id, season, episode, name, type} for every one of *our* titles that
    matched a mapped ('item' non-null) disc title."""
    their = their_disc.get("titles") or []
    by_key, by_source = {}, {}
    for tt in their:
        by_key[(_norm_source(tt.get("sourceFile")), _norm_seg(tt.get("segmentMap")))] = tt
        by_source.setdefault(_norm_source(tt.get("sourceFile")), []).append(tt)

    out = []
    for ot in our_titles:
        key = (_norm_source(ot.source), _norm_seg(ot.segmap))
        tt = by_key.get(key)
        if tt is None:
            cands = by_source.get(_norm_source(ot.source), [])
            if len(cands) == 1:
                cand = cands[0]
                their_dur = _parse_hms(cand.get("duration"))
                # accept the source-only fallback only if runtimes agree closely
                if not their_dur or abs(their_dur - ot.duration) <= 90:
                    tt = cand
        if tt is None:
            continue
        item = tt.get("item")
        if not item:
            continue
        start, end = _parse_episode(item.get("episode"))
        name = item.get("title") or ""
        # skip Play-All titles (no real single episode) and disc-spanning ranges
        if start is None or "play all" in name.lower():
            continue
        out.append({
            "title_id": ot.id,
            "season": _as_int(item.get("season")),
            "episode": start,
            "episode_end": end if end != start else None,   # double episode
            "name": name,
            "type": item.get("type") or "",
        })
    return out


def _build_match(disc, disc_hash, node, rel, their_disc):
    node_type = (node.get("type") or "").lower()
    kind = "tv" if node_type in ("series", "tvshow", "show") else "movie"
    ext = node.get("externalids") or {}
    assignments = join_titles(disc.titles, their_disc)

    # modal season across the mapped episodes (the wizard is single-season;
    # a rare multi-season disc pre-fills the dominant one, rest done by hand)
    season = None
    if kind == "tv":
        seasons = [a["season"] for a in assignments if a.get("season")]
        if seasons:
            season = max(set(seasons), key=seasons.count)

    return {
        "matched": True,
        "kind": kind,
        "hash": disc_hash,
        "tmdb": _as_int(ext.get("tmdb")),
        "imdb": ext.get("imdb") or None,
        "title": node.get("title") or "",
        "year": _as_int(node.get("year")),
        "release": rel.get("title") or rel.get("slug") or "",
        "disc": their_disc.get("name") or f"Disc {their_disc.get('index')}",
        "season": season,
        "assignments": assignments,
    }


def identify(disc, cfg=None, cache_dir=None, endpoint=None, timeout=None):
    """High-level lookup for a scanned Disc. Returns a normalized match dict
    (see _build_match) or None. Never raises - advisory only."""
    cfg = cfg or {}
    if not cfg.get("discdb", True):
        return None
    device = getattr(disc, "device", "")
    disc_hash = content_hash(device)
    if not disc_hash:
        return None

    endpoint = endpoint or cfg.get("discdb_endpoint", DEFAULT_ENDPOINT)
    timeout = timeout or cfg.get("discdb_timeout", DEFAULT_TIMEOUT)
    cdir = _cache_dir(cache_dir or cfg.get("discdb_cache_dir"))

    cached = _cache_read(cdir, disc_hash)
    if cached is not None:
        match = cached.get("match")
        # a cached hit stored assignments against a previous scan's title ids;
        # re-run the join so ids line up with *this* scan (cheap, deterministic)
        if match and match.get("_raw_disc"):
            match = dict(match)
            match["assignments"] = join_titles(disc.titles, match.pop("_raw_disc"))
        return match

    data = query_api(disc_hash, endpoint, timeout)
    if data is None:
        return None                       # transient failure -> don't cache
    found = _find_disc(data, disc_hash)
    if not found:
        _cache_write(cdir, disc_hash, None)   # negative cache (expires)
        return None
    node, rel, their_disc = found
    match = _build_match(disc, disc_hash, node, rel, their_disc)
    # stash the raw disc record so a cache re-read can re-join to a new scan
    to_cache = dict(match)
    to_cache["_raw_disc"] = their_disc
    _cache_write(cdir, disc_hash, to_cache)
    return match


# ---------------------------------------------------------------------------
# Standalone validation (Phase 0): hash a real disc and look it up
# ---------------------------------------------------------------------------
def _main(argv):
    disc_hash = None
    device = None
    args = list(argv)
    if args and args[0] == "--hash" and len(args) > 1:
        disc_hash = args[1].upper()
    elif args:
        device = args[0]
        disc_hash = content_hash(device)
        if not disc_hash:
            print(f"Could not hash a video disc at {device!r} "
                  "(no BDMV\\STREAM or VIDEO_TS, or drive empty).")
            return 1
    else:
        print("usage: py discdb.py <drive-letter>   |   py discdb.py --hash <HASH>")
        return 2

    print(f"ContentHash: {disc_hash}")
    print(f"Querying {DEFAULT_ENDPOINT} ...")
    data = query_api(disc_hash)
    if data is None:
        print("  (no response / network error)")
        return 1
    found = _find_disc(data, disc_hash)
    if not found:
        print("  Not in TheDiscDb.")
        return 0
    node, rel, their_disc = found
    ext = node.get("externalids") or {}
    print(f"  Match: {node.get('title')} ({node.get('year')})  [{node.get('type')}]")
    print(f"         tmdb:{ext.get('tmdb')}  imdb:{ext.get('imdb')}")
    print(f"         release: {rel.get('title') or rel.get('slug')}  -  "
          f"{their_disc.get('name') or 'disc ' + str(their_disc.get('index'))}")
    mapped = [t for t in (their_disc.get("titles") or []) if t.get("item")]
    print(f"  {len(mapped)} mapped title(s):")
    for tt in mapped:
        it = tt["item"]
        loc = (f"s{it.get('season')}e{it.get('episode')}"
               if it.get("episode") is not None else it.get("type") or "?")
        print(f"    {tt.get('sourceFile'):<14} seg={tt.get('segmentMap'):<20} "
              f"{loc:<8} {it.get('title') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
