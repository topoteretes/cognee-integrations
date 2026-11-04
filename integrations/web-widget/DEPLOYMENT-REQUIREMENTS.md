# cognee web-widget backend: hosting requirements

A request for one dedicated server to run the web-widget backend
(`integrations/web-widget` in `topoteretes/cognee-integrations`) permanently.

Today the backend runs on a developer laptop and reaches the internet through a
Cloudflare **quick tunnel**. That tunnel gets a new hostname every time it restarts,
and each new hostname means editing, committing and redeploying `widget/widget.js`
in the docs repo. A fixed hostname is the main thing this server is for.

---

## What the service is

- A small **FastAPI** app (Python 3.11, served by uvicorn). Its dependencies are
  `fastapi`, `uvicorn` and `httpx`, installed with `uv sync` or `pip install -e .`.
  Entry point: `cognee-web-widget`, or `python -m cognee_integration_web_widget.server`.
- **It holds almost no data of its own.** Every question, conversation and ingested
  document goes to our Cognee Cloud tenant over HTTPS. The one exception is a small
  JSON file recording which conversations have been synced (see 6). The LLM keys live on
  the tenant, not here.
- It serves two kinds of client:
  - **The chat widget** on the docs site's Mintlify staging preview,
    `https://cognee-cognee-widget-integration.mintlify.site/`, which calls
    `/api/chat` and `/api/forget` from visitors' browsers, cross-origin.
  - **The operator dashboard** at `/dashboard`, which the team uses to ingest docs,
    watch analytics and start distillation. It is protected by a token.

## Requirements

### 1. Exactly one instance, one worker process

**This one is not negotiable.** The backend keeps state that must not be split
across processes:
- the sync record file, which a second process would overwrite
- the distillation run status
- a 15-minute cache of the knowledge-graph summary

So:
- **Processes:** run a single uvicorn process with no `--workers N`, no autoscaling
  and no load balancing across replicas.
- **Restarts:** a restart is fine; the caches rebuild themselves.

### 2. A stable public HTTPS hostname

- **Hostname:** a fixed name, for example `widget-api.cognee.ai`, with a valid TLS
  certificate. It must be HTTPS, because the widget runs on an HTTPS page (the
  Mintlify preview) and browsers block mixed content.
- **Proxy:** TLS terminates at a reverse proxy or load balancer in front of the app.
  The app itself speaks plain HTTP on `WEB_HOST:WEB_PORT`.
- **Tell us the hostname.** We put it into `DEFAULT_API` in the docs repo's
  `widget/widget.js`.

### 3. Reverse-proxy settings

| Setting | Value | Why |
|---|---|---|
| Upstream read/idle timeout | **≥ 10 minutes** | Clearing a dataset (`POST /api/dashboard/clear`) holds the request open until Cognee finishes deleting, which has taken over 2 minutes. |
| Max request body | **≥ 64 MB** | Dashboard ingests send file text in the request body. The app allows up to 50M characters per ingest. |
| Forward `X-Forwarded-For` / `-Proto` | yes | The rate limits count requests per visitor IP, read from `X-Forwarded-For` for the proxies named in `WIDGET_TRUSTED_PROXIES`. The proxy must append to the header, not pass a client's value through unchanged. |
| Query strings in access logs | **strip or don't log** | The dashboard token is sent as `?token=…`, so it currently ends up in access logs. We will move it to a header; until then, keep it out of logs. |
| CORS | leave to the app | The app sets CORS itself. The proxy must not add or strip CORS headers, and must pass `OPTIONS` requests through. |

The chat endpoints themselves are short, a few seconds per answer.

### 4. Outbound network

- **Destination:** HTTPS (443) to our Cognee Cloud tenant,
  `https://tenant-f44d1b1d-4277-4715-aac6-761bc90d2a3a.aws.cognee.ai`. No other
  outbound access is needed.
- **Long calls:** some calls to the tenant run for minutes. Distillation calls are
  allowed 7 minutes, and dataset deletes can take longer. Any egress proxy or NAT
  must not cut idle connections before about 10 minutes.

### 5. Size

| Resource | Minimum | Recommended | Why |
|---|---|---|---|
| vCPU | 1 | 2 | Mostly waiting on the network. |
| RAM | 1 GB | **2 GB** | The dashboard pulls the whole graph from Cognee, 46 MB of JSON for the current corpus, and parses it in memory. That grows with the corpus. |
| Disk | 5 GB | 10 GB | Code, virtualenv, logs, and the sync record (a few KB, on persistent disk). |

### 6. Configuration and secrets

Set these as environment variables, or in a `.env` file in the directory the app
starts in (a variable in the environment wins over the file).

| Variable | Secret? | Value |
|---|---|---|
| `COGNEE_BASE_URL` | no | the tenant URL above |
| `COGNEE_API_KEY` | **yes** | API key for the tenant (we will provide) |
| `WIDGET_DASHBOARD_TOKEN` | **yes** | random token that unlocks `/dashboard`; if unset, the dashboard returns 404 |
| `WIDGET_SYNC_STATE_PATH` | no | path to a small JSON file on **persistent disk** (e.g. `/var/lib/cognee-widget/sync-state.json`); it records which conversations have been synced, and must survive restarts and redeploys |
| `WEB_HOST` | no | `0.0.0.0`, or whatever the proxy reaches (the default is `127.0.0.1`) |
| `WEB_PORT` | no | e.g. `8101` |
| `WIDGET_SITE_ID` | no | `demo` |
| `WIDGET_ALLOWED_ORIGINS` | no | `https://docs.cognee.ai` (plus the Mintlify preview origin while testing); the default `*` lets any site call the API |
| `WIDGET_TRUSTED_PROXIES` | no | the proxy's or load balancer's IPs or CIDR ranges, comma-separated, so each visitor's IP is read from `X-Forwarded-For`; without it every visitor shares one rate limit |
| `WIDGET_RATE_LIMIT_PER_IP_HOUR` | no | requests per visitor IP per hour; default `20` |
| `WIDGET_RATE_LIMIT_PER_IP_DAY` | no | requests per visitor IP per day; default `100` |
| `WIDGET_RATE_LIMIT_GLOBAL_HOUR` | no | chat requests per hour across all visitors, the ceiling on tenant spend; default `500` |

- **Store secrets** in the secrets manager. Never put them in the image or the repo.
- **Rotating the dashboard token** only needs a restart.

### 7. Running it

- **Supervision:** run it as a supervised service (systemd, or a container with a
  restart policy) that restarts on crash and on boot.
- **Deploying:** check out the repo at a given branch or commit, run `uv sync`, then
  restart the service. We will tell you which branch to track. It is currently
  `feature/web-widget-dashboard-and-citations`.
- **Logs:** everything goes to stdout/stderr. Keep 14 days.
- **Health check:** `GET /widget.js` → `200`. There is no dedicated `/health` route
  yet; we can add one if your tooling needs it.
- **Monitoring:** alert if the health check fails for 5 minutes. No metrics endpoint
  exists yet.

### 8. Access

- **Public:** `/api/chat`, `/api/forget`, `/widget.js`, `/`. These are what visitors'
  browsers call. `/api/chat` and `/api/forget` are rate-limited per visitor IP, and
  chat also has a global hourly limit (see section 6).
- **Token-protected:** `/dashboard` and `/api/dashboard/*`. These can stay public
  because of the token. If you'd rather restrict them to VPN or office IPs, that
  works too; tell us which.
- **Deploy access:** SSH or deploy access for us (the widget developers) to read
  logs and trigger a redeploy.

### 9. TBD: Okta login for the dashboard

**Not decided yet; not needed for the first deploy.** The plan is to replace the
dashboard's shared token with an Okta sign-in, done at the proxy rather than in the
app:

- **At the proxy:** Okta login in front of **only** `/dashboard` and
  `/api/dashboard/*`, for example ALB OIDC authentication, oauth2-proxy or
  Cloudflare Access.
- **Everything else stays public:** `/api/chat`, `/api/forget` and `/widget.js` must
  never go behind Okta, or the widget breaks for every visitor.
- **What it needs from you:**
  - an Okta OIDC app with a redirect URI on the new hostname
  - the Okta group allowed in
  - which proxy you'd use (the backend reads the signed-in user from a header, and
    the header's form depends on the proxy)
- **Our side:** the backend reads the user from the proxy's header instead of the
  token, and must only be reachable through the proxy.

Until then the dashboard stays behind `WIDGET_DASHBOARD_TOKEN`.

---

## Fixed on our side before go-live

These are ours to do, not the cloud team's, and are listed so nobody is surprised:

1. **Stop seeding demo documents on startup.** At present, every boot writes a few
   sample paragraphs into the live docs dataset (`lifespan` in `server.py`).
2. **Move the dashboard token out of the URL** and into a header, so it stops
   appearing in logs.
3. **Make dataset clears tolerate slow deletes:** drop the graph cache whatever the
   outcome and allow a longer timeout (see finding #12 in `COGNEE-FINDINGS.md`).
4. **Point `widget.js` at the new hostname** in the docs repo once you give it to us.
5. **Add a `/health` route** if wanted.

## Questions for the cloud team

- Which hostname can we have, and under which domain?
- Container or VM? Either works; the app has no Dockerfile yet, and we can add one.
- Should the dashboard be public behind its token, or behind VPN or an IP allowlist?
- TBD (section 9): which proxy could put Okta in front of the dashboard, and who can
  register the Okta app?
- Where do secrets live, and how do we hand over `COGNEE_API_KEY`?
