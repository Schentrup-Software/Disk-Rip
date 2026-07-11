/*
 * demo.js - static-demo harness for the Disk-Rip web UI.
 *
 * The real UI (../ui/index.html) is a single self-contained file that talks to a
 * Python backend over /api/*. This harness makes it run with NO backend:
 *   1. it overrides window.fetch to answer every /api/* call from mock-data.json,
 *   2. it loads the real ui/index.html verbatim (markup + styles + script) so the
 *      demo never drifts from the actual UI, and
 *   3. it computes /api/preview and /api/rip results from whatever the user
 *      actually assigns, and animates a fake rip so progress bars move.
 *
 * Four "drives" map to the four cases:
 *   0 movie found in TheDiscDb   1 movie not found
 *   2 TV found in TheDiscDb      3 TV not found
 */
(function () {
  "use strict";

  var DATA = null;
  var mock = { drive: 0, rip: null };
  var realFetch = window.fetch.bind(window);

  function scenario() { return DATA.scenarios[String(mock.drive)]; }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function p2(n) { return String(n).padStart(2, "0"); }
  function jsonResponse(obj) {
    return new Response(JSON.stringify(obj), {
      status: 200, headers: { "Content-Type": "application/json" }
    });
  }

  // A small inline "film frame" so titles show a thumbnail without any backend.
  function thumb(id) {
    var hues = [210, 340, 150, 45, 275, 190, 20, 300];
    var h = hues[id % hues.length];
    var svg =
      "<svg xmlns='http://www.w3.org/2000/svg' width='96' height='54'>" +
      "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>" +
      "<stop offset='0' stop-color='hsl(" + h + " 55% 34%)'/>" +
      "<stop offset='1' stop-color='hsl(" + ((h + 40) % 360) + " 50% 20%)'/>" +
      "</linearGradient></defs><rect width='96' height='54' fill='url(#g)'/>" +
      "<circle cx='48' cy='27' r='11' fill='rgba(255,255,255,.16)'/>" +
      "<path d='M44 21 l11 6 l-11 6 z' fill='rgba(255,255,255,.85)'/></svg>";
    return "data:image/svg+xml," + encodeURIComponent(svg);
  }

  function decorateScan(scan) {
    (scan.titles || []).forEach(function (t) {
      if (t.bucket === "episode" || t.bucket === "duplicate" || t.bucket === "multi") {
        t.thumb = thumb(t.id);
      }
    });
    return scan;
  }

  // Build per-episode rows for /api/preview and /api/rip from the user's actual
  // assignments (mirrors what webapp.py does server-side).
  function tvRows(assignments, sc, forRip) {
    var m = sc.meta, season = sc.select.season;
    return (assignments || []).map(function (a) {
      var end = a.episode_end;
      var span = "s" + p2(season) + "e" + p2(a.episode) + (end ? "-e" + p2(end) : "");
      var fname = m.fileShow + " - " + span + ".mkv";
      var target = m.seasonDir + fname;
      var exists = (m.existing || []).indexOf(a.episode) !== -1;
      if (!forRip) {
        return { title_id: a.title_id, episode: a.episode, episode_end: end || null,
                 target: target, exists: exists };
      }
      var name = m.episodes[String(a.episode)] || "";
      return { title_id: a.title_id, episode: a.episode,
               label: (span + " " + name).trim(), target: target,
               pct: 0, status: exists ? "skipped" : "queued", phase: "", exists: exists };
    });
  }

  // Time-based fake rip: each non-skipped item runs Analysing 0-100% then
  // Ripping 0-100%, in order, so the bars and phase label animate.
  function ripStatus() {
    var r = mock.rip;
    if (!r) return { running: false, items: [] };
    var PER = 3200, ANALYSE = 0.4;           // ms per item, fraction spent analysing
    var elapsed = Date.now() - r.start, idx = 0, toRip = 0;
    r.items.forEach(function (it) {
      if (it.status === "skipped" || it.exists) { it.status = "skipped"; it.pct = 100; return; }
      toRip++;
      var local = elapsed - idx * PER; idx++;
      if (local <= 0) { it.status = "queued"; it.pct = 0; it.phase = ""; }
      else if (local >= PER) { it.status = "done"; it.pct = 100; it.phase = ""; }
      else {
        it.status = "ripping";
        var aT = PER * ANALYSE;
        if (local < aT) { it.phase = "Analysing"; it.pct = Math.round(local / aT * 100); }
        else { it.phase = "Ripping"; it.pct = Math.round((local - aT) / (PER - aT) * 100); }
      }
    });
    return { running: elapsed < toRip * PER, kind: r.kind, items: r.items, error: null };
  }

  function handle(path, body) {
    var sc = scenario();
    switch (path) {
      case "/api/drives": return DATA.drives;
      case "/api/scan":
        mock.drive = body.drive; mock.rip = null;
        return decorateScan(clone(scenario().scan));
      case "/api/search":
        return (sc.search && sc.search[body.kind]) || [];
      case "/api/select":
        return clone(sc.select);
      case "/api/preview":
        if (sc.kind === "movie") {
          return [{ title_id: sc.select.mainTitleId, label: "movie",
                    target: sc.select.target, exists: sc.select.exists }];
        }
        return tvRows(body.assignments, sc, false);
      case "/api/rip": {
        var items;
        if (sc.kind === "movie") {
          items = [{ title_id: sc.select.mainTitleId, label: sc.select.title,
                     target: sc.select.target, pct: 0, status: "queued", phase: "", exists: false }];
        } else {
          items = tvRows(body.assignments, sc, true);
        }
        mock.rip = { items: items, kind: sc.kind, start: Date.now() };
        return { started: true, count: items.length };
      }
      case "/api/rip/status": return ripStatus();
      case "/api/thumbs/status": return { total: 0, done: 0, running: false };
      default: return { error: "not mocked: " + path };
    }
  }

  // ---- fetch override ----
  window.fetch = function (input, opts) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    try {
      var u = new URL(url, location.origin);
      if (u.pathname.indexOf("/api/") === 0) {
        var body = opts && opts.body ? JSON.parse(opts.body) : {};
        return Promise.resolve(jsonResponse(handle(u.pathname, body)));
      }
    } catch (e) { /* fall through to the real fetch for anything non-/api */ }
    return realFetch(input, opts);
  };

  // ---- load the real UI verbatim, then run it ----
  async function boot() {
    DATA = await (await realFetch("mock-data.json")).json();
    var html = await (await realFetch("../ui/index.html")).text();
    var doc = new DOMParser().parseFromString(html, "text/html");

    doc.querySelectorAll("style").forEach(function (s) {
      var el = document.createElement("style"); el.textContent = s.textContent;
      document.head.appendChild(el);
    });
    var scripts = Array.prototype.slice.call(doc.querySelectorAll("script"));
    scripts.forEach(function (s) { s.remove(); });
    document.getElementById("app").innerHTML = doc.body.innerHTML;
    // run the UI script in global scope so its functions back the inline handlers
    scripts.forEach(function (s) {
      var el = document.createElement("script"); el.textContent = s.textContent;
      document.body.appendChild(el);
    });
  }

  boot().catch(function (err) {
    document.getElementById("app").innerHTML =
      "<div style='padding:24px;color:#f85149;font:14px sans-serif'>" +
      "This demo must be served over HTTP, not opened from the filesystem.<br>" +
      "Run <code>python -m http.server</code> in the repo root and open " +
      "<code>/demo/</code>, or view the deployed GitHub Pages site.<br><br>" +
      "<span style='color:#8b98a5'>(" + err + ")</span></div>";
  });
})();
