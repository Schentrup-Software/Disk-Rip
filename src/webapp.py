#!/usr/bin/env python3
"""
webapp.py - Local web UI for Disk-Rip.

Starts a small HTTP server (stdlib only) and opens a browser page that lets you:
  pick a drive -> scan the disc -> drag disc titles onto episodes -> rip.
...or, on the Library page (/library), organize a folder of discs you have
already ripped: scan -> review the matches -> move into the movie library.

All the real work (MakeMKV scan/rip, TMDB lookup, segment-map de-dup, naming) is
done by diskrip.py and library.py; this file is just a thin JSON API + static
file server on top.

Run:  py webapp.py            (Windows; opens http://127.0.0.1:8765)
      python3 src/webapp.py   (macOS / Linux)
"""

import json
import os
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import diskrip as dr
import discdb
import library
import paths
from thumbs import Thumbnailer

HOST = "127.0.0.1"
PORT = 8765
HERE = Path(__file__).parent      # src/
ROOT = HERE.parent                # repo root (holds config.json, ui/)


# ---------------------------------------------------------------------------
# Application state (single-user local tool -> one shared state object)
# ---------------------------------------------------------------------------
class App:
    def __init__(self, config_path):
        self.cfg = dr.load_config(config_path)
        self.min_len = int(self.cfg.get("min_length_minutes", 15)) * 60
        self.movie_min = int(self.cfg.get("movie_min_minutes", 70)) * 60
        self._mk = None           # created on first use - see the mk property
        self.tmdb = dr.TMDB(self.cfg["tmdb_api_key"], self.cfg.get("language", "en"))
        self.disc = None
        self.tv = None            # dr.TvProposal (naming + buckets, reused across selects)
        self.movie = None         # dr.MovieProposal
        self.ripjob = None        # dict: progress state for the current rip
        self.thumbjob = None      # dict: thumbnail prefetch progress
        self.discdb_ep_titles = set()    # title ids TheDiscDb mapped to an episode
        self.discdb_multi_titles = set() # ...of those, the ones spanning a range
        self.lib = None           # library.LibraryScan from the last library scan
        self.libjob = None        # dict: progress of the running library scan
        self.applyjob = None      # dict: progress of the running library move
        self.libthumbs = library.LibThumbs(self.cfg)
        self.lock = threading.Lock()
        self.thumbnailer = Thumbnailer(
            self.cfg.get("ffmpeg"), self.cfg["work_dir"],
            makemkvcon_path=self.cfg.get("makemkvcon"), opts=self.cfg)

    @property
    def mk(self):
        """MakeMKV is only needed to scan/rip a disc, so it is created on first
        use - the Library page works on a machine without MakeMKV."""
        if self._mk is None:
            self._mk = dr.MakeMKV(self.cfg.get("makemkvcon") or "")
        return self._mk

    # --- title bucketing --------------------------------------------------
    def title_rows(self):
        """Every disc title tagged with its bucket + duplicate group, for the UI."""
        dups = self.tv.duplicate_ids() if self.tv else set()
        gmap = self.tv.group_map() if self.tv else {}
        reps = self.tv.representative_ids() if self.tv else set()
        rep_of = self.tv.rep_of_map() if self.tv else {}
        lowres = {t.id for t in self.tv.excluded_lowres()} if self.tv else set()
        notinpa = {t.id for t in self.tv.excluded_notinplayall()} if self.tv else set()
        thumbs_on = (self.thumbnailer.available()
                     and bool(getattr(self.disc, "device", "")))
        rows = []
        for t in self.disc.titles:
            if t.id in self.discdb_multi_titles:
                bucket = "multi"       # community-confirmed multi-part (e.g. e18-e21)
            elif t.id in self.discdb_ep_titles:
                bucket = "episode"     # community-confirmed episode - never a Play-All
            elif t.duration >= self.movie_min:
                bucket = "playall"
            elif t.duration < self.min_len:
                bucket = "short"
            elif t.id in lowres:
                bucket = "lowres"
            elif t.id in notinpa:
                bucket = "notinplayall"
            elif t.id in dups:
                bucket = "duplicate"
            else:
                bucket = "episode"
            rows.append({
                "id": t.id,
                "duration": t.duration,
                "hms": dr.hms(t.duration),
                "resolution": t.resolution or "",
                "chapters": t.chapters,
                "segmap": t.segmap,
                "bucket": bucket,
                "group": gmap.get(t.id),
                "isRep": t.id in reps,
                # duplicates share their representative's frame (same episode) so
                # the browser fetches one image per group, not one per title
                "thumb": (f"/api/thumb?title={rep_of.get(t.id, t.id)}&pos=mid"
                          if thumbs_on and bucket in ("episode", "duplicate", "multi") else None),
            })
        return rows

    # --- endpoint implementations ----------------------------------------
    def api_drives(self, _body):
        return [
            {"index": idx, "name": name, "label": label, "loaded": loaded}
            for idx, name, label, loaded, _device in self.mk.list_drives()
        ]

    def api_scan(self, body):
        drive = int(body["drive"])
        self.disc = self.mk.scan(drive)          # may raise DiskRipError (stall)
        # capture the optical device path (e.g. "E:") for ffmpeg bluray: access
        for idx, _n, _l, _loaded, device in self.mk.list_drives():
            if idx == drive:
                self.disc.device = device
                break
        self.tv = dr.TvProposal(self.tmdb, self.disc, self.min_len,
                                self.movie_min, self.cfg["tv_root"],
                                float(self.cfg.get("segment_overlap_threshold", 0.6)))
        self.movie = dr.MovieProposal(self.tmdb, self.disc, self.movie_min)
        query, hints = dr.guess_query_from_label(self.disc.label)
        disc_type = dr.classify(self.disc, self.movie_min, self.min_len)
        db = self._discdb_lookup()
        self._prefetch_thumbs(self.disc)
        return {
            "label": self.disc.label,
            "type": db["kind"] if db else disc_type,   # community data wins the type call
            "query": query,
            "hints": hints,
            "titles": self.title_rows(),
            "discdb": db,
        }

    def _discdb_lookup(self):
        """Ask TheDiscDb whether it knows this disc. Advisory: any failure -> None
        and the wizard runs its normal identify flow. On a hit, collapse the
        community's per-title mapping to one title per episode (preferring our
        representative when they mapped an intro/no-intro pair) so the board can
        pre-fill directly."""
        self.discdb_ep_titles = set()
        self.discdb_multi_titles = set()
        try:
            match = discdb.identify(self.disc, self.cfg)
        except Exception:
            traceback.print_exc()
            return None
        if not match:
            return None
        reps = self.tv.representative_ids() if self.tv else set()

        def score(a):
            # rank candidates that TheDiscDb mapped to the same episode: avoid
            # commentary/alternate-audio tracks, prefer our representative title,
            # and prefer a real multi-part title over a single (so the double
            # "12-13" beats a lone "12 - With Commentary" extra)
            name = (a.get("name") or "").lower()
            return (0 if "commentary" in name else 1,
                    1 if a["title_id"] in reps else 0,
                    1 if a.get("episode_end") else 0)

        best = {}
        for a in match.get("assignments", []):
            if a.get("episode") is None:
                continue
            key = (a.get("season"), a["episode"])
            cur = best.get(key)
            if cur is None or score(a) > score(cur):
                best[key] = a
        # a double (e19-e20) already contains its later episodes, so drop any
        # separate assignment for one they swallow (e.g. a lone "e20 commentary")
        covered = set()
        for a in best.values():
            if a.get("episode_end"):
                for n in range(a["episode"] + 1, a["episode_end"] + 1):
                    covered.add((a.get("season"), n))
        final = sorted(
            (a for k, a in best.items() if k not in covered),
            key=lambda a: (a.get("season") or 0, a["episode"]))
        match["assignments"] = final
        # remember which of our titles TheDiscDb actually mapped, so title_rows()
        # doesn't mislabel a mapped-but-long finale as a 'Play All'
        self.discdb_ep_titles = {a["title_id"] for a in final}
        self.discdb_multi_titles = {a["title_id"] for a in final if a.get("episode_end")}
        return match

    def _prefetch_thumbs(self, disc):
        """Warm the thumbnail cache in the background right after a scan, so
        frames are ready by the time the matching board renders. Only the
        representative of each duplicate group is extracted (one frame per unique
        episode); duplicates reuse it. Progress is exposed via /api/thumbs/status."""
        if not self.thumbnailer.available() or not getattr(disc, "device", ""):
            self.thumbjob = {"total": 0, "done": 0, "running": False}
            return
        reps = self.tv.representative_ids()
        targets = [t for t in disc.titles
                   if t.id in reps and self.min_len <= t.duration < self.movie_min]
        self.thumbjob = {"total": len(targets), "done": 0, "running": True}

        def worker():
            for t in targets:
                if self.disc is not disc:                 # a newer scan replaced it
                    return
                if self.ripjob and self.ripjob.get("running"):
                    break                                 # don't fight a rip for the drive
                try:
                    self.thumbnailer.frame(disc, t, "mid")
                except Exception:
                    pass
                self.thumbjob["done"] += 1
            self.thumbjob["running"] = False

        threading.Thread(target=worker, daemon=True).start()

    def api_thumbs_status(self, _body):
        return getattr(self, "thumbjob", None) or {"total": 0, "done": 0, "running": False}

    def api_search(self, body):
        kind, query = body["kind"], body["query"].strip()
        year = body.get("year") or None
        if kind == "movie":
            results = self.tmdb.search_movie(query, year)
            return [{"id": r["id"], "name": r.get("title", ""),
                     "year": dr.year_of(r.get("release_date", "")),
                     "overview": r.get("overview", "")} for r in results[:12]]
        results = self.tmdb.search_tv(query, year)
        return [{"id": r["id"], "name": r.get("name", ""),
                 "year": dr.year_of(r.get("first_air_date", "")),
                 "overview": r.get("overview", "")} for r in results[:12]]

    def api_select(self, body):
        kind = body["kind"]
        tmdb_id = int(body["id"])
        if kind == "movie":
            self.movie.choose(tmdb_id)
            folder, fname = self.movie.folder_and_file()
            mt = self.movie.main_title
            return {
                "kind": "movie",
                "title": self.movie.title, "year": self.movie.year,
                "imdb": self.movie.imdb, "folder": folder, "file": fname,
                "runtime": (self.movie.details.get("runtime") or 0),
                "mainTitleId": mt.id if mt else None,
                "target": str(Path(self.cfg["movie_root"]) / folder / fname),
                "exists": (Path(self.cfg["movie_root"]) / folder / fname).exists(),
            }
        # --- TV ---
        p = self.tv
        p.choose(tmdb_id)
        p.season_number = int(body.get("season") or p.season_number or 1)
        p.load_season()
        existing = sorted(p.existing_episodes())
        suggested_start = p.suggested_start()
        episodes = [{
            "number": e.get("episode_number"),
            "name": e.get("name") or "",
            "air": e.get("air_date") or "",
            "runtime": e.get("runtime"),
            "overview": e.get("overview") or "",
            "exists": e.get("episode_number") in set(existing),
        } for e in p.episodes]
        active = p.active_titles()
        suggested = [{"title_id": t.id, "episode": suggested_start + i}
                     for i, t in enumerate(active)]
        return {
            "kind": "tv",
            "title": p.title, "year": p.year, "imdb": p.imdb,
            "showFolder": p.show_folder(), "season": p.season_number,
            "seasonCount": len(episodes), "suggestedStart": suggested_start,
            "existing": existing, "episodes": episodes, "suggested": suggested,
        }

    def api_preview(self, body):
        """Compute final target paths for a proposed set of assignments."""
        kind = body["kind"]
        out = []
        if kind == "movie":
            folder, fname = self.movie.folder_and_file()
            target = Path(self.cfg["movie_root"]) / folder / fname
            out.append({"title_id": self.movie.main_title.id,
                        "label": "movie", "target": str(target),
                        "exists": target.exists()})
            return out
        season_dir = self.tv.season_dir()
        for a in body["assignments"]:
            epnum = int(a["episode"])
            ep_end = int(a["episode_end"]) if a.get("episode_end") else None
            fname = self.tv.file_for(epnum, ep_end)
            target = season_dir / fname
            out.append({"title_id": int(a["title_id"]),
                        "episode": epnum, "episode_end": ep_end,
                        "target": str(target), "exists": target.exists()})
        return out

    # --- ripping (runs in a background thread) ----------------------------
    def api_rip(self, body):
        with self.lock:
            if self.ripjob and self.ripjob.get("running"):
                raise dr.DiskRipError("A rip is already in progress.")
            kind = body["kind"]
            # refuse up front rather than after a long rip: the target root
            # must be a real, writable library folder
            dr.root_for(self.cfg, "movies" if kind == "movie" else "tv")
            items = []
            if kind == "movie":
                mt = self.movie.main_title
                folder, fname = self.movie.folder_and_file()
                items.append({
                    "title_id": mt.id, "label": self.movie.title,
                    "target": str(Path(self.cfg["movie_root"]) / folder / fname),
                    "pct": 0, "status": "queued", "phase": "",
                })
            else:
                season_dir = self.tv.season_dir()
                for a in body["assignments"]:
                    epnum = int(a["episode"])
                    ep_end = int(a["episode_end"]) if a.get("episode_end") else None
                    fname = self.tv.file_for(epnum, ep_end)
                    ep = self.tv.episode_meta(epnum)
                    span = (f"s{self.tv.season_number:02d}e{epnum:02d}"
                            + (f"-e{ep_end:02d}" if ep_end else ""))
                    items.append({
                        "title_id": int(a["title_id"]),
                        "episode": epnum,
                        "label": f"{span} {ep.get('name','')}".strip(),
                        "target": str(season_dir / fname),
                        "pct": 0, "status": "queued", "phase": "",
                    })
            self.ripjob = {"running": True, "kind": kind, "items": items,
                           "error": None, "started": time.time()}
        threading.Thread(target=self._rip_worker, args=(kind,), daemon=True).start()
        return {"started": True, "count": len(self.ripjob["items"])}

    def _rip_worker(self, kind):
        import shutil
        job = self.ripjob
        titles = {t.id: t for t in self.disc.titles}
        root_name = "movies" if kind == "movie" else "tv"
        try:
            for item in job["items"]:
                t = titles.get(item["title_id"])
                target = Path(item["target"])
                if target.exists():
                    item["status"] = "skipped"
                    item["pct"] = 100
                    continue
                if t is None:
                    item["status"] = "failed"
                    item["error"] = "title not found on the disc"
                    continue
                item["status"] = "ripping"

                def progress(pct, phase="", _item=item):
                    _item["pct"] = pct
                    _item["phase"] = phase

                ripped = self.mk.rip(self.disc.drive_index, t,
                                     self.cfg["work_dir"], on_progress=progress)
                if not ripped:
                    item["status"] = "failed"
                    item["error"] = self.mk.last_rip_error or "rip failed"
                    continue
                try:
                    dr.root_for(self.cfg, root_name)   # the share may have dropped
                    paths.check_destination(target.parent)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(ripped), str(target))
                except paths.RootError as e:
                    item["status"] = "failed"
                    item["error"] = str(e)
                    job["root"] = e.name
                    continue
                except OSError as e:
                    item["status"] = "failed"
                    item["error"] = f"couldn't move to NAS: {e}"
                    continue
                item["pct"] = 100
                item["status"] = "done"
        except dr.DiskRipError as e:
            job["error"] = str(e)
        except Exception as e:  # never let the worker die silently
            job["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            job["running"] = False

    def api_rip_status(self, _body):
        return self.ripjob or {"running": False, "items": []}

    # --- library mode: organize already-ripped folders ---------------------
    def api_library_config(self, _body):
        return {
            "root": self.cfg.get("rips_root", ""),
            "movieRoot": self.cfg.get("movie_root", ""),
            "categories": library.CATEGORIES,
            "thumbs": self.libthumbs.available(),
            "makemkv": os.path.isfile(self.cfg.get("makemkvcon") or ""),
        }

    def _need_lib(self):
        if self.lib is None:
            raise dr.DiskRipError("Scan the folder first.")
        return self.lib

    def _lib_json(self, overrides):
        """The scan plus, per file, where it would go under the current choices."""
        lib = self._need_lib()
        overrides = overrides or {}
        data = lib.to_json(self.cfg)
        items = library.build_plan(lib, self.cfg, overrides)
        data["plan"] = {i["file"]: {"role": i["role"], "category": i["category"],
                                    "name": i["name"], "target": i["target"],
                                    "status": i["status"]} for i in items}
        gov = overrides.get("groups") or {}
        data["include"] = {
            g.id: bool((gov.get(g.id) or {}).get("include", library.default_include(g)))
            for g in lib.groups}
        data["thumbs"] = self.libthumbs.available()
        return data

    def api_library_scan(self, body):
        with self.lock:
            if self.libjob and self.libjob.get("running"):
                raise dr.DiskRipError("A library scan is already running.")
            if self.applyjob and self.applyjob.get("running"):
                raise dr.DiskRipError("Files are being moved - wait for that to finish.")
            root = (body.get("root") or "").strip() or None
            job = self.libjob = {"running": True, "phase": "Starting", "done": 0,
                                 "total": 0, "error": None}

        def worker():
            def progress(phase, done, total):
                job["phase"], job["done"], job["total"] = phase, done, total
            try:
                self.lib = library.scan_library(self.cfg, self.tmdb, root, progress)
            except paths.RootError as e:
                job["error"], job["root"] = str(e), e.name
            except dr.DiskRipError as e:
                job["error"] = str(e)
            except Exception as e:
                traceback.print_exc()
                job["error"] = f"{type(e).__name__}: {e}"
            finally:
                job["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True}

    def api_library_status(self, _body):
        job = dict(self.libjob or {"running": False, "error": None})
        if not job.get("running") and not job.get("error") and self.lib is not None:
            job["result"] = self._lib_json(None)
        return job

    def api_library_plan(self, body):
        return self._lib_json(body.get("overrides"))

    def api_library_search(self, body):
        lib = self._need_lib()
        query = (body.get("query") or "").strip()
        if not query:
            return []
        year = body.get("year") or None
        return [lib.identifier._trim(c) for c in lib.identifier.search(query, year, limit=8)]

    def api_library_select(self, body):
        """Pin a group to a specific TMDB movie, then re-pick its main feature
        against that movie's runtime."""
        lib = self._need_lib()
        g = lib.by_id.get(body.get("group"))
        if g is None:
            raise dr.DiskRipError("Unknown movie group - scan again.")
        lib.identifier.choose(g, int(body["id"]))
        library.classify_group(g, self.cfg)
        return self._lib_json(body.get("overrides"))

    def api_library_apply(self, body):
        with self.lock:
            lib = self._need_lib()
            if self.applyjob and self.applyjob.get("running"):
                raise dr.DiskRipError("A move is already in progress.")
            items = library.build_plan(lib, self.cfg, body.get("overrides"))
            todo = [i for i in items if i["status"] == "move"]
            if not todo:
                raise dr.DiskRipError("Nothing to move - tick at least one matched movie.")
            for i in todo:
                i["state"], i["pct"] = "queued", 0
            job = self.applyjob = {"running": True, "items": todo, "error": None,
                                   "log": None, "pruned": []}
        prune = bool(body.get("prune"))

        def worker():
            try:
                log = library.apply_plan(items, self.cfg, lib, prune=prune)
                job["log"], job["pruned"] = log.get("path"), log.get("pruned", [])
            except paths.RootError as e:
                job["error"], job["root"] = str(e), e.name
                for i in todo:
                    if i.get("state") == "queued":
                        i["state"], i["error"] = "failed", "not started"
            except Exception as e:
                traceback.print_exc()
                job["error"] = f"{type(e).__name__}: {e}"
            finally:
                job["running"] = False
                self.lib = None      # paths changed - the old scan is stale

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "count": len(todo)}

    def api_library_apply_status(self, _body):
        job = self.applyjob
        if not job:
            return {"running": False, "items": []}
        return {
            "running": job["running"], "error": job["error"], "log": job["log"],
            "pruned": job["pruned"],
            "items": [{"src": i["src"], "target": i["target"], "role": i["role"],
                       "state": i.get("state", "queued"), "pct": i.get("pct", 0),
                       "error": i.get("error")} for i in job["items"]],
        }

    def api_library_undo(self, body):
        """Reverse a finished move from its log (only logs this tool wrote)."""
        if self.applyjob and self.applyjob.get("running"):
            raise dr.DiskRipError("A move is still in progress.")
        log_dir = (Path(self.cfg["work_dir"]) / "library-logs").resolve()
        log = Path(body.get("log") or "").resolve()
        if log.parent != log_dir or not log.is_file():
            raise dr.DiskRipError("That isn't one of Disk-Rip's move logs.")
        restored, problems = library.undo(log)
        self.lib = None
        return {"restored": restored, "problems": problems}

    def library_thumb(self, file_id):
        if self.lib is None:
            return None
        f = self.lib.files.get(file_id)
        return self.libthumbs.frame(f) if f else None

    def thumb_frame(self, title_id, pos):
        """Path to a cached JPEG frame for a title, or None. Never reads the
        drive while a rip is running (one program owns the drive)."""
        if self.ripjob and self.ripjob.get("running"):
            return None
        if not self.disc:
            return None
        title = next((t for t in self.disc.titles if t.id == title_id), None)
        if title is None:
            return None
        return self.thumbnailer.frame(self.disc, title, pos)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
ROUTES = {
    "/api/drives": "api_drives",
    "/api/scan": "api_scan",
    "/api/search": "api_search",
    "/api/select": "api_select",
    "/api/preview": "api_preview",
    "/api/rip": "api_rip",
    "/api/rip/status": "api_rip_status",
    "/api/library/scan": "api_library_scan",
    "/api/library/plan": "api_library_plan",
    "/api/library/search": "api_library_search",
    "/api/library/select": "api_library_select",
    "/api/library/apply": "api_library_apply",
    "/api/library/undo": "api_library_undo",
}

GET_ROUTES = {
    "/api/rip/status": "api_rip_status",
    "/api/thumbs/status": "api_thumbs_status",
    "/api/library/config": "api_library_config",
    "/api/library/status": "api_library_status",
    "/api/library/apply/status": "api_library_apply_status",
}


class Handler(BaseHTTPRequestHandler):
    app = None  # set in main()

    def log_message(self, *_):
        pass  # quiet

    def _send_json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, ctype):
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            return self._send_file(ROOT / "ui" / "index.html", "text/html; charset=utf-8")
        if route in ("/library", "/library.html"):
            return self._send_file(ROOT / "ui" / "library.html", "text/html; charset=utf-8")
        if route in GET_ROUTES:
            return self._dispatch(GET_ROUTES[route], {})
        if route == "/api/thumb":
            return self._send_thumb(parse_qs(parsed.query))
        if route == "/api/library/thumb":
            return self._send_library_thumb(parse_qs(parsed.query))
        self.send_error(404)

    def _send_library_thumb(self, qs):
        try:
            path = self.app.library_thumb(qs.get("id", [""])[0])
        except Exception:
            traceback.print_exc()
            path = None
        if not path:
            return self.send_error(404)
        self._send_file(path, "image/jpeg")

    def _send_thumb(self, qs):
        try:
            title_id = int(qs.get("title", ["-1"])[0])
        except ValueError:
            return self.send_error(400)
        pos = qs.get("pos", ["mid"])[0]
        if pos not in ("mid", "tail"):
            pos = "mid"
        try:
            path = self.app.thumb_frame(title_id, pos)
        except Exception:
            traceback.print_exc()
            path = None
        if not path:
            return self.send_error(404)
        self._send_file(path, "image/jpeg")

    def do_POST(self):
        route = urlparse(self.path).path
        handler = ROUTES.get(route)
        if not handler:
            return self.send_error(404)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send_json({"error": "bad JSON"}, 400)
        self._dispatch(handler, body)

    def _dispatch(self, handler_name, body):
        try:
            result = getattr(self.app, handler_name)(body)
            self._send_json(result)
        except dr.DiskRipError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Local web UI for Disk-Rip.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--library", action="store_true",
                    help="Open on the Library page (organize already-ripped folders).")
    args = ap.parse_args()

    Handler.app = App(args.config)
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    # No MakeMKV on this machine -> there is nothing to rip with, so land on
    # the Library page instead of an empty drive list.
    has_makemkv = os.path.isfile(Handler.app.cfg.get("makemkvcon") or "")
    start = url + "/library" if (args.library or not has_makemkv) else url
    print(f"Disk-Rip web UI running at {url}")
    print(f"  Rip a disc:        {url}/")
    print(f"  Organize a folder: {url}/library")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(start)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
