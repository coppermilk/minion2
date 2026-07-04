# Bananaland -- Software Architecture and Design Specification

Document class: architecture + design description with embedded requirements
and verification cross-references. Companion: [OPERATIONS.md](OPERATIONS.md)
(operations, failure modes, recovery). Status: final. Encoding: ASCII only
(self-applied law, section 4).

Standards note: the applicable airborne-software standard is **DO-178C**
(RTCA/EUROCAE, 2011); designations such as "DO-187C"/"DA-178C" do not exist.
This document borrows DO-178C, the NASA/JPL Power of 10, and SpaceX
flight-software practice as *forcing functions* for a non-certified personal
media system: they are applied where they buy traceability, boundedness, and
testability, and tiered by consequence (section 2) -- not performed as
ceremony.

## 1. System definition and the fixed contract

### 1.1 Mission statement

The system ingests media items (photos, videos, links) from two transports
(Telegram, watched folders), applies exactly one small transformation per item
per bot, and delivers the result to a defined location in a single directory
tree. Availability target: continuous unattended operation with supervised
restart; integrity target: no loss or overwrite of user media under any single
fault.

### 1.2 The media tree (the one invariant interface)

Every path derives from one root, `DRIVE`. The tree below is the system's only
fixed contract; all code, all environments (Windows mapped drive, Docker
`/data` bind-mount, Google Drive API) resolve to this same logical tree and the
software never branches on the host OS.

```
DRIVE/
  _inbox/                 ingest drop + the classified working week
                          (prim names, EXIF fandom, weekly tag)      [MEDIA]
  pictures/<Fandom>/      sorted library (shelved Mondays, untagged) [MEDIA]
  print/  print/_done/    print queue + archive                      [MEDIA]
  Scripts/                weekly document archive (done__<name>)     [MEDIA]
  bots/
    _data/
      regen/              model weights + _embeddings.npz            [CACHE]
      state/              per-bot <bot>.offset                       [STATE]
      logs/               <bot>.log                                  [TELEMETRY]
    <bot>/[done/]         per-bot work + archive directories         [MEDIA]
```

Data classification is normative, not advisory:

- **MEDIA** -- the system's purpose; protected by REQ-DATA-001/002.
- **STATE** -- durable; a restart depends on it; never deleted (REQ-DATA-003).
- **CACHE** -- disposable; rebuilds unattended; may be wiped at any idle moment.
- **TELEMETRY** -- append-only evidence for every disposition (REQ-OBS-001).

Shared-folder rule: `_inbox/` serves two readers conflict-free **by type,
enforced**: the sorter consumes only image extensions via the local mount; the
cloud bot consumes only Google Docs via the Drive API. Neither writes the
other's type.

## 2. Criticality tiers (DAL analog)

Each behaviour is assigned a tier by the worst credible outcome of its failure;
rigor is spent per tier. Nothing here is DAL A in the certification sense; the
worst credible outcome is irreversible loss of the user's own files.

| Tier | Worst credible outcome | Representative items | Rigor above baseline laws |
|------|------------------------|----------------------|---------------------------|
| CT-A | Irreversible media/state loss | offset persistence; two-sided quota; disposal ordering; collision-free naming; atomic writes | requirement + robustness (boundary + fault-injection) tests; second-pass review; no fallback of any kind |
| CT-B | Wrong output, hard to notice; privacy leak | sort placement; censor correctness (a miss leaks the hidden subject); SSRF guard; placement-mapping freshness after demote | requirement + positive and negative tests; decision coverage on the path |
| CT-C | Availability loss (wedge/stall) | step crash guard; download timeout; bounded queue; batch lock; supervised restart; extractor self-update | requirement + a fault-injection test proving survival |
| CT-D | Cosmetic | reply strings, log formatting | review only |
| CT-E | None (tooling) | tests, stubs, docs | structural laws only (lint + types) |

## 3. Requirements and bidirectional traceability

Every line of code traces up to a requirement; every requirement traces down to
a verification (review, static analysis, or test -- the DO-178C menu). Code
with no upstream requirement is deleted; a requirement with no downstream
verification does not ship. Verification names below are the test/rule
identifiers used in CI.

| Req | Statement | Tier | Design element (section) | Verified by |
|-----|-----------|------|--------------------------|-------------|
| REQ-KRN-001 | A raising Step yields FAILED and never terminates the daemon | CT-C | kernel crash guard (5) | test: step crash injection |
| REQ-KRN-002 | A non-DELIVERED envelope bypasses all later Steps | CT-B | kernel short-circuit (5) | test: short-circuit |
| REQ-KRN-003 | Source-to-belt buffering is bounded (backpressure) | CT-C | bounded queue bridge (5) | test: backpressure |
| REQ-KRN-004 | Disposal Sinks execute only after delivery is decided | CT-A | sink ordering (5) | test: failed job leaves source intact |
| REQ-KRN-005 | Folder emits a file only after its size is stable across one poll interval | CT-B | Folder stability guard (5) | test: growing file withheld, stable file emitted |
| REQ-DATA-001 | Output never overwrites an existing file | CT-A | files.next_free_path (6) | test: collision resolves to _2 |
| REQ-DATA-002 | File writes are atomic (no torn media) | CT-A | files atomic write (6) | test: interrupted-write |
| REQ-DATA-003 | The Telegram offset persists across restart | CT-A | STATE dir (1.2) | test: offset round-trip |
| REQ-SEC-001 | Fetches to loopback/link-local/RFC-1918/reserved hosts are rejected pre-connect | CT-B | fetch SSRF guard (6) | test: rejection table |
| REQ-RES-001 | Every download is wall-time bounded | CT-C | download_timeout_sec (7) | test: hung-extractor timeout |
| REQ-RES-002 | Disk quota is enforced before, and mid-stream for direct transfers | CT-A | quota_bytes (7) | test: quota boundary |
| REQ-RES-003 | A batch bot cannot overlap its own previous run | CT-C | per-bot lock (8) | test: second invocation exits |
| REQ-SORT-001 | Placement never uses a persisted layout: the fandom mapping is rebuilt from the live tree on every refresh; stored vectors are keyed by file identity only | CT-B | identity-keyed cache (9) | test: demote then re-place with zero recomputation |
| REQ-CFG-001 | A relative path override is rejected at load | CT-C | settings.load (7) | test: relative override raises |
| REQ-OBS-001 | Every non-DELIVERED disposition is logged with a stable reason code | CT-C | Verdict.reason (5) | review + test: log emitted |
| REQ-ARC-001 | No bot imports a sibling bot | CT-E | layout (6) | analysis: import-boundary rule |
| REQ-ARC-002 | No file outside adapters/ imports a vendor SDK | CT-E | adapter rule (6) | analysis: forbidden-import rule |
| REQ-DEG-001 | A token-less bot degrades to folder-only without code branches in the bot | CT-C | TgChannel no-op (5) | test: tokenless graph runs |
| REQ-PRT-001 | The print spooler command is a Settings value; no code branches on the host OS | CT-C | print spooler axis (7, 9) | test: argv assembled from Settings; analysis: no sys.platform outside adapters |
| REQ-DOCK-001 | A streaming bot with a configured watch dir serves both docks through one belt; each delivered result reaches the sink of its origin (tg -> chat, loc -> done dir) | CT-B | RouteOrigin sink (5) + watch axes (9) | test: merged graph routes both origins |
| REQ-CATCH-001 | catch never moves or deletes a file out of the watched folder; the library copy is a copy | CT-A | catch ClassifyCopy step (9) | test: original present and byte-identical after delivery |
| REQ-CATCH-002 | A file that fails classification is left untouched and logged with a stable reason; the belt continues | CT-B | catch step verdicts (9) | test: failing classifier yields FAILED, file intact, next file processed |

## 4. Design laws (CI-enforced; the design cannot drift)

Structural, discharged by analysis (ruff `select=ALL`, mypy strict):

- **<= 3 args per function** -> a frozen config object, never a parameter list.
- **McCabe <= 5**, line <= 79 -> one decision per function; helpers, not nesting.
- **ASCII only** (repo-wide test) -> `--`, `->`, straight quotes, `...`.
- One import per line, isort order, no unused, no `X as Y` aliases (canonical
  third-party excepted); single quotes; `dict[str, Any]` not bare `dict`.
  Target Python 3.14.

Semantic, discharged by review + the traceability of section 3:

- **No literals in decisions.** Every tunable is a `Settings` field (section 7).
- **No dead code, no silent fallback.** A failure becomes a `Disposition` plus a
  reason code, or a log line -- never a swallowed exception. Daemons may catch
  broadly but must then log (REQ-OBS-001).
- **Validate untrusted input explicitly** (raise or REJECTED; survives
  `python -O`); `assert` is reserved for developer-error invariants. No counted
  assert quota -- counted metrics breed filler.
- **One concept, one name; name the role** (`BlurPeople`), never the mechanism;
  adapter-facing names stay vendor-neutral (`PERSON_SEG_*`, never a model name).
- **Three kinds of file only** (section 6); each vendor behind exactly one
  adapter (REQ-ARC-002); bots never import siblings (REQ-ARC-001).

**Power of 10, rule-by-rule mapping** (the intent transfers from C exactly):

| # | Rule | Mechanism here |
|---|------|----------------|
| 1 | Simple control flow, no recursion | McCabe <= 5; recursion barred by review |
| 2 | All loops bounded | every work loop capped (section 7 budgets); the single sanctioned infinite loop is the kernel drain -- the top-level scheduler, named as the exception |
| 3 | Bounded memory | bounded queue, capped scans, LRU dedup, weights loaded once into persistent CACHE |
| 4 | Small functions | <= 3 args + McCabe <= 5 + 79 cols keep functions under a page |
| 5 | Assertions / checks | explicit boundary validation surviving -O; invariant asserts inside |
| 6 | Smallest scope | frozen dataclasses passed down; no module-level env reads; the only shared state is Settings-as-value and the thread-safe dedup/offset stores |
| 7 | Check returns, validate params | Disposition inspected by construction (kernel short-circuits); guard validates and sanitizes |
| 8 | Limited preprocessor (analog) | no import-time side effects; no dynamic import except the sanctioned lazy vendor loads |
| 9 | One level of indirection | exactly one adapter layer; no speculative Protocol port until a second concrete provider ships (then: a ~10-line factory, added that day) |
| 10 | Zero warnings, daily static analysis | ruff ALL + mypy strict + ASCII gate, green on main; every waiver written down and localized (section 12) |

## 5. The kernel (`kernel.py`) -- one file, one algebra

One data type flows, one operator composes, three stage kinds work. This file
defines the fault-containment regions and the single unbounded loop.

```python
Stream = Iterator['Envelope']

class Disposition(Enum):
    DELIVERED = 'delivered'   # result produced and handed to sinks
    SKIPPED   = 'skipped'     # nothing to act on; not an error
    REJECTED  = 'rejected'    # invalid/disallowed input; never retry
    FAILED    = 'failed'      # internal or transient; retry may help

@dataclass(frozen=True)
class Origin:
    source: str               # transport-neutral tag: 'tg' | 'loc'
    ref: str                  # opaque, source-defined; kernel never parses it

@dataclass(frozen=True)
class Job:
    src: Path; dest: Path; stem: str; origin: Origin

@dataclass(frozen=True)
class Verdict:
    disposition: Disposition
    reason: str = ''          # stable code, 1:1 with the OPERATIONS failure table
    result: Path | None = None
    reply: str = ''
    dest: Path | None = None  # set when a step relocates the job

@dataclass(frozen=True)
class Envelope:
    job: Job; verdict: Verdict | None = None

class Stage(ABC):
    def __call__(self, up: Stream) -> Stream: ...
    def __rshift__(self, nxt): return _Chain(self, nxt)   # a >> b : then
    def __or__(self, other):   return _Merge(self, other) # a | b  : two docks, one belt
```

- **Source** (abstract `produce(emit)`): one daemon thread each; a blocking
  loop (long-poll, folder watch) is bridged into a lazy stream through a
  **bounded** queue (REQ-KRN-003). Concrete: `Folder`, `TgLinks`,
  `TgDocuments`, plus `SeenPaths` (LRU-capped dedup).
- **Step** (abstract `process(job) -> Verdict`): short-circuits any envelope
  not DELIVERED (REQ-KRN-002); **crash-guarded** -- a raising step logs and
  yields FAILED, never killing the daemon (REQ-KRN-001, the primary
  containment region). It advances the frozen job by constructing its
  successor: `src <- verdict.result`; `dest <- verdict.dest` when set (this one
  line lets a classifier choose a folder without mutating frozen state).
- **Sink** (`handle(item)`, re-emitting): `Reply`, `SendResult`, `ArchiveTo`,
  `DisposeSource`. Disposal is composed last, so a failed delivery leaves the
  source untouched (REQ-KRN-004).
- **TgChannel**: a bot's Telegram identity; a no-op without a token, so loss of
  the transport degrades the bot to folder-only with zero caller branching
  (REQ-DEG-001).
- **run(name, graph)**: drains the stream; a daemon source never ends (drain
  forever), a batch source ends (drain once, exit). Clean stop -> 0; fatal init
  -> non-zero. Logging (console + `logs/<name>.log`) lives here: telemetry is a
  kernel service, not an optional import.

Concurrency decision: threads, not asyncio -- the real inputs are blocking
loops; one thread per source into a bounded queue is the smallest correct
bridge and keeps every Step synchronous and trivially testable ("test like you
fly": the tested path is the flight path).

## 6. Code organization -- three kinds of file (a closed set)

Every file in the codebase is exactly one of: **the kernel**, **an adapter**,
or **a bot**. The set is closed; "where does this go?" has no third answer.
Bot-specific logic lives with its bot (e.g. reply-parsing and placement live
inside `sort/`), never in a shared corner.

```
minion_core/
  kernel.py       the belt (section 5) + logging
  settings.py     Settings + load(env) (section 7)
  prompts/        LLM prompt texts as package-data + load_prompt
  adapters/       one file per external system; vendors import ONLY here
                  (each vendor's sanctioned sites are enumerated in the
                  structural analysis; requests serves two boundaries)
    tg.py         Bot API, long-poll, media receive            (requests)
    fetch.py      link download: yt-dlp + timeout + quota + SSRF guard (yt-dlp)
    llm.py        JSON image classification + background restore (google-genai)
    scripts.py    weekly Google Docs script text for the classify hint (requests)
    vision.py     embeddings/nearest-fandom + person masks + faces (torch et al.)
    video.py      probe + frame extraction                     (ffmpeg/ffprobe)
    files.py      stem/usd_prim/next_free_path, EXIF, validate/sanitize, dedup, atomic, lock (Pillow, piexif)
minions/
  sort/ censor_blur/ censor_black/ restore/ fetch/ frames/ inbox/
  catch/ week-clean/ print/ _template/
```

Import direction is strictly downward: bots -> adapters/kernel/settings;
adapters -> kernel/settings; kernel -> stdlib. Heavy dependencies (torch,
transformers, facenet, google) are imported lazily inside the functions that
use them, so a bot needing no ML -- and the test suite -- never loads them
(Power-of-10 rule 3). Swapping a vendor rewrites one adapter and touches no
bot (REQ-ARC-002).

## 7. Configuration -- one value, built once (`settings.py`)

No module-level environment reads; no globals. One frozen `Settings`,
constructed once in `main()` via `load(env: Mapping[str, str])` and passed down
as the single config argument the 3-args law expects.

```python
@dataclass(frozen=True)
class Settings:
    drive: Path
    download_timeout_sec: int
    quota_bytes: int
    max_embedding_scan: int
    seen_paths_max: int
    demote_min_count: int
    ytdlp_format: str
    ytdlp_container: str
    ytdlp_player_clients: tuple[str, ...]
    source_dirs: tuple[Path, ...]
    print_spooler: tuple[str, ...]  # argv prefix; the PDF is appended
    print_timeout_sec: int
    censor_blur_watch: Path | None  # second dock; None disables
    censor_black_watch: Path | None # second dock; None disables
    restore_watch: Path | None      # second dock; None disables
    frames_watch: Path | None       # second dock; None disables
    catch_dir: Path | None          # catch bot source; None disables
    # derived, never overridable separately:
    @property
    def inbox(self):    return self.drive / '_inbox'
    @property
    def pictures(self): return self.drive / 'pictures'
    @property
    def state(self):    return self.drive / 'bots' / '_data' / 'state'
    @property
    def regen(self):    return self.drive / 'bots' / '_data' / 'regen'
```

`load` coerces one line per field and **raises on any relative path override**
(REQ-CFG-001; a raise, not an assert -- it survives `python -O`). Absolute is
tested against BOTH path flavors (POSIX and Windows), never the host OS, so a
single `.env` is shared verbatim across the NAS and the Windows box: a Windows
`CATCH_DIR` reads as absolute on Linux (the containers that never use it just
ignore it) and a POSIX `DRIVE` reads as absolute on Windows; a path absolute
on neither flavor is genuinely relative and still refused. Precedence is
the mapping you pass: production passes `os.environ` (so a container's
`DRIVE=/data` wins); a test passes `{'DRIVE': str(tmp_path)}` -- hence no
import-order rituals and nothing to leak between tests. Secrets live in a
git-ignored `.env`.

## 8. Fault containment, degradation, supervision

Partition model (the IMA idea: a fault is confined to its region and converted
to a Disposition or a log line at the boundary; it never becomes a silent
success and never crosses a partition):

- one **thread** per Source (a jammed dock cannot stall another);
- one **crash guard** per Step (REQ-KRN-001);
- one **process/container** per bot;
- one **lock** per batch bot (REQ-RES-003: a slow run never overlaps itself; a
  tight cron schedule is therefore safe by construction).

Degradation and supervision (SpaceX practice): loss of the Telegram transport
degrades to folder-only (REQ-DEG-001); the watchdog is Docker
`restart: unless-stopped` for daemons and foreground `cron -f` for batch
containers; a crash is a supervised restart, safe because all durable state is
on disk (STATE, section 1.2). Liveness signal: container up **and** offset
advancing.

## 9. The bots (behavioural requirements per unit)

Consolidation rule (normative): two bots with the same **graph shape** are one
bot with a Settings value; two bots whose Steps differ stay separate. This rule
eliminates duplicates by construction.

Recorded waiver (BLUEPRINT 12): the censor family (censor-blur,
censor-black, restore) and fan-save are deliberately split against
this rule -- one Telegram identity per behaviour, chosen by the
operator. The behaviours diverge on purpose: censor-black hides
**faces** (vision.HideFaces), censor-blur blurs the **person
silhouette** (vision.BlurContour, Mask R-CNN segmentation), restore
blurs **person boxes** for the LLM to erase (vision.HidePersonBoxes,
llm.RestoreBackground). Each is a thin Step over the shared adapters,
so only graph assembly repeats.

Telegram contract: files cross Telegram as **documents only, both
directions** -- compressed photo/video payloads are refused with a
logged reason (not_a_document) and results go back via sendDocument,
so originals are never recompressed. A message that produces no work
(plain text, or a refused compressed payload) gets a one-line usage
reply plus the documents-only reminder.

Output-folder convention: every archiving bot (frames, censor-blur/
black, restore, fan-save) files its result into
`bots/<bot>/done/<MMDD> <name>/` and keeps the consumed original in
that folder's `_done/` (files.Shelve).

| Bot | Kind | Behaviour | Config axis |
|-----|------|-----------|-------------|
| inbox | streaming | Telegram file -> `_inbox/` | - |
| fetch | streaming | link -> video | sink: chat / fan queue |
| fan-save | streaming | link -> video parked in `done/<MMDD> <title>/` (link in `_done/`) | - |
| frames | streaming | video/link -> every 5th frame into `done/<MMDD> <clip>/`, clip in `_done/`, summary to chat | `frames_watch` dock |
| censor-blur | streaming | photo -> person silhouette blurred -> chat + done folder | `censor_blur_watch` dock |
| censor-black | streaming | photo -> faces blacked out -> chat + done folder | `censor_black_watch` dock |
| restore | streaming | photo -> people blurred -> LLM repaints scene (`_s1`/`_s2`) -> chat + done folder | `restore_watch` dock |
| sort | batch | images classified in place (prim name + EXIF fandom); the week waits in `_inbox/` | `source_dirs`: `_inbox/` or `Downloads/`; `SORT_WATCH`: streaming trigger, instant classification |
| catch | streaming | new Downloads image -> prim-named copy in `pictures/<Fandom>/` (same Gemini verdict as sort); the original is renamed in place, never moved | `catch_dir` |
| week-clean | batch | Monday, purely mechanical: strip the weekly tag and shelve each classified image from `_inbox/` into `pictures/<Fandom>/` per its EXIF fandom; unclassified files stay for retry | - |
| print | streaming | PDF in `print/` -> spooler -> `print/_done/` | `print_spooler`: lp / SumatraPDF argv |
| kindle | outlier | Google Doc -> PDF -> `print/`; weekly archive | Apps Script (off-kernel, declared, not disguised) |

Two honest kinds: **streaming** bots are per-file belts drained forever;
**batch** bots operate on a whole folder, orchestrate adapters directly (the
belt algebra would be a poor fit), run one-shot under cron, and hold the lock
of section 8.

Sort is three named passes: **Classify** (one Gemini JSON verdict per image
decides BOTH the OpenUSD prim filename and the fandom; the weekly script
hint from `adapters/scripts.py` rides into the prompt. The image is renamed
IN PLACE, the fandom recorded in EXIF (`files.tag_fandom`) and the weekly
tag applied -- the working week stays visible in `_inbox/`. When the model
punts (Unknown), CLIP picks the nearest library fandom in the SAME run:
everything is decided during the week, so the Monday week-clean run is
purely mechanical) -> **Demote** (fandoms under `demote_min_count` ->
`Unknown/`; vectors survive -- the cache is identity-keyed and the fandom
mapping is never persisted, REQ-SORT-001) -> **Re-place** (vision re-matches
`Unknown/` against the live layout at zero recompute cost; Gemini never
sees an image twice).

Library naming: files under `pictures/` are valid OpenUSD prim identifiers
(UpperCamelCase, letters+digits, e.g. `FgSnapeOfficeAngry.jpg`); collisions
take a bare digit suffix (`FgSnapeOfficeAngry2.jpg`, `files.next_free_prim`).
The `MMDD_<source>_<name>` stem remains the transport-bot convention only.
A `censored=true` verdict (bars/mosaic/blur/stickers) changes nothing about
placement; it is recorded in the log line (operator decision).

## 10. Resource budgets (every resource has a declared, enforced bound)

| Resource | Bound | Enforcement point | Tier |
|----------|-------|-------------------|------|
| Source-to-belt buffer | fixed queue depth | kernel bridge | CT-C |
| Download wall-time | `download_timeout_sec` | fetch | CT-C |
| Disk | `quota_bytes`, pre-check + mid-stream for direct transfers | fetch + files | CT-A |
| Embedding scan | `max_embedding_scan` | vision | CT-C |
| Dedup memory | `seen_paths_max` (LRU) | kernel | CT-C |
| Model memory | CPU-only torch; weights loaded once into CACHE | build + lazy import | CT-C |
| Top-level loop | intentionally unbounded (daemon drain) | kernel `run` -- the single named exception | - |

## 11. The non-deterministic frontier

The pipeline is deterministic given its inputs except at four named, isolated,
logged boundaries, so any surprise is attributable from telemetry alone:

1. **Model inference** (llm, vision) -- behind adapters; reply parsing lives
   with `sort/`, so one reply maps to exactly one placement.
2. **Network** (fetch, Drive, weekly script fetch) -- bounded by timeout +
   quota; the fetch host set is made deterministic by the SSRF guard
   (REQ-SEC-001); the script fetch talks to one fixed Docs export host and
   degrades to an empty hint on any failure.
3. **Model-weights version** -- pinned in CACHE; a weights change is a
   configuration change (section 12), never drift.
4. **Wall clock** -- read only by cron; batch bots are pure functions of the
   folder state at start.

## 12. Configuration management

- **Single source of truth**: tunables in `settings.py`, prompts in `prompts/`,
  naming in `files.stem`, all paths from `DRIVE`. One place per fact.
- **Reproducible build**: pinned requirements; CPU-only torch from the PyTorch
  index; a wheel build in CI proves `pip install .`.
- **Pinned non-determinism**: weights and the yt-dlp binary/version are managed
  artifacts; updating either is a logged, deliberate change.
- **Waiver register**: every lint/type ignore and every naming deviation is
  documented and scoped to the smallest unit. A waiver without a written reason
  is a defect.

## 13. Verification strategy

Method matches tier (section 2) and the DO-178C menu (review / analysis /
test); the suite is the flight path -- bots are wired through the same kernel
with doubles at the adapter boundary only.

- **Requirements-based tests**: one named verification per row of section 3,
  walkable in both directions.
- **Robustness set** (CT-A/B): quota at the edge, name collision, interrupted
  write, SSRF rejection table, hung extractor, stale-vector rebuild, offset
  round-trip, tokenless degradation, batch-lock exclusion.
- **Coverage goal**: statement + decision on the kernel and every CT-A/CT-B
  path (McCabe <= 5 keeps decision counts small; MC/DC remains cheap if ever
  demanded).
- **Hermeticity**: pytest offline -- tmp_path Settings, generated images, no
  network, no models (lazy imports make torch optional).
- **CI gates, all green on main**: ruff check (ALL) + format check; mypy strict
  on `minion_core` and every minion; pytest; wheel build; ASCII test.

## 14. Environments and deployment

| Aspect | Windows | NAS (Docker) | Cloud |
|--------|---------|--------------|-------|
| Bots | print (SumatraPDF axis), catch (Downloads watcher) | streaming bots, sort (watch daemon), week-clean | kindle |
| `DRIVE` | `.env` (e.g. `C:\Users\a\My Drive`) | compose `DRIVE=/data` | Apps Script `Config.gs` |
| Filesystem | Drive for Desktop (mapped) | `${DRIVE_NAS}` bind at `/data` | Drive API |
| Launch | Task Scheduler, bare Python | `docker compose up -d` | Apps Script trigger |

One image, N containers on one mount (`x-minion` anchor: build, `env_file`,
`environment: DRIVE=/data TORCH_HOME=<CACHE>/torch`, volumes, `restart:
unless-stopped`). Batch containers run `cron -f` foreground (the supervisor);
link bots run `yt-dlp -U` before start so extractors are fresh on every
restart.

## 15. Build-from-zero procedure

1. `pyproject.toml`: metadata, package discovery, the law block of section 4,
   package-data (`py.typed`, `prompts/*.md`).
2. `kernel.py` per section 5, with tests discharging REQ-KRN-001..004.
3. `settings.py` per section 7, with the REQ-CFG-001 test.
4. Adapters per section 6, each with its lazy vendor import and its boundary
   tests (REQ-SEC-001, REQ-RES-001/002, REQ-DATA-001/002).
5. Bots: copy `_template`, add a Step, assemble one graph (section 9 rules).
6. `docker/`, `docker-compose.yml`, `deploy/crontab.example` per section 14.
7. Create the media tree of section 1.2 under `DRIVE`; verify the traceability
   matrix (section 3) resolves requirement <-> verification in both directions;
   release when CI is green.
