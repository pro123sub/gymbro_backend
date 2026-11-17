# Crush Lifestyle Fitness Studio — Backend Repository (`FastAPI + Google Sheets + n8n Control Plane`)

Self-contained deployable Python backend for **Crush Lifestyle Fitness Studio**, designed to run independently on **Railway** or **Docker** alongside a separate Next.js SaaS frontend and a Railway-hosted **n8n** instance.

---

## Key Features

- **100% Google Sheets-Based Storage (`GYMBRO_STORAGE_BACKEND=google_sheets`)**: Zero external SQL/NoSQL database server required for gym owners. Uses an in-memory relational engine hydrated from and flushed on every write to a multi-tab Google Sheets JSON workbook (`GOOGLE_SHEETS_WORKBOOK_PATH=./data/crush_fitness_sheets_workbook.json`), with bidirectional remote push/pull (`n8n_bridge` or direct `google_api`).
- **Automated n8n Flow Provisioning (`workflows/GM-01`–`GM-05`)**: Auto-injects Google Spreadsheet ID, FastAPI callback URL, Gmail email follow-up templates, and n8n credentials (`googleSheetsOAuth2Api` / `gmailOAuth2`), deploying directly to n8n via REST API (`N8N_API_KEY`).
- **Enterprise Control Plane & Scheduling (`/api/v1/automations/*`)**: On-demand workflow triggers, recurring `cron` / `interval_minutes` / `one_time` schedules in `Asia/Kolkata`, dead-letter replay, signed HMAC-SHA256 webhooks (`X-Crush-Signature`), and enterprise Next.js headers (`X-Request-ID`, `X-Tenant-ID`, `X-Process-Time-Ms`, optional `X-API-Key`).
- **Cult.fit-Grade Gym & CFO Accounting Engine**: Member 360° lifecycle, Indian mobile (`91XXXXXXXXXX`) normalization, freeze with expiry extension & early unfreeze reclaim, attendance streaks & leaderboard, 6-stage Email follow-up cadence, 18% GST (`9% CGST + 9% SGST`) tax invoices, expenses P&L, receivables aging, Razorpay webhooks, and an 11-tool AI Gym Manager Agent.

---

## Repository Contents (`backend/`)

| Path | Purpose |
| --- | --- |
| [`app/`](app/) | FastAPI application, Google Sheets workbook persistence engine, routers, services, schemas, and built-in console UI |
| [`workflows/`](workflows/) | Importable / auto-deployable n8n workflow templates (`GM-01` through `GM-05`) + test payloads |
| [`scripts/`](scripts/) | Node.js & Python structural + JS runtime validators for all 5 n8n workflows |
| [`tests/test_api.py`](tests/test_api.py) | 28-test end-to-end pytest suite covering all backend APIs, Google Sheets sync, n8n provisioning & Docker artifacts |
| [`Dockerfile`](Dockerfile) | Production `python:3.12-slim` image copying `app/` and `workflows/`, running as non-root `appuser` |
| [`railway.json`](railway.json) | Railway Dockerfile builder + `/health` probe configuration |
| [`docker-compose.yml`](docker-compose.yml) | Local container compose setup mounting `./data:/app/data` and reading `./.env` |
| [`.env.example`](.env.example) | Complete environment template (ignored `.env` holds live secrets via [`.gitignore`](.gitignore) & [`.dockerignore`](.dockerignore)) |

---

## Quick Start (Local Python / Virtual Env)

```bash
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
copy .env.example .env
.venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8000 --reload
```

Run tests & workflow validators inside `backend/`:
```bash
.venv\Scripts\pytest.exe tests/test_api.py -v
.venv\Scripts\python.exe scripts/validate-n8n-workflow.py
node scripts/validate-all-workflows.js
```

---

## Deploying to Railway or Docker (Standalone `backend/` Repo)

### Deploy to Railway
1. Push `backend/` as its own Git repository (`.gitignore` excludes `.env`, `.venv/`, `__pycache__/`, and cache files) and connect it to Railway, or run `railway up` from inside `backend/`.
2. Railway automatically uses [`railway.json`](railway.json) and [`Dockerfile`](Dockerfile) to build the image and healthcheck `/health`.
3. Add variables from [`.env.example`](.env.example) in Railway **Variables**:
   - `GOOGLE_SHEETS_SPREADSHEET_ID`
   - `N8N_BASE_URL` & `N8N_API_KEY`
   - `N8N_WEBHOOK_SECRET`
   - `FASTAPI_PUBLIC_URL` (your deployed Railway backend URL)
   - `CORS_ORIGINS` (your deployed Next.js frontend domain)
   - `NOTIFICATION_CHANNEL=Email`, `STUDIO_SENDER_EMAIL`, `GYMBRO_API_KEY`, and optional `LLM_API_KEY`
4. Attach an optional persistent Railway Volume at `/app/data` to persist `crush_fitness_sheets_workbook.json` across redeploys.

### Run Locally with Docker Compose
```bash
docker compose up --build
```
API docs: `http://localhost:8000/docs` | Health: `http://localhost:8000/health` | System Info: `http://localhost:8000/api/v1/system/info`.

---

## Enterprise API Endpoints (For Next.js Frontend & n8n)

| Domain | Endpoints |
| --- | --- |
| **System & Auth** | `GET /health`, `GET /api/v1/system/info`, `GET /api/v1/analytics/dashboard` |
| **Google Sheets DB** | `GET /api/v1/sheets/status`, `GET /api/v1/sheets/tabs/{tab}`, `POST /api/v1/sheets/tabs/{tab}/append`, `POST /api/v1/sheets/sync/push`, `POST /api/v1/sheets/sync/pull`, `GET /api/v1/sheets/workbook-export` |
| **n8n Auto-Provisioning** | `GET /api/v1/automations/n8n/env-checklist`, `POST /api/v1/automations/n8n/generate`, `POST /api/v1/automations/n8n/deploy` |
| **Workflow Control & Schedules** | `GET/PATCH /api/v1/automations/workflows/{code}`, `POST .../trigger`, `GET/POST/PATCH /api/v1/automations/schedules`, `POST .../run-now`, `POST .../tick`, `POST .../logs/{id}/retry`, `POST /api/v1/webhooks/n8n/workflow-callback` |
| **Members & Freeze** | `POST/GET /api/v1/members`, `GET/PATCH/DELETE /api/v1/members/{id}`, `GET .../sheets-row`, `POST .../freeze`, `POST .../unfreeze`, `POST .../renew` |
| **Attendance & Follow-Ups** | `POST /api/v1/attendance/check-in`, `GET .../today`, `GET .../history`, `GET .../leaderboard`, `GET /api/v1/follow-ups/due`, `POST /api/v1/follow-ups/dispatch`, `GET .../history` |
| **Payments, GST & CFO Accounts** | `POST/GET /api/v1/payments`, `GET .../invoice`, `POST .../refund`, `POST .../webhook/razorpay`, `GET /api/v1/accounts/summary`, `GET .../receivables`, `POST/GET/DELETE /api/v1/accounts/expenses` |
| **Plans, Settings & AI Agent** | `GET/POST/PATCH /api/v1/plans`, `GET/PUT /api/v1/settings`, `GET /api/v1/automation-logs`, `POST /api/v1/agent/chat`, `POST /api/v1/agent/autonomous-cycle`, `GET /api/v1/agent/runs` |
