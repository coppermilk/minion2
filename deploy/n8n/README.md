# Using the services from n8n

Every Step is a service with an HTTP API (and MCP), so n8n drives them like
any other node -- on their own ports, independently. n8n holds only the
wiring; the IP (the blur, restore, ... code) stays in this repo. Rip n8n out
any day and the same services still run.

Below: the blur Step (`censor-blur`) from n8n, two ways.

## 1. The easy way -- `POST /run-file` (bytes in, bytes out)

n8n already has the image as **binary**, and wants binary back. `/run-file`
takes an uploaded file, runs the Step, and returns the result file -- one
HTTP Request node, no object store needed.

**HTTP Request node**
- Method: `POST`
- URL: `http://svc-censor-blur:8000/run-file` (in the compose network) or
  `http://<host>:8091/run-file` (published port)
- Send Body: **on**; Body Content Type: **Form-Data (multipart)**
- Body Parameters -> one entry:
  - Name: `file`
  - Parameter Type: **n8n Binary File**
  - Input Data Field Name: `data` (the binary property from the previous
    node -- e.g. a Telegram Trigger's photo, a Google Drive download)
- Options -> Response -> Response Format: **File** (so the blurred image
  comes back as binary, ready for the next node)

Response headers carry `X-Disposition` (`delivered` / `skipped` / `failed`)
and `X-Run-Ms` (the Step's time). A Step that finds nothing to do (no person
in the photo) returns HTTP **422** with `skipped: no_person` -- branch on it
with an IF node if you like.

**Example flow:** `Telegram Trigger (photo)` -> `HTTP Request (/run-file)` ->
`Telegram (send photo)`. Swap the trigger/sink for Google Drive, Dropbox,
Webhook, etc. -- the blur node in the middle never changes. An importable
`censor-blur.workflow.json` (Webhook -> blur -> respond) sits next to this
file; test it with:

```
curl -F "file=@photo.jpg;type=image/jpeg" \
  http://<host>:8091/run-file -o blurred.jpg
```

### Long-running Steps (a minute or more): async `/jobs/file`

`/run-file` is synchronous -- it holds the connection until the Step
finishes. Fine up to ~a minute (raise the HTTP Request node's **Timeout**,
e.g. 120000 ms). For slower or uncertain Steps, don't hold the connection --
submit and check back:

```
POST /jobs/file  (multipart file [+ ?callback_url=...])  -> 202 { job_id }
GET  /jobs/{id}          -> { status: running|done|failed, disposition, ms, ... }
GET  /jobs/{id}/result   -> the result file, once done
```

The Step runs in the background; submit returns **at once** (202). Two ways
to learn it is ready from n8n:
- **Poll:** an HTTP Request node on `GET /jobs/{id}` in a loop (n8n's "Wait"
  + IF on `status == "done"`), then `GET /jobs/{id}/result`.
- **Webhook:** pass `?callback_url=<your n8n Webhook URL>`; the service POSTs
  the finished job summary there when done -- n8n's Webhook node wakes the
  rest of the flow. No polling, no held connection.

## 2. The MCP way -- for agent-style flows

Each service also speaks MCP (`SKIN=mcp`), exposing a `run` tool. n8n's
**MCP Client Tool** node connects to the `censor-blur` MCP server and calls
`run(input_path=...)`, returning `{output_path, disposition, reason, ms}`. Use
this when an AI Agent node should decide when to blur; use (1) for a plain
media pipeline.

## OpenAPI

Every service serves `GET /openapi.json`, so n8n (and any client) can
generate the request shape automatically; `GET /healthz` is the probe.
