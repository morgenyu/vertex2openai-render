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
| `RETRY_INTERVAL_MS` | Initial retry delay; clamped to 0–120000 ms. Default: 5000. |
| `RETRY_MAX_BACKOFF_MS` | Maximum delay after exponential backoff. Default: 3000 ms. |
| `RETRY_STATUS_CODES` | Retryable HTTP codes. Default: 408,409,425,429,500,502,503,504. |

The console's retry count and initial delay override the environment defaults.
The delay doubles after each failure and never exceeds `RETRY_MAX_BACKOFF_MS`.
With `RETRY_MAX_ATTEMPTS=31` and `RETRY_INTERVAL_MS=500`, the 30 retry waits are
0.5, 1, 2, then 3 seconds each, totaling 84.5 seconds plus upstream call time.
HTTP
400/401/403/404 are not retried. Transport failures and upstream quota errors
are retryable. Express and Cookie requests share this policy, including normal,
real streaming, fake streaming, and image collection. Streams retry only before
content is emitted. SDK retries are disabled to keep the configured attempt
count accurate. Waiting streams retain the existing SSE heartbeat behavior.

Runtime settings are stored under `STATE_DIR` (default `/app/data`). On Render's
free plan the filesystem is ephemeral: console edits are lost on redeployment.
Environment credentials and retry defaults are reapplied at startup.

## Subscription egress

Set `PROXY_SUBSCRIPTION_URL` as a private Render environment variable containing
the HTTPS Clash subscription URL. Add further HTTPS Clash subscriptions with
`PROXY_SUBSCRIPTION_URLS`, a JSON array of URLs. The original subscription is
retained, duplicates are removed, and each provider gets a unique node prefix
when several subscriptions are configured. Providers are named `subscription`,
`subscription-2`, etc. The container runs a pinned Mihomo binary
alongside the API; its proxy and authenticated controller listen on loopback only.
The subscription and random controller secret are created at startup under
`STATE_DIR`, never included in the image or repository.

Google requests use the proxy through `PROXY_URL`. Startup checks a node against
Google HTTPS; retryable failures rotate to another healthy node before retrying.
Failed nodes cool down for 300 seconds, and concurrent failures on the same
generation cause a single rotation. Success keeps the current node. Existing
streams are preserved; new requests open fresh proxy tunnels. An unavailable
proxy pool returns an error instead of switching Google traffic to direct access.
Different subscription nodes may still share an exit IP.

Authenticated diagnostics are available at `GET /api/egress` (direct IP, proxy exit
IP, counts per provider and current selection) and `POST /api/egress/rotate` (test
a switch). `POST /api/egress/check` probes every node against Google HTTPS and
returns healthy counts. Both POST routes accept an optional `provider` query
parameter to check or select from one subscription. An unavailable provider does
not prevent selecting nodes from the remaining providers. A subscription that
cannot be downloaded at startup is omitted for that deployment.
These endpoints use the console's existing login session. Raw Mihomo output is
suppressed because errors can include private subscription URLs.

Build and validate without contacting a real model:

```powershell
docker build -t vertex2openai-render:local .
docker run --rm -e PYTHONPATH=/app -e STATE_DIR=/tmp/render-tests -e RETRY_MAX_ATTEMPTS=31 -e RETRY_INTERVAL_MS=500 -e RETRY_MAX_BACKOFF_MS=3000 -v "${PWD}/render/tests:/tests:ro" vertex2openai-render:local python -m unittest discover -s /tests -v
```

The patch checks the pinned handler layouts and fails the build if an image
upgrade changes a retry path. Update the digest only after rerunning the tests.
