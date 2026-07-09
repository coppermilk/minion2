# Platform architecture: microservices, visual graph, one core many skins

Document class: architecture decision record (ADR) with a staged roadmap.
Companions: [ORCHESTRATION.md](ORCHESTRATION.md) (why our own visual pipeline,
n8n as glue), [BLUEPRINT.md](BLUEPRINT.md) (kernel design + requirements),
[OPERATIONS.md](OPERATIONS.md). Encoding: ASCII only (BLUEPRINT section 4).
Status: decisions recorded; Phases 0-4 landed (1.5 event taps; 2 service skins;
3 orchestrator; 4 multi-tenant API in `services/api.py`); Phases 5-6 deferred.

## 1. Context

ORCHESTRATION.md established the direction: our own visual pipeline over our own
kernel, n8n only as optional glue. Phase 0 (the service seam,
`minion_core/service.py`) and Phase 1 (graph as data, `minion_core/graph.py`)
have landed. New requirements now widen the target from a read-only viewer to a
multi-user platform:

- multi-user / multi-tenant web platform, designed correctly from the start;
- a React Flow (reactflow.dev) canvas -- a "simple constructor from ready
  modules";
- **each service is its own Docker container with an API**, reachable
  independently by our own orchestrator AND by n8n, each on its own port;
- expose services over **MCP as well as HTTP**, so agents (Claude, ...) call the
  same services as tools;
- resource metering in Resource Units (RU): Compute, Memory, Storage, Network;
- real-time: items animate along the graph, interactions are visible;
- per-request processing time surfaced to the user (explicitly last).

The load-bearing fact from Phase 0/1 is unchanged: each `Step` has one contract
(`process(Job) -> Verdict`), `invoke` (`minion_core/service.py`) is the single
seam that runs any Step, and a belt is already expressible as data
(`minions/frames/graph.json`). The platform is thin layers over that seam, not a
rewrite.

## 2. Decisions (this session)

| Topic | Decision | Rationale |
|-------|----------|-----------|
| Protocol facade | **HTTP/OpenAPI + MCP, both** over one `invoke` core | one core, two thin skins; n8n/orchestrator use HTTP, agents use MCP; zero duplication |
| Data plane | **Object store, S3 API** (MinIO self-hosted for offline/NAS + dev; S3-compatible in cloud) | one code path; self-hosted MinIO keeps the offline + detach guarantee; per-tenant buckets = isolation + egress metering |
| Tenancy / billing | **Schema now, enforce progressively** | tenant/usage schema and metric capture land early (cheap, at the dispatcher); full RU billing and per-request time-to-user land last |
| Backend | **FastAPI (Python)** | reuses the Phase 0 catalog directly; gives OpenAPI (n8n) and MCP (Python MCP SDK) over the same Steps; one language with the IP |
| Canvas | **React Flow** | nodes/edges map 1:1 to `graph.json`; palette from the catalog |

## 3. Core principle: one core, many skins, two run modes

- **Service core** = `invoke(step, call)` over the catalog (Phase 0). The IP
  (Steps) is never moved or rewritten.
- **Protocol skins** are thin wrappers over that core. One generic wrapper
  image, parameterized by `STEP=frames`, yields N service containers with no
  per-service code:
  - **HTTP/OpenAPI**: `POST /run {input_ref, params} -> {output_ref, verdict,
    ms, usage}`, plus `GET /openapi.json`, `GET /healthz`. Consumed by our
    orchestrator and by n8n's HTTP Request node -- each on its own port.
  - **MCP**: the same Step exposed as an MCP tool `run(...)`. Consumed by Claude
    and other agents.
- **Two run modes over one `graph.json` (Phase 1):**
  - **Mode A -- embedded / offline**: the in-process belt (`kernel.run` + the
    Phase 1 loader). One node, no network. This is what exists today; it is not
    broken by any later phase.
  - **Mode B -- distributed / cloud**: an orchestrator service walks the
    `graph.json`, calls each node's service over HTTP/MCP, moves payloads by
    object-store reference, emits events, and meters usage. Multi-tenant.
  - The Step code is identical in both modes; only the runner differs. This is
    what keeps the detach/offline guarantee structural.

```
                     one graph.json (Phase 1)
                    /                         \
       Mode A: in-process belt         Mode B: orchestrator
       (kernel.run, offline)           walks graph -> HTTP/MCP calls
                    \                         /
                     one Step core: invoke() -- the IP
                    /            |            \
             HTTP/OpenAPI       MCP        (embedded call)
            (n8n, orchestrator) (agents)
```

## 4. Data plane: object store (S3 API) from day one

- A `Store` abstraction with an S3-compatible backend: MinIO self-hosted
  (offline/NAS and dev), AWS S3 / compatible in cloud -- one code path.
- Services are **stateless**: receive `input_ref` (bucket/key), fetch to temp,
  process, put `output_ref`, return the ref. In Mode B nothing relies on a
  shared POSIX volume.
- Per-tenant bucket/prefix + signed URLs give both isolation and the natural
  network-egress metering point.
- Mode A (offline embedded) may still use the kernel's local-FS belt; running
  MinIO on the NAS unifies both modes on one plane when desired.

## 5. Real-time: event bus -> SSE/WebSocket -> React Flow

- Event schema: `{run_id, tenant, node_id, phase: entered|left|verdict, ts, ms,
  usage, reason}`. `reason` reuses the kernel's stable codes (REQ-OBS-001).
- Emitters: the orchestrator emits per node transition in Mode B; the Phase 1
  loader can wrap each stage to emit the same events in Mode A, so animation
  works offline too (Phase 1.5).
- Bus: Redis Streams / NATS in cloud; an in-process channel on a single node.
  The API bridges to the browser via SSE or WebSocket.
- React Flow: nodes come from `graph.json`; on `entered/left` a token animates
  along the edge; a node colors on its `verdict`; `ms`/usage show on hover. This
  is the "items running along the graph".

## 6. Metering / Resource Units (schema now, enforce later)

- The dispatcher `invoke` is the single chokepoint: wrap each Step run with a
  timer plus the container's declared resource envelope (vCPU, GB RAM from
  limits) and emit a `Usage` record `{run, node, tenant, ms, vcpu_s, gb_s,
  bytes_in, bytes_out}`.
- RU model (as specified):

  | Resource | Unit | Source |
  |----------|------|--------|
  | Compute | vCPU-hour | vCPU x wall-time (from `invoke`) |
  | Memory | GB-hour | RAM x wall-time (from `invoke`) |
  | Storage | GB-month | tenant bucket size, sampled |
  | Network | GB egress | `Store` transfer counters |

  `RU = C_cpu*(vCPU.h) + C_ram*(GB.h) + C_disk*(GB.month) + C_net*(GB)`. Internal
  tariffs `C_x` are configurable and decoupled from public prices.
- Stored in a Postgres `usage` table (schema defined now). Billing dashboards and
  the per-request time shown to the end user are the last phase.

## 7. Multi-tenant platform API (FastAPI): schema now, enforce progressively

- Owns users/orgs (tenants), graphs (`graph.json` versions), runs, usage, and
  the service catalog. Postgres. Auth (OIDC/JWT), per-tenant quotas, signed
  object-store refs.
- Endpoints: `GET /catalog` (the React Flow palette), `/graphs` CRUD, `/runs`
  (start/status), `/events` (SSE), `/usage`.
- The schema is multi-tenant from day one; isolation, quotas, and billing are
  switched on in stages.

## 8. n8n coexistence (the detach guarantee, now at the API level)

Because every service publishes OpenAPI (and MCP), n8n's HTTP Request node (or a
generated node) or its MCP Client node calls the same services, on their own
ports, independently. n8n holds only wiring, never IP -- rip it out any day and
replace a trigger with a native `Source`. This is the ORCHESTRATION.md guarantee,
now expressed at the service-API boundary instead of only the trigger boundary.

## 9. The austerity boundary (critical)

Two tiers with different rules; they must not blur:

- **Kernel + Steps (the IP)** -- `minion_core/`, `minions/*/main.py`. The
  BLUEPRINT laws apply verbatim: ASCII only, stdlib-only kernel, `ruff
  select=ALL`, `mypy --strict`, McCabe <= 5, <= 3 args, import direction
  (REQ-ARC-001/002). Untouched by the platform. Stays embeddable and sellable
  standalone.
- **Platform tier** -- new top-level dirs (`services/`, `platform/`, `web/`) for
  the FastAPI wrappers, orchestrator, MCP servers, React Flow UI, and the
  Postgres/MinIO/Redis it needs. It *imports* the catalog but never pollutes the
  kernel, and carries its own, lighter conventions (a web stack cannot live under
  stdlib-only laws).

## 10. What stays in the kernel (IP) vs what the platform owns

| Concern | Owner |
|---------|-------|
| Media transforms (frames, blur, restore, sort, classify, props, print, fetch) | **kernel Steps (IP)** |
| Belt shape (which node feeds which) | **`graph.json` + kernel loader** |
| Delivery / disposal | **kernel Sinks** |
| Native triggers (Telegram, folder) | **kernel Sources** |
| Run a Step by name | **`invoke` (Phase 0)** |
| HTTP/OpenAPI + MCP skins | **platform (`services/`)** |
| Object store (S3/MinIO) data plane | **platform** |
| Orchestrator (Mode B), event bus | **platform (`platform/`)** |
| Metering records / RU / billing | **platform** (records emitted at `invoke`) |
| Multi-tenant API, auth, quotas | **platform (`platform/`)** |
| React Flow canvas + constructor | **platform (`web/`)** |
| Exotic triggers we choose not to write | **n8n (optional, external)** |

## 11. Staged roadmap

- **Phase 0 -- done.** Service seam: `invoke` runs any Step
  (`minion_core/service.py`, `minions/service.py`).
- **Phase 1 -- done.** Graph as data: JSON loader into existing Stages
  (`minion_core/graph.py`, `minions/graph.py`, pilots `frames`/`inbox`).
- **Phase 1.5 -- event taps. Done.** The loader wraps stages to emit
  entered/left/verdict events in-process (`minion_core/events.py`); `--events`
  streams them. Cheap; unlocks animation and metering hooks; works offline.
- **Phase 2 -- service skin. Done.** One core (`services/core.py:run_service`)
  behind two facades -- HTTP/OpenAPI (`services/http.py`) and MCP
  (`services/mcp_server.py`) -- selected by `STEP`/`SKIN` env
  (`services/serve.py`, one image, N containers; `services/Dockerfile`). The
  `Store` (`services/store.py`: LocalStore now, S3Store for MinIO/AWS) is the
  object-store data plane with `input_ref`/`output_ref`; `ms` is captured at
  `invoke`. Hermetic core test in the kernel gate; skin tests in
  `services/tests`. The IP was not touched.
- **Phase 3 -- orchestrator (Mode B). Done.** `services/orchestrate.py` walks a
  graph's Step nodes as service calls (a `Caller`: LocalCaller in-process, or
  HttpCaller over each service's `/run`), threads each output ref into the next
  input, emits the Phase 1.5 events (so Mode A and Mode B animate the same),
  and records a `Usage` per node (`ms` toward RU, not yet billed). Proven with
  two independent services orchestrated over real HTTP through a shared store.
- **Phase 4 -- platform API. Done.** `services/api.py` (FastAPI): tenant-scoped
  `/catalog` (the React Flow palette), `/graphs` CRUD, `/runs` (drives the
  Phase 3 orchestrator), `/runs/{id}/events` (SSE), `/usage` (Compute RU from
  ms). Multi-tenant schema from day one (`services/models.py`, tenant_id on
  every entity; `services/repo.py` tenant-scoped, backend pluggable); tenant
  from an `X-Tenant-Id` header now, OIDC later (enforcement progressive). Runs
  are synchronous with SSE replay; a live event bus / background runs is the
  next refinement.
- **Phase 5 -- React Flow UI.** Viewer -> live animation -> constructor (palette
  from `/catalog`, drag, save `graph.json`).
- **Phase 6 -- last.** Per-request time shown to the user; RU billing,
  dashboards, tariffs.

## 12. Verification (per phase, not this document)

- Phase 1.5: an event tap emits one entered/left/verdict per node for a driven
  batch; equal to the current belt output otherwise.
- Phase 2: `POST /run` over a service returns the same verdict as `invoke`
  locally (golden, mirroring `tests/test_service.py`); the MCP tool returns the
  same; a round-trip through the `Store` preserves bytes.
- Phase 3+: an end-to-end run over two services reproduces the Mode A result for
  the same `graph.json`; a `Usage` row is emitted per node.
- Kernel gate stays green throughout: `ruff` (select=ALL), `mypy --strict`, the
  full `pytest` suite, and the ASCII + import-boundary analyses.
