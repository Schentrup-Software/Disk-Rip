#!/usr/bin/env python3
"""
webapp.py - Local web UI for Disk-Rip.

Starts a small HTTP server (stdlib only) and opens a browser page that lets you:
  pick a drive -> scan the disc -> drag disc titles onto episodes -> rip.

All the real work (MakeMKV scan/rip, TMDB lookup, segment-map de-dup, naming) is
done by diskrip.py; this file is just a thin JSON API + static file server on top.

Run:  py webapp.py            (opens http://127.0.0.1:8765 in your browser)
"""

import json
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import diskrip as dr

HOST = "127.0.0.1"
PORT = 8765
HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# Application state (single-user local tool -> one shared state object)
# ---------------------------------------------------------------------------
class App:
    def __init__(self, config_path):
        self.cfg = dr.load_config(config_path)
        self.min_len = int(self.cfg.get("min_length_minutes", 15)) * 60
        self.movie_min = int(self.cfg.get("movie_min_minutes", 70)) * 60
        self.mk = dr.MakeMKV(self.cfg["makemkvcon"])
        self.tmdb = dr.TMDB(self.cfg["tmdb_api_key"], self.cfg.get("language", "en"))
        self.disc = None
        self.tv = None            # dr.TvProposal (naming + buckets, reused across selects)
        self.movie = None         # dr.MovieProposal
        self.ripjob = None        # dict: progress state for the current rip
        self.lock = threading.Lock()

    # --- title bucketing --------------------------------------------------
    def title_rows(self):
        """Every disc title tagged with its bucket, for the UI."""
        dups = self.tv.duplicate_ids() if self.tv else set()
        rows = []
        for t in self.disc.titles:
            if t.duration >= self.movie_min:
                bucket = "playall"
            elif t.duration < self.min_len:
                bucket = "short"
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
            })
        return rows

    # --- endpoint implementations ----------------------------------------
    def api_drives(self, _body):
        return [
            {"index": idx, "name": name, "label": label, "loaded": loaded}
            for idx, name, label, loaded in self.mk.list_drives()
        ]

    def api_scan(self, body):
        drive = int(body["drive"])
        self.disc = self.mk.scan(drive)          # may raise DiskRipError (stall)
        self.tv = dr.TvProposal(self.tmdb, self.disc, self.min_len,
                                self.movie_min, self.cfg["tv_root"])
        self.movie = dr.MovieProposal(self.tmdb, self.disc, self.movie_min)
        query, hints = dr.guess_query_from_label(self.disc.label)
        disc_type = dr.classify(self.disc, self.movie_min, self.min_len)
        return {
            "label": self.disc.label,
            "type": disc_type,
            "query": query,
            "hints": hints,
            "titles": self.title_rows(),
        }

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
            fname = self.tv.file_for(epnum)
            target = season_dir / fname
            out.append({"title_id": int(a["title_id"]),
                        "episode": epnum, "target": str(target),
                        "exists": target.exists()})
        return out

    # --- ripping (runs in a background thread) ----------------------------
    def api_rip(self, body):
        with self.lock:
            if self.ripjob and self.ripjob.get("running"):
                raise dr.DiskRipError("A rip is already in progress.")
            kind = body["kind"]
            items = []
            if kind == "movie":
                mt = self.movie.main_title
                folder, fname = self.movie.folder_and_file()
                items.append({
                    "title_id": mt.id, "label": self.movie.title,
                    "target": str(Path(self.cfg["movie_root"]) / folder / fname),
                    "pct": 0, "status": "queued",
                })
            else:
                season_dir = self.tv.season_dir()
                for a in body["assignments"]:
                    epnum = int(a["episode"])
                    fname = self.tv.file_for(epnum)
                    ep = self.tv.episode_meta(epnum)
                    items.append({
                        "title_id": int(a["title_id"]),
                        "episode": epnum,
                        "label": f"s{self.tv.season_number:02d}e{epnum:02d} "
                                 f"{ep.get('name','')}".strip(),
                        "target": str(season_dir / fname),
                        "pct": 0, "status": "queued",
                    })
            self.ripjob = {"running": True, "kind": kind, "items": items,
                           "error": None, "started": time.time()}
        threading.Thread(target=self._rip_worker, args=(kind,), daemon=True).start()
        return {"started": True, "count": len(self.ripjob["items"])}

    def _rip_worker(self, kind):
        import shutil
        job = self.ripjob
        titles = {t.id: t for t in self.disc.titles}
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
                    continue
                item["status"] = "ripping"

                def progress(pct, _item=item):
                    _item["pct"] = pct

                ripped = self.mk.rip(self.disc.drive_index, t,
                                     self.cfg["work_dir"], on_progress=progress)
                if not ripped:
                    item["status"] = "failed"
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(ripped), str(target))
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
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            return self._send_file(HERE / "ui" / "index.html", "text/html; charset=utf-8")
        if route == "/api/rip/status":
            return self._dispatch("api_rip_status", {})
        self.send_error(404)

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
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    Handler.app = App(args.config)
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    print(f"Disk-Rip web UI running at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
