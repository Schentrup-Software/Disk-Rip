# Integrating Disk-Rip with TheDiscDb

*Companion to [DISC-DATABASE-REPORT.md](DISC-DATABASE-REPORT.md). The original
evaluation (done 2026-07-05) concluded: **use TheDiscDb for lookups; contribute
back when we had to match manually.** The **read path (lookup) is now
implemented** — see the status block below. This document now tracks the
remaining **write path (contribute)**, which is intentionally on hold.*

---

## Read path — DONE ✅

The lookup half shipped. A scanned disc is hashed and looked up against
TheDiscDb; on a hit the wizard pre-fills identity and the episode board from the
community's mapping, and the CLI skips the TMDB search. Validated live on
2026-07-06 against the *Avatar: The Last Airbender – The Complete Series* discs
(hash `289B5883B75EDB95925573F3CF11F8DC` → `tmdb:246`, 19 mapped titles).

Where it lives:

- **`discdb.py`** (new, stdlib only): `content_hash()` (ContentHash port),
  `query_api()` (GraphQL lookup with a `%APPDATA%\Disk-Rip\discdb-cache`
  cache — positive results cached forever, misses expire after a week),
  `join_titles()` (maps their titles onto ours by `sourceFile`+`segmentMap`),
  and `identify()` (the high-level, never-raises entry point). Also runnable
  standalone for validation: `py discdb.py <drive-letter>` or
  `py discdb.py --hash <HASH>`.
- **`webapp.py`** `api_scan` → `_discdb_lookup()`: attaches a `discdb` block to
  the scan response (collapsing intro/no-intro duplicates to one title per
  episode, preferring our representative). `ui/index.html` consumes it —
  auto-selects the TMDB match, pre-assigns the board, and shows a "Matched by
  TheDiscDb" banner; the user still confirms and hits **Rip**.
- **`diskrip.py`** `main()`: on a hit, pre-selects the TMDB id and season and
  skips the search/disambiguation step.
- **`config.json`**: `discdb` (on/off, default true), `discdb_endpoint`,
  `discdb_timeout`. Every integration point is advisory — timeout, silent
  fallback to the normal flow, feature only ever *adds* signal.

Everything below is the **remaining work**. The strategic premise still holds:
TheDiscDb has the pieces the first report said not to rebuild (identity scheme,
curated data model, PR-based moderation, hosting, a browsable site) and none of
the pieces Disk-Rip is — they have **no ripper**. The projects are
complementary; the one gap they have is an automated, human-verified submission
pipeline from an actual ripper, which is exactly what the write path offers.

---

## TheDiscDb, verified (reference for the write path)

State of the project as inspected on 2026-07-05:

- **Structure**: the GitHub org has two repos. [`TheDiscDb/data`](https://github.com/TheDiscDb/data)
  (MIT, 1,484+ commits) holds all disc data as flat files plus the C# tooling;
  [`TheDiscDb/web`](https://github.com/TheDiscDb/web) (Apache-2.0) is the Blazor
  site + GraphQL API at `https://thediscdb.com/graphql/`. The git repo is
  canonical; the website/API is a derived, read-only view of it. **There is no
  write API — the GraphQL schema has zero mutations. Pull requests to
  `TheDiscDb/data` are the only door in.**
- **Scale**: ≈**1,476 movies/series**, ≈**1,747 releases** (+52 boxsets),
  ≈**4,433 discs** with content hashes. Series are the minority (≈262 release
  folders) — TV box sets are where coverage is thinnest and where Disk-Rip
  users would contribute the most value.
- **Data layout**: `data/{movie|series|sets}/<Title (Year)>/<release-slug>/`
  containing per release:
  - `release.json` — slug, UPC, ASIN, year, locale, region, dates, contributors
  - `discNN.txt` — **verbatim `makemkvcon -r info` robot output** (the same
    stream Disk-Rip already parses in `MakeMKV.scan()`,
    [diskrip.py:302](src/diskrip.py#L302))
  - `discNN.json` — structured disc record: `ContentHash`, and per title
    `SourceFile`, `SegmentMap`, `Duration`, `Size`, full `Tracks`, and the
    curated `Item` (`Type`/`Season`/`Episode`/chapters)
  - `discNN-summary.txt` — the human-authored mapping (MakeMKV title fields
    copy-pasted + `Type:`/`Season:`/`Episode:` lines) that their tooling
    compiles into `discNN.json`
  - `discNN.ref` — pointer used when the *same pressing* ships in several
    releases (401 of these confirm the "masterings get reused" model)
  - title-level `metadata.json` with `ExternalIds` (**TMDB, IMDB, TVDB**)
- **Tooling**: ImportBuddy, a C# interactive CLI in `tools/`, drives
  `makemkvcon`, hashes the disc, checks for existing entries via the API,
  and finalizes the folder for a PR.
- **Process**: clone → ImportBuddy import → hand-edit `discNN-summary.txt` →
  Finalize → **pull request**. Human review by maintainers is the quality gate.

### The ContentHash (implemented; write path reuses it)

`ContentHash` = uppercase-hex MD5 over the concatenated 8-byte little-endian
file sizes of every `BDMV\STREAM\*.m2ts` (Blu-ray) or `VIDEO_TS\*` (DVD) file,
sorted by name. Ported and validated in **`discdb.content_hash()`**. The write
path reuses it and additionally records the `HSH:index,name,creationTime,size`
lines their `discNN.txt` format expects.

---

## Write path: contribute when we matched manually  *(ON HOLD)*

The trigger is precisely the case the user asked for: **the lookup missed, the
human fixed the board by hand, the rip succeeded** — at that moment Disk-Rip
holds a mapping TheDiscDb lacks, verified by the person holding the physical
disc. There is no upload API, so contribution means producing their PR-able
folder. Tiered by friction:

### Tier 1 — "Export for TheDiscDb" (ship first)

After a confirmed rip of an unknown disc, write a ready-to-PR folder:

| File | Source in Disk-Rip today | Gap |
|---|---|---|
| `discNN.txt` | The raw scan lines from `MakeMKV._run()` — already the exact format ([diskrip.py:302](src/diskrip.py#L302)); currently discarded after parsing | Keep them; append the `HSH:index,name,creationTime,size` lines from the hash computation (`discdb` already has the sizes) |
| `discNN-summary.txt` | Everything per entry is in hand: title name (TMDB episode name from `episode_meta`), `Source file name` (`t.source`), `Duration`, `Chapters count`, `Segment map`, `File name` (`t.output_name`), `Type/Season/Episode` from the confirmed board | `Size:` display value — parse MakeMKV attr 10/11 (one line in `scan()`); mark the export's `Type` for non-episodes (`Extra`, `Trailer`) only if the user labeled them |
| `release.json` | Title/year/slug derivable; `DateAdded`, contributor name from config | UPC/ASIN unknown to a ripper — prompt (one optional text field; it's on the box in the user's hand) |
| `front.jpg` / `back.jpg` | Not available from the disc | Leave to the contributor/PR review; optional in practice |
| `metadata.json`, `tmdb.json`, `imdb.json` (new media items only) | We have the TMDB id and details already | Only needed when the *show/movie itself* is new to TheDiscDb; their ImportBuddy "Finalize" step generates/validates these — don't reimplement it, point the user at it |

The export lands in a local folder with a `README-SUBMIT.txt` explaining:
fork `TheDiscDb/data`, drop the folder under `data/series/<Title (Year)>/<slug>/`,
open the PR (or run ImportBuddy Finalize over it first). Zero coupling, works
today, and their maintainers review as they do every submission.

### Tier 2 — guided PR

Same export, plus automation of the GitHub mechanics via a user-supplied
token: fork, branch, commit the folder, open the PR with a templated body
("Submitted from Disk-Rip; user-confirmed mapping; MakeMKV x.y.z log
attached"). ~150 lines against the GitHub REST API, no infrastructure. The PR
is authored by *the user's* account — right for a contribution culture built on
human review and a `contributors.json`.

### Tier 3 — only with the maintainers' blessing

If volume ever justifies it: a relay bot that takes anonymous submissions and
opens PRs from a service account, or upstream work on ImportBuddy (an "import
from Disk-Rip export" mode — it's MIT, C#, actively maintained). **Do not build
this speculatively.** Open a discussion on their org (`web@thediscdb.com` /
GitHub Discussions) once Tier 1 exports exist and ask what pipeline they want.

### Quality bar for anything we send

- Only **human-confirmed** boards (never `--yes` heuristic output) — now
  enforced by someone else's review queue.
- Respect their curation conventions (their wiki's Summary File Format page):
  `Type:` ∈ `MainMovie|Episode|Extra|Trailer|DeletedScene`, episode ranges as
  `Episode: 5-6` for double-length titles, season-level extras allowed.
  Disk-Rip's board would need a small "extras labeling" affordance before
  exports include non-episode titles; v1 can export episodes only and leave
  extras unmapped, which their format permits.

---

## Remaining implementation plan

- **Phase 0 — validate the linchpin. DONE** (`py discdb.py`, live-verified).
- **Phase 1 — read path. DONE** (`discdb.py` + `api_scan`/CLI hooks + wizard
  pre-fill + banner + config keys).
- **Phase 2 — write path, Tier 1 (small-medium).** Retain raw scan lines;
  parse the size attribute; `--export-discdb` / "Export for TheDiscDb" button
  producing the PR-ready folder + instructions. Prompt for UPC (optional).
- **Phase 3 — outreach, then Tier 2/3.** Post in their GitHub Discussions with
  a sample export PR; add token-based one-click PRs if they're receptive; talk
  before building anything server-side. Also worth proposing upstream: their
  `GetDiscDetailByContentHash` query lacks `externalids` (their own client
  re-queries for it) — the live test confirmed the API returns it when asked,
  so Disk-Rip's single-query flow already works; a doc PR noting the pattern
  would help the next integrator.

---

## Risks and pitfalls (write path + operating the live read path)

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **API has no SLA/versioning** (hobby project, read-only GraphQL) | Medium | Advisory-only integration (already built): short timeouts, silent fallback, local result cache; an optional full hash-index sync (`GetAllDiscContentHash`) could make lookups survive outages |
| 2 | **Single-maintainer project** could stall | Medium | The data is a clonable MIT git repo — worst case, pin a clone and compute lookups locally from `discNN.json`; nothing is lost |
| 3 | **Hash reimplementation drift** (sort order, size encoding) | Handled | Ported in `discdb.content_hash()` and validated against the household's own Avatar discs (byte-for-byte match on the 2026-07-06 live test) |
| 4 | **Coverage gaps**, especially TV (≈262 series releases) | Certain, shrinking | The miss→contribute loop (write path) is the fix; thin TV coverage is precisely where Disk-Rip users add value |
| 5 | **PR latency** (human review, days) | Low | Local cache serves the household's own mapping immediately; the PR is for everyone else |
| 6 | **Their formats/API evolve** | Medium | Keep the GraphQL query minimal (fields verified); pin export formats to observed examples; a failing lookup is already non-fatal |
| 7 | Burned/recordable discs produce junk hashes | Low | Detect and skip contribution for non-pressed media; lookups simply miss |
| 8 | Politeness/rate limits (no published policy) | Low | One cached lookup per scan is negligible; identify with a UA string (done); ask maintainers before syncing the full index |
| 9 | Attribution/licensing | Low | Data repo is MIT — credit TheDiscDb in the README and in the UI banner on hits (banner done) |

---

## Bottom line

The read path is live: every disc in TheDiscDb's ~4,433 is now zero-effort to
identify, including re-rips, with a clean advisory-only fallback when a disc is
unknown or the API is down. What remains is the reciprocal half — producing
PR-ready submission folders from human-confirmed rips so Disk-Rip users can fill
TheDiscDb's thin TV coverage — deferred until we choose to build it. When we do,
Phase 2 (Tier 1 export) is a self-contained, low-risk change: retain the raw
scan lines we already have, format their summary file, and write a folder with
submission instructions.
