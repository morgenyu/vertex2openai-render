# Render deployment

The root Dockerfile uses the same console image as the current local deployment,
pinned to `sha256:67c110e477d8d92da5e11034d9f3cdf6cedaec437afabcec64cef26710bea21e`
(created 2026-10-13). It retains the console, current models, request processing,
Express and Cookie channels, and resumable streams. The older `app/` source is
not copied over the console image.

Render keeps its existing `API_KEY` and `VERTEX_EXPRESS_API_KEY`. The process
listens on Render's `PORT`, with a local fallback of 7860. `/health` identifies
the running base image without requiring credentials.

The adaptation preserves the existing Render retry environment variables:

| Variable | Meaning |
| --- | --- |
| `RETRY_ENABLED` | `false` disables application retries. Default: `true`. |
| `RETRY_MAX_ATTEMPTS` | Total attempts, including the first; clamped to 1–51. Default: 11. |
| `RETRY_INTERVAL_MS` | Delay between retries; clamped to 0–120000 ms. Default: 5000. |
| `RETRY_STATUS_CODES` | Retryable HTTP codes. Default: 408,409,425,429,500,502,503,504. |

The console's retry count and delay override the environment defaults. HTTP
400/401/403/404 are not retried. Transport failures and upstream quota errors
are retryable. Express and Cookie requests share this policy, including normal,
real streaming, fake streaming, and image collection. Streams retry only before
content is emitted. SDK retries are disabled to keep the configured attempt
count accurate. Waiting streams retain the existing SSE heartbeat behavior.

Runtime settings are stored under `STATE_DIR` (default `/app/data`). On Render's
free plan the filesystem is ephemeral: console edits are lost on redeployment.
Environment credentials and retry defaults are reapplied at startup.

Build and validate without contacting a real model:

```powershell
docker build -t vertex2openai-render:local .
docker run --rm -e PYTHONPATH=/app -e STATE_DIR=/tmp/render-tests -e RETRY_MAX_ATTEMPTS=20 -e RETRY_INTERVAL_MS=500 -v "${PWD}/render/tests:/tests:ro" vertex2openai-render:local python -m unittest discover -s /tests -v
```

The patch checks the pinned handler layouts and fails the build if an image
upgrade changes a retry path. Update the digest only after rerunning the tests.
