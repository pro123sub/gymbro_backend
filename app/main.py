"""FastAPI entrypoint for Crush Lifestyle Fitness Studio Gym Management SaaS."""

from __future__ import annotations

from contextlib import asynccontextmanager
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .config import get_settings, timestamp_ist_str
from .database import get_db, init_db
from .routers import accounts, agent, attendance, automations, followups, members, n8n_webhooks, payments
from .services import n8n_client


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize SQLite schema and default plans on application startup."""
    init_db()
    yield


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Enterprise Cult.fit-grade Gym Management SaaS & n8n Control Plane for Next.js Clients.",
        lifespan=lifespan,
    )

    allowed_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_origin_regex=r"https://.*\.(vercel\.app|up\.railway\.app)",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Tenant-ID", "X-Process-Time-Ms"],
    )

    @app.middleware("http")
    async def enterprise_context_and_auth_guard(request: Request, call_next):
        started = time.perf_counter()
        request_id = request.headers.get("X-Request-ID", "").strip() or f"req-{secrets.token_hex(6)}"
        tenant_id = request.headers.get("X-Tenant-ID", "").strip() or "crush-main-studio"

        configured_key = get_settings().api_key.strip()
        is_webhook_path = request.url.path.startswith("/api/v1/payments/webhook/") or request.url.path.startswith(
            "/api/v1/webhooks/n8n/"
        )
        if configured_key and request.url.path.startswith("/api/") and not is_webhook_path:
            provided = request.headers.get("X-API-Key", "").strip()
            if provided != configured_key:
                resp = JSONResponse(status_code=401, content={"detail": "Invalid or missing X-API-Key."})
                resp.headers["X-Request-ID"] = request_id
                resp.headers["X-Tenant-ID"] = tenant_id
                resp.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
                return resp

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Tenant-ID"] = tenant_id
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
        return response

    app.include_router(members.router)
    app.include_router(payments.router)
    app.include_router(attendance.router)
    app.include_router(followups.router)
    app.include_router(accounts.router)
    app.include_router(agent.router)
    app.include_router(automations.router)
    app.include_router(n8n_webhooks.router)

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def serve_home():
        return HTMLResponse(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>GymBro Backend</title></head>"
            "<body><h1>Welcome to Gymbero your app is healthy</h1></body></html>"
        )

    @app.get("/health", tags=["System"])
    def health_check():
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.app_version,
            "timezone": settings.timezone,
            "timestamp_ist": timestamp_ist_str(),
        }

    @app.get("/api/v1/system/info", tags=["System"])
    def get_system_info():
        """Return enterprise integration capabilities, headers, and automation telemetry for Next.js clients."""
        workflows = n8n_client.list_workflows()
        with get_db() as conn:
            active_sched_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM scheduled_automation_jobs WHERE status = 'ACTIVE'"
            ).fetchone()

        def _count_leaf_routes(routes: list) -> int:
            paths = app.openapi().get("paths", {})
            return sum(len(ops) for ops in paths.values()) + len(
                [r for r in routes if getattr(r, "path", None) in ("/", "/health")]
            )

        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "timezone": settings.timezone,
            "timestamp_ist": timestamp_ist_str(),
            "docs_url": "/docs",
            "openapi_url": "/openapi.json",
            "required_headers": ["X-API-Key (when configured)", "X-Tenant-ID (optional)", "X-Request-ID (optional)"],
            "exposed_response_headers": ["X-Request-ID", "X-Tenant-ID", "X-Process-Time-Ms"],
            "active_schedules_count": int(active_sched_row["cnt"] or 0),
            "workflows_count": len(workflows),
            "workflows": workflows,
            "routes_count": _count_leaf_routes(app.routes),
        }

    return app


app = create_app()
