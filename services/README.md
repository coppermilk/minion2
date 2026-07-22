# services -- atomic web services over a Step (HTTP/OpenAPI + MCP)

Each Step becomes **its own tiny container with an API**: bytes in, bytes out
over the web. A Telegram transport, a React Flow canvas or an MCP agent
all call the same service the same way. One image, N services: `STEP` selects
the Step, `SKIN` the facade (`http` | `mcp`).

## The shape

```
        one core: services/core.py:run_service
       (input file -> invoke(Step) -> output file, timed)
        /                                        \
   HTTP/OpenAPI                                 MCP
   services/http.py                        services/mcp_server.py
   (curl, HTTP client, tg-* relay)       (Claude / agents)
```

- **Nothing here touches the IP.** The Steps live in `minion_core` /
  `minions`; this tier only runs the file through the dispatcher
  (`minion_core.service.invoke`) and returns the result. Rip out this tier
  and the kernel still runs offline.
- **Stateless, bytes in / bytes out.** No shared object store: each request
  gets a fresh temp store, so the container holds nothing between calls.
- **`ms` is the timing seam:** measured around `invoke`, the one chokepoint
  every Step runs through (returned as the `X-Run-Ms` header).

## Run one service

```
# HTTP (OpenAPI at /openapi.json, probe at /healthz):
STEP=censor-blur SKIN=http python -m services.serve

# MCP (stdio):
STEP=censor-blur SKIN=mcp python -m services.serve
```

## Ways in (no object store)

- **`POST /run-file`** -- upload a file, get the result file back
  (`X-Disposition` / `X-Run-Ms` headers; `422` when the Step skips). The
  frictionless path for an HTTP client and the `tg-*` relays:

  ```
  curl -F file=@photo.jpg http://localhost:8091/run-file --output blurred.jpg
  ```

- **`POST /jobs/file`** -- the async path for slow Steps: `202` + a job id at
  once, then poll `GET /jobs/{id}` (and `GET /jobs/{id}/result`) or pass
  `?callback_url=` for a webhook. Upload and result live in a per-process temp
  store until fetched.
- **`GET /healthz`**, **`GET /openapi.json`** -- probe and schema.
- **MCP tool `run(input_path)`** -- the same Step for agents; returns the
  output path + verdict.

## The Telegram <-> service split

A pixel-transform bot (`censor-blur`, `censor-black`) is two containers:

- `svc-censor-blur` -- this tier, `STEP=censor-blur`, does the actual blur.
- `tg-censor-blur` -- a thin transport (`minions/telegram/relay.py`): a Telegram
  dock that POSTs the photo to `svc-censor-blur/run-file` via
  `minion_core/adapters/service_call.py:CallService` and sends the bytes back.
  No torch in the transport; the heavy work is the service.

React Flow is just another web consumer of the same `/run-file`.

## Tests

`tests/test_service_core.py` proves the core hermetically (in the kernel gate);
`tests/test_service_call.py` proves the relay seam. The skin tests need the web
stack and run off the gate:

```
pip install -e '.[dev]'   # from services/, or: pip install fastapi httpx mcp
python -m pytest services/tests
```
