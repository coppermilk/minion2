# Orchestration strategy: our own visual pipeline, n8n only as glue

Document class: architecture decision record (ADR) with a staged roadmap.
Companions: [BLUEPRINT.md](BLUEPRINT.md) (design + requirements),
[OPERATIONS.md](OPERATIONS.md) (operations + recovery). Encoding: ASCII only
(BLUEPRINT section 4). Status: decision recorded; implementation deferred to
its own change (this document ships no code).

## 1. Context and the decision

We evaluated moving the "watcher" side of the bots -- the event sources
(Telegram, watched folders, cron) -- onto **n8n**, a self-hosted workflow
tool, so we would stop re-writing polling/reconnect/offset glue for every new
source. The attraction is real: n8n already ships Telegram, Google Drive,
IMAP, Dropbox, RSS, Webhook, Cron and hundreds of other triggers, with retry
and branching, all tested by many people.

Two forces push the other way:

1. **We must be able to detach at any moment and sell this as our own
   product.** No business logic may live inside a third-party tool.
2. **We must not duplicate what we already have.**

The second force is decisive once you look at the kernel. The finding below
turns "build our own n8n" from a from-scratch project into a thin layer on top
of an engine we already own.

### 1.1 The finding: the kernel is already a node-graph engine

`minion_core/kernel.py` already is a dataflow node system. This is not a
metaphor; the vocabulary maps one to one:

| Node-graph concept | Kernel construct | Examples in the tree |
|--------------------|------------------|----------------------|
| Trigger / source node | `Source` (`kernel.py`) | `Folder`, `TgAny` / `TgMedia` / `TgLinks` (`adapters/tg.py`) |
| Processing node (our IP) | `Step` (`kernel.py`) | `ExtractFrames` (`minions/frames`), `HideFaces` / `BlurContour` (`adapters/vision.py`), `RestoreBackground` (`adapters/llm.py`), `SortTrigger`, `ClassifyCopy`, `FetchLink`, `PrintPdf` |
| Delivery / disposal node | `Sink` (`kernel.py`) | `Reply`, `SendResult`, `ArchiveTo`, `DisposeSource`, `RouteOrigin` |
| Wiring: sequential | `a >> b` (`Stage.__rshift__`) | every `minions/*/main.py` `build()` |
| Wiring: two docks, one belt | `a \| b` (`Stage.__or__`) | `merge_watch` in `minions/frames` |

Every `build()` function in `minions/*/main.py` is a hand-drawn graph. Every
`Step` already exposes the same pure contract: `process(Job) -> Verdict`.

So the thing n8n is admired for -- reusable triggers, "stop writing the boring
part", a graph of nodes -- is roughly 80% present in ~630 lines of kernel. The
gaps versus n8n are only three: (a) fewer trigger types, (b) the graph is
Python code rather than data, (c) no visual canvas.

### 1.2 Decision

- **Do NOT build a general-purpose n8n clone.** Matching n8n's breadth
  (hundreds of integrations, a credential vault, an editor, a scheduler, a
  retry engine) is years of work and is exactly the duplication we want to
  avoid. Integration breadth is n8n's moat; we neither can nor should chase it.
- **Do finish three thin layers on top of our own kernel** (section 3), each
  independently useful, each preserving the detach guarantee.
- **Treat n8n, if used at all, as an optional and disposable trigger farm**
  (section 4). No logic ever moves into it.

## 2. Notes on n8n (for the record)

- **Cost.** Self-hosted n8n is free forever (fair-code, Sustainable Use
  License). What costs money is n8n Cloud (hosting) and Enterprise features
  (SSO, RBAC, versioning). The impression of "lots of paid API" comes from the
  integration nodes: the OpenAI node costs because the OpenAI API costs, not
  because n8n does. The real cost to us is operational: a Node.js + Postgres +
  editor stack living next to a tight, offline-first, DO-178C-flavored system.
- **"It could not reach the internet on first install."** This is the classic
  n8n trigger/host misconfiguration. Webhook- and OAuth-based triggers need
  `N8N_HOST`, `N8N_PROTOCOL` and `WEBHOOK_URL` set correctly AND a reachable
  **inbound** URL (a tunnel or reverse proxy). Behind NAT on a NAS with no port
  forward, webhook triggers (Telegram webhook, Google Drive push, the Webhook
  node) silently never fire. **Polling** triggers (Cron, IMAP, RSS) work
  offline. This is fixable, but it fights the project's ethos: today
  `docker-compose.yml` publishes no host port and takes no inbound connection
  at all.
- **Consequence for section 4:** if we ever bridge to n8n, use polling
  triggers only, so we never need the inbound webhook URL that failed on first
  install.

## 3. Roadmap: three layers on our own engine

Each phase is shippable on its own and keeps the detach guarantee intact.

### Phase 0 -- Expose the Steps as services (orchestrator-agnostic)

This is the "build the services first, regardless of orchestrator" step. Each
`Step` already has the uniform contract `process(Job) -> Verdict`. Add ONE thin
dispatcher that can invoke any registered step as a service:
`run(step_name, input_path) -> (result_path, verdict)`.

- The IP does **not** move or get rewritten; we add a single entry point in
  front of code that already has a single interface.
- Transport: start with a CLI (`python -m minion_core.service <step> <path>`);
  later, optionally, a thin HTTP surface bound to **loopback / the compose
  network only** -- never a host port, so offline-first is preserved.
- New modules under `minion_core/` (working names `service.py` +
  `registry.py`), obeying every design law in BLUEPRINT section 4 and the
  import-boundary rule REQ-ARC-001 (the registry lives in `minion_core`; bots
  register into it and never import one another).

Result: the processing services become callable by **any** orchestrator -- the
current Python graphs, a future canvas, or n8n over local HTTP -- without
touching the IP. This is the infrastructure-vs-business-logic split.

### Phase 1 -- Make the graph data, not code (the seed of visual nodes)

Today each bot's wiring lives in a Python `build()`. Add a declarative graph
loader: a small YAML/JSON file naming sources, steps and sinks and their
`>>` / `\|` wiring, assembled into the **existing** `Stage` objects via the
Phase 0 registry. We write **no new execution engine** -- the kernel already
runs graphs (`run(name, graph)`).

- Highest-leverage move: once the graph is data it is inspectable, diffable in
  git, renderable as a canvas, and gives the property "a new module is a new
  node, not a new watcher".
- Adding Dropbox becomes "register a `DropboxSource`, reference it in YAML" --
  zero new pipeline code.
- Migrate incrementally: start with one or two bots (`frames`, `_template`) as
  the pilot; leave the rest on Python `build()`. No big-bang rewrite.

### Phase 2 -- A thin read-only visual viewer (our "mini n8n")

A tiny static web page that renders the graph YAML as a node diagram (nodes =
sources/steps/sinks, edges = wiring) plus live status read from the existing
per-bot logs (TELEMETRY is already structured with stable reason codes,
REQ-OBS-001).

- **Read-only first:** 90% of the perceived value of n8n's canvas (see the
  pipeline, see what is flowing, see failures) at ~1% of the code, and it is
  entirely ours and sellable.
- An interactive editor (drag to rewire, write the YAML back) is a
  clearly-scoped later step, taken only if the value proves out.

## 4. Optional bridge to n8n (honors the detach guarantee)

If some exotic source is genuinely tedious to write natively (say Gmail,
Discord, OneDrive), let an n8n **polling** trigger call the Phase 0 service
over local HTTP and hand off. n8n then holds only "on event X, POST to my
service" -- never a line of our logic. Pulling n8n out on any day means
replacing that one trigger with a native `Source`; nothing else changes. Keep
to polling triggers so no inbound webhook URL is ever required (section 2).

## 5. What stays in Python vs what the orchestrator owns

| Concern | Owner | Rationale |
|---------|-------|-----------|
| Media transforms (frames, blur, restore, sort, classify, props, print, fetch) | **Python `Step`s (our IP)** | The value nobody else will write; the thing we sell |
| The graph shape (which node feeds which) | **Our YAML + kernel** (Phase 1) | Data we own; renderable; diffable |
| Delivery / disposal / archive | **Python `Sink`s** | Tied to the media-tree contract (BLUEPRINT 1.2) |
| Native triggers (Telegram, local folder) | **Python `Source`s** | Already written, offline, no inbound |
| Exotic triggers we choose not to write | **n8n polling trigger (optional)** | Disposable convenience, no logic inside |
| Visual view of the pipeline + status | **Our read-only viewer** (Phase 2) | Ours and sellable; reads existing logs |

Everything in the "Python" rows is the "own solution": Steps + kernel + graph
format + viewer, all in this repository under our license. Detachment is
therefore structural, not aspirational -- at no layer does business logic live
in a third-party tool.

## 6. Invariants any implementation must not break

Carried from BLUEPRINT so a future implementation does not drift:

- **Style laws (BLUEPRINT 4):** ASCII only; <= 3 args per function (use a
  frozen config object); McCabe <= 5; line <= 79; ruff `select=ALL`; mypy
  strict; target Python 3.14. New modules (`service.py`, `registry.py`,
  `graph.py`) obey these exactly.
- **REQ-ARC-001:** no bot imports a sibling bot. The step registry lives in
  `minion_core`; bots register into it rather than importing each other.
- **REQ-ARC-002:** no file outside `adapters/` imports a vendor SDK. New
  triggers (Dropbox, Drive) are `Source` classes and their SDK use stays in
  `adapters/`.
- **Offline-first, no inbound:** any HTTP surface binds to loopback or the
  compose network only; no host port is published (as in today's
  `docker-compose.yml`).
- **Kernel is not modified:** we build on top of `Stage` / `Source` / `Step` /
  `Sink` / `run`, never fork them.

## 7. Verification (for the implementation phases, not this document)

- **Phase 0:** a unit test that `run('frames', video)` yields the same
  `Verdict` as calling `ExtractFrames(...).process(job)` directly (golden
  comparison); a CLI smoke run on a test file.
- **Phase 1:** a test that the pilot bot's YAML assembles into the same graph
  and produces the same result as its current `build()` (old-path vs new-path
  equivalence).
- **Gate:** `ruff check` (select=ALL), `ruff format --check`, `mypy --strict`,
  and the existing `pytest` suite (including the ASCII gate and the
  import-boundary analysis) all stay green.
