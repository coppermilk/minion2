# services -- HTTP/OpenAPI + MCP skins over the Step core

Platform tier (PLATFORM.md, Phase 2). Each Step becomes **its own container
with an API**, reachable independently by our orchestrator, by n8n, and by
MCP agents -- on their own ports. One image, N services: `STEP` selects the
Step, `SKIN` the facade.

## The shape

```
        one core: services/core.py:run_service
       (fetch input_ref -> invoke(Step) -> put output_ref, timed)
        /                    |                     \
   HTTP/OpenAPI            MCP                    Store
   services/http.py    services/mcp_server.py   services/store.py
   (orchestrator,      (Claude / agents)        (LocalStore | S3Store:
    n8n HTTP node)                               MinIO / AWS)
```

- **Nothing here touches the IP.** The Steps live in `minion_core` /
  `minions`; this tier only fetches an input by reference, calls the Phase 0
  dispatcher (`minion_core.service.invoke`), and puts the output back. Rip
  out this tier and the kernel still runs offline (Mode A).
- **Stateless.** Each request gets a fresh temp DRIVE; state lives in the
  Store (an S3-compatible object store: MinIO self-hosted, or AWS/compatible
  in cloud), never in the container.
- **`ms` is the metering seam** (PLATFORM.md, section 6): timing is captured
  around `invoke`, the one chokepoint every Step runs through.

## Run one service

```
# HTTP (OpenAPI at /openapi.json, probe at /healthz):
STEP=deliver SKIN=http STORE_ROOT=/tmp/store python -m services.serve

# MCP (stdio):
STEP=deliver SKIN=mcp  STORE_ROOT=/tmp/store python -m services.serve
```

`POST /run` body `{"input_ref": "file:///path/to/input"}` returns
`{"output_ref", "disposition", "reason", "ms"}`.

## n8n and agents, side by side

Because every service publishes OpenAPI, n8n's HTTP Request node calls it on
its own port with no glue; because every service also speaks MCP, an agent
calls the very same Step as a tool. n8n holds only wiring, never IP -- the
detach guarantee at the protocol level (PLATFORM.md, section 8).

## Tests

`tests/test_service_core.py` proves the core hermetically (LocalStore, in the
kernel gate). The skin tests need the web stack and run off the gate:

```
pip install -e '.[dev]'   # from services/, or: pip install fastapi httpx mcp boto3
python -m pytest services/tests
```

## Not yet (follow-ups)

- Directory results (frames): `services/store.py:child_refs` puts each file;
  `run_service` returns the first-level ref set. Wired in a later pass.
- S3Store against a live MinIO (unit-covered by shape; integration later).
