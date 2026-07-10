# Architecture at a glance

Companion to [PLATFORM.md](PLATFORM.md) (the platform ADR) and
[ORCHESTRATION.md](ORCHESTRATION.md). One picture of the whole system: the
austere kernel (the IP), the platform tier around it, and the consumers.

```mermaid
flowchart TB
  classDef ip   fill:#143024,stroke:#2ecc71,color:#dfe6f5
  classDef plat fill:#171e2e,stroke:#7a5cff,color:#dfe6f5
  classDef ext  fill:#0c1120,stroke:#2d6cdf,color:#dfe6f5
  classDef data fill:#30290f,stroke:#c9a227,color:#dfe6f5

  subgraph EXT["Consumers"]
    UI["React Flow canvas<br/>(/ui)"]:::ext
    N8N["n8n<br/>HTTP node / MCP"]:::ext
    AG["MCP agents<br/>(Claude, ...)"]:::ext
  end

  subgraph PLAT["Platform tier -- services/ (lighter conventions)"]
    API["Platform API (FastAPI)<br/>/catalog /graphs /runs<br/>/events (SSE) /billing<br/>multi-tenant"]:::plat
    ORCH["Orchestrator (Mode B)<br/>walks a graph -> service calls"]:::plat
    subgraph SVC["Service = one Step, own container + port"]
      SKINS["HTTP skin: /run, /run-file, /jobs (async)<br/>MCP skin: run()"]:::plat
      CORE["core: invoke() + timing (ms)"]:::plat
    end
  end

  subgraph KERN["Kernel tier -- minion_core / minions (the IP, untouched)"]
    CAT["Step catalog"]:::ip
    STEPS["Steps: deliver, censor-blur,<br/>frames, restore, fetch, ..."]:::ip
    BELT["Mode A: in-process belt<br/>kernel.run (offline, no network)"]:::ip
  end

  GRAPH["graph.json<br/>one description, two run modes"]:::data
  STORE["Store (data plane)<br/>LocalStore file://  |  S3Store -> MinIO / AWS"]:::data

  UI --> API
  AG --> SKINS
  N8N --> SKINS
  N8N -. optional .-> API
  API --> ORCH
  API -. live events (SSE) .-> UI
  ORCH -- HTTP / MCP --> SKINS
  ORCH -. events .-> API
  SKINS --> CORE
  CORE --> CAT --> STEPS
  CORE <--> STORE
  GRAPH --> ORCH
  GRAPH --> BELT
  BELT --> STEPS
```

## How to read it

- **Two tiers.** The **kernel** (green) is the IP -- the Steps and the belt,
  under the BLUEPRINT laws (ASCII, stdlib-only kernel, ruff ALL, mypy strict).
  The **platform** (purple) wraps it with a web stack and lighter conventions.
  The platform imports the kernel; never the other way round.
- **One core, many skins.** Every Step runs through one seam, `invoke()`.
  Around it sit thin skins: HTTP/OpenAPI (`/run`, `/run-file`, async `/jobs`)
  and MCP. Adding a protocol never touches a Step.
- **Two run modes over one `graph.json`.** *Mode A* is the in-process belt
  (`kernel.run`) -- offline, single node, no network. *Mode B* is the
  orchestrator walking the same graph as service calls. Same Steps; only the
  runner differs. This is why the detach guarantee is structural.
- **Data plane.** Services are stateless: they pass object references, not
  bytes. `Store` is `LocalStore` (offline, `file://`) or `S3Store` (MinIO /
  AWS, `s3://`) -- one image, picked by env.
- **Consumers are equal.** The React Flow canvas, n8n, and MCP agents all call
  the same services over the same APIs, each on its own port. n8n holds only
  wiring; the IP stays in the kernel. Rip any consumer out; the rest run.
- **Metering** rides at `invoke()` (the `ms` on every call) and rolls up into
  Resource Units at `/billing`.
