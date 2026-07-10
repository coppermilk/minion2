# Using the services from n8n

Every Step is a service with an HTTP API (and MCP), so n8n drives them like
any other node -- on their own ports, independently. n8n holds only the
wiring; the IP (the blur, restore, frames, ... code) stays in this repo. Rip
n8n out any day and the same services still run under our own orchestrator.

Below: the blur Step (`censor-blur`) from n8n, three ways.

## 1. The easy way -- `POST /run-file` (bytes in, bytes out)

n8n already has the image as **binary**, and wants binary back. `/run-file`
takes an uploaded file, runs the Step, and returns the result file -- one
HTTP Request node, no S3 node needed.

**HTTP Request node**
- Method: `POST`
- URL: `http://censor-blur:8000/run-file` (in the compose network) or
  `http://<host>:8081/run-file` (published port)
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
  http://<host>:8081/run-file -o blurred.jpg
```

## 2. The ref way -- `POST /run` (the object-store data plane)

When the media already lives in the shared object store (MinIO), pass a
reference instead of bytes -- this is what our own orchestrator uses, and it
avoids moving big media through n8n.

- n8n **S3 node** (point it at MinIO): upload the image -> you have
  `s3://minion/<key>`.
- **HTTP Request node**: `POST http://censor-blur:8000/run`, JSON body
  `{ "input_ref": "s3://minion/<key>" }`. Reply:
  `{ "output_ref", "outputs", "disposition", "reason", "ms" }`.
- n8n **S3 node**: download `output_ref` (or each of `outputs` for a
  multi-file Step like frames).

## 3. The MCP way -- for agent-style flows

Each service also speaks MCP (`SKIN=mcp`), exposing a `run` tool. n8n's
**MCP Client Tool** node connects to the `censor-blur` MCP server and calls
`run(input_ref=...)`, returning the same `{output_ref, outputs, ...}`. Use
this when an AI Agent node should decide when to blur; use (1) for a plain
media pipeline.

## OpenAPI

Every service serves `GET /openapi.json`, so n8n (and any client) can
generate the request shape automatically; `GET /healthz` is the probe.
