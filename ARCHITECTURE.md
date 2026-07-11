# Architecture at a glance

One picture of the whole system: the austere kernel (the IP), the atomic web
services wrapped around each Step, and the consumers that call them over the
web -- Telegram transports, n8n, a React Flow canvas, MCP agents. Everything
is one docker-compose project.

```mermaid
flowchart TB
  classDef ip   fill:#143024,stroke:#2ecc71,color:#dfe6f5
  classDef svc  fill:#171e2e,stroke:#7a5cff,color:#dfe6f5
  classDef ext  fill:#0c1120,stroke:#2d6cdf,color:#dfe6f5

  subgraph EXT["Consumers (separate containers, over the web)"]
    TG["tg-* relay<br/>Telegram dock -> service"]:::ext
    N8N["n8n<br/>HTTP node / MCP"]:::ext
    RF["React Flow canvas<br/>(placeholder)"]:::ext
    AG["MCP agents<br/>(Claude, ...)"]:::ext
  end

  subgraph SVCS["Atomic services -- services/ (one Step each, bytes in/out)"]
    SKINS["HTTP: /run-file, /jobs/file, /healthz, /openapi.json<br/>MCP: run(input_path)"]:::svc
    CORE["core: invoke(Step) + timing (ms)"]:::svc
  end

  subgraph KERN["Kernel tier -- minion_core / minions (the IP, untouched)"]
    CAT["Step catalog<br/>minions/service.py"]:::ip
    STEPS["Steps: censor-blur, censor-black,<br/>restore-mark, deliver, frames, fetch, ..."]:::ip
    BELT["In-process belt: kernel.run<br/>(the monolith bots, offline)"]:::ip
  end

  TG -- "POST /run-file (bytes)" --> SKINS
  N8N -- "HTTP / MCP" --> SKINS
  RF -- "HTTP" --> SKINS
  AG -- "MCP" --> SKINS
  SKINS --> CORE
  CORE --> CAT --> STEPS
  BELT --> STEPS
```

## How to read it

- **Two tiers.** The **kernel** (green) is the IP -- the Steps and the belt,
  under the BLUEPRINT laws (ASCII, stdlib-only kernel, ruff ALL, mypy strict).
  The **services** tier (purple) wraps a Step with a thin web skin and lighter
  conventions. Services import the kernel; never the other way round.
- **One core, two skins.** Every Step runs through one seam, `invoke()`.
  Around it sit thin skins: HTTP/OpenAPI (`/run-file`, async `/jobs/file`) and
  MCP. Adding a protocol never touches a Step.
- **Bytes in, bytes out.** A service is stateless -- upload a file, get the
  result file back; a fresh temp store per request, no shared object store, no
  cloud SDK. Timing (`ms`) is captured around `invoke` (`X-Run-Ms`).
- **The Telegram split.** A pixel-transform bot is a pair: `svc-<step>` does
  the work, `tg-<step>` is a thin Telegram dock that POSTs the file to the
  service and sends the bytes back (`CallService`). The heavy compute leaves
  the transport.
- **Consumers are equal.** The `tg-*` relays, n8n, React Flow and MCP agents
  all call the same services over the same HTTP/MCP. Rip any consumer out; the
  rest run. The IP never leaves the kernel.
- **The monolith bots stay.** Bots whose work does not reduce to bytes in/out
  (inbox's canonical naming, fetch's routing, frames' folder-of-frames output,
  the command bots) keep running as single in-process belts -- functionality
  untouched.
