"""Master PostgreSQL + Google Sheets Presentation Persistence Engine."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover - optional when running SQLite/Sheets fallback only
    psycopg2 = None  # type: ignore[assignment]

from .config import get_settings, timestamp_ist_str

PLAN_SEED_ROWS = [
    ("PLAN-M01", "Monthly", 30, 1500.0, 0, "30-day all-access gym floor & group classes", 1),
    ("PLAN-Q03", "Quarterly", 90, 4000.0, 7, "90-day transformation pack with 7 freeze days", 1),
    ("PLAN-H06", "Half-Yearly", 180, 7500.0, 15, "180-day consistency pack with 15 freeze days & BCA check-ins", 1),
    ("PLAN-Y12", "Yearly", 365, 13500.0, 30, "365-day Cult-grade annual membership with 30 freeze days", 1),
]

DEFAULT_GYM_SETTINGS: dict[str, str] = {
    "gym_name": "Crush Lifestyle Fitness Studio",
    "branch_name": "Main Studio",
    "timezone": "Asia/Kolkata",
    "currency": "INR",
    "gst_rate": "0.18",
    "gstin": "27AAACR5055K1Z5",
    "sac_code": "999723",
    "opening_time": "05:30",
    "closing_time": "22:30",
    "support_phone": "919876543210",
}

WORKFLOW_SEED_ROWS = [
    (
        "GM-01",
        "GM-01 - Gym Member Registration",
        "Intake validation, MEM-XXXXXXXX generation, Master PostgreSQL upsert, and formatted Google Sheets append",
        "form",
        "",
        "Asia/Kolkata",
        "/webhook/crush-member-registration",
        "TyvdBoE1S0x0CqEh",
        1,
    ),
    (
        "GM-02",
        "GM-02 - Smart Expiry, Dues & Onboarding Follow-Up Cadence",
        "Daily 9AM IST multi-stage Email (Gmail) retention cadence synced to Master PostgreSQL & Sheets",
        "schedule",
        "0 9 * * *",
        "Asia/Kolkata",
        "/webhook/crush-gm02-followup",
        "iF0yxmS2iQOl5yg4",
        1,
    ),
    (
        "GM-03",
        "GM-03 - Payment Webhook & Membership Renewal Engine",
        "Idempotent Razorpay & FastAPI payment settlement into Master PostgreSQL first, then GST Ledger & Sheets",
        "webhook",
        "",
        "Asia/Kolkata",
        "/webhook/crush-payment-webhook",
        "aEx0lIp361veuGZm",
        1,
    ),
    (
        "GM-04",
        "GM-04 - Daily Owner Digest & Inactivity Follow-Up",
        "Daily 9PM IST automated KPI trends, churn radar, and autonomous AI P&L audit",
        "schedule",
        "0 21 * * *",
        "Asia/Kolkata",
        "/webhook/crush-gm04-digest",
        "2CmWRbhlVxDhCeCk",
        1,
    ),
    (
        "GM-05",
        "GM-05 - Autonomous AI Gym Manager & Accounts Agent",
        "Bidirectional Email & Owner command bridge to Master PostgreSQL, OpenAI Agent & Sheets",
        "webhook",
        "",
        "Asia/Kolkata",
        "/webhook/crush-ai-agent",
        "LLxGJlLf6cBj5jBx",
        1,
    ),
]

TAB_TABLE_ORDER: list[tuple[str, str]] = [
    ("Membership Plans", "membership_plans"),
    ("Members", "members"),
    ("Workflow Registry", "workflow_registry"),
    ("Payments", "payments"),
    ("Attendance", "attendance"),
    ("Membership Freezes", "membership_freezes"),
    ("Follow-Up Logs", "follow_up_logs"),
    ("Automation Log", "automation_logs"),
    ("Gym Settings", "gym_settings"),
    ("Expenses", "expenses"),
    ("Agent Runs", "agent_runs"),
    ("Scheduled Jobs", "scheduled_automation_jobs"),
]

TABLE_PRIMARY_KEYS: dict[str, str] = {
    "membership_plans": "plan_id",
    "members": "member_id",
    "workflow_registry": "workflow_code",
    "payments": "payment_id",
    "attendance": "attendance_id",
    "membership_freezes": "freeze_id",
    "follow_up_logs": "log_id",
    "automation_logs": "log_id",
    "gym_settings": "setting_key",
    "expenses": "expense_id",
    "agent_runs": "run_id",
    "scheduled_automation_jobs": "job_id",
}

TAB_NAME_TO_TABLE: dict[str, str] = {tab: tbl for tab, tbl in TAB_TABLE_ORDER}
TAB_NAME_LOOKUP: dict[str, str] = {tab.lower(): tab for tab, _ in TAB_TABLE_ORDER}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS membership_plans (
    plan_id TEXT PRIMARY KEY,
    plan_name TEXT UNIQUE NOT NULL,
    duration_days INTEGER NOT NULL,
    price REAL NOT NULL,
    max_freeze_days INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    gender TEXT NOT NULL DEFAULT '',
    date_of_birth TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    emergency_contact_name TEXT NOT NULL DEFAULT '',
    emergency_contact_phone TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL,
    fee REAL NOT NULL,
    start_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    payment_status TEXT NOT NULL,
    last_payment_date TEXT NOT NULL DEFAULT '',
    last_reminder_date TEXT NOT NULL DEFAULT '',
    member_status TEXT NOT NULL DEFAULT 'Active',
    notes TEXT NOT NULL DEFAULT '',
    fitness_goal TEXT NOT NULL DEFAULT 'General Fitness',
    batch_slot TEXT NOT NULL DEFAULT 'Flexible',
    assigned_trainer TEXT NOT NULL DEFAULT 'Unassigned',
    medical_notes TEXT NOT NULL DEFAULT '',
    discount_amount REAL NOT NULL DEFAULT 0,
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    last_checkin_date TEXT NOT NULL DEFAULT '',
    total_checkins INTEGER NOT NULL DEFAULT 0,
    freeze_days_used INTEGER NOT NULL DEFAULT 0,
    frozen_until TEXT NOT NULL DEFAULT '',
    created_at_ist TEXT NOT NULL DEFAULT '',
    updated_at_ist TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_members_phone ON members(phone);
CREATE INDEX IF NOT EXISTS idx_members_expiry ON members(expiry_date);
CREATE INDEX IF NOT EXISTS idx_members_status ON members(member_status);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    invoice_number TEXT UNIQUE NOT NULL,
    member_id TEXT NOT NULL,
    member_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    amount_paid REAL NOT NULL,
    gst_amount REAL NOT NULL DEFAULT 0,
    payment_mode TEXT NOT NULL DEFAULT 'UPI',
    transaction_ref TEXT UNIQUE NOT NULL,
    payment_date TEXT NOT NULL,
    previous_expiry TEXT NOT NULL DEFAULT '',
    new_expiry_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Confirmed',
    notes TEXT NOT NULL DEFAULT '',
    created_at_ist TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (member_id) REFERENCES members(member_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attendance (
    attendance_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    checkin_date TEXT NOT NULL,
    checkin_time_ist TEXT NOT NULL,
    workout_type TEXT NOT NULL DEFAULT 'Gym Floor',
    batch_slot TEXT NOT NULL DEFAULT 'Flexible',
    streak_after_checkin INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (member_id) REFERENCES members(member_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attendance_member_date
    ON attendance(member_id, checkin_date);

CREATE TABLE IF NOT EXISTS membership_freezes (
    freeze_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    freeze_days INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    previous_expiry TEXT NOT NULL,
    new_expiry TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at_ist TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (member_id) REFERENCES members(member_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS follow_up_logs (
    log_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    followup_type TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'Email',
    message TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'SENT',
    dispatched_at_ist TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (member_id) REFERENCES members(member_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS automation_logs (
    log_id TEXT PRIMARY KEY,
    workflow TEXT NOT NULL,
    event_type TEXT NOT NULL,
    member_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    response_status INTEGER,
    details TEXT NOT NULL DEFAULT '',
    created_at_ist TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS expenses (
    expense_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    vendor TEXT NOT NULL DEFAULT '',
    payment_mode TEXT NOT NULL DEFAULT 'UPI',
    expense_date TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL DEFAULT 'AI_Agent',
    created_at_ist TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    actor_role TEXT NOT NULL,
    actor_phone TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL,
    intent TEXT NOT NULL,
    tools_called_json TEXT NOT NULL DEFAULT '[]',
    reply TEXT NOT NULL,
    sheets_sync_json TEXT NOT NULL DEFAULT '{}',
    created_at_ist TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gym_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at_ist TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS workflow_registry (
    workflow_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    trigger_type TEXT NOT NULL,
    cron_expression TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    webhook_path TEXT NOT NULL DEFAULT '',
    n8n_workflow_id TEXT NOT NULL DEFAULT '',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    last_triggered_at_ist TEXT NOT NULL DEFAULT '',
    last_status TEXT NOT NULL DEFAULT 'IDLE',
    total_runs INTEGER NOT NULL DEFAULT 0,
    updated_at_ist TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scheduled_automation_jobs (
    job_id TEXT PRIMARY KEY,
    workflow_code TEXT NOT NULL,
    job_name TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    cron_expression TEXT NOT NULL DEFAULT '',
    run_at_ist TEXT NOT NULL DEFAULT '',
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    last_run_at_ist TEXT NOT NULL DEFAULT '',
    next_run_at_ist TEXT NOT NULL DEFAULT '',
    run_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT 'NextJS_Client',
    created_at_ist TEXT NOT NULL DEFAULT '',
    updated_at_ist TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (workflow_code) REFERENCES workflow_registry(workflow_code)
);
"""

_PG_SCHEMA_SQL = _SCHEMA_SQL.replace(" REAL ", " DOUBLE PRECISION ")

_STORE_LOCK = threading.RLock()
_MEM_CONN: sqlite3.Connection | None = None
_ACTIVE_STORE_KEY: tuple[str, str] | None = None
_PG_CONN: Any = None
_PG_CONNECTED: bool = False
_PG_LAST_ERROR: str = ""
_PG_LAST_SYNC_IST: str = ""
_TX_DEPTH = 0


def _resolved_workbook_path() -> Path:
    return Path(get_settings().google_sheets_workbook_path).expanduser().resolve()


def _configured_database_url() -> str:
    return (get_settings().database_url or "").strip()


def _is_postgres_enabled() -> bool:
    url = _configured_database_url().lower()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _open_pg_connection(dsn: str) -> Any:
    if psycopg2 is None:
        raise RuntimeError("psycopg2-binary is required when DATABASE_URL is configured.")
    kwargs: dict[str, Any] = {"connect_timeout": 10}
    if "sslmode=" not in dsn.lower():
        kwargs["sslmode"] = "require"
    conn = psycopg2.connect(dsn, **kwargs)
    conn.autocommit = False
    return conn


def _get_pg_connection_locked() -> Any:
    global _PG_CONN, _PG_CONNECTED, _PG_LAST_ERROR
    dsn = _configured_database_url()
    if _PG_CONN is not None and not getattr(_PG_CONN, "closed", 1):
        return _PG_CONN
    _PG_CONN = _open_pg_connection(dsn)
    _PG_CONNECTED = True
    _PG_LAST_ERROR = ""
    return _PG_CONN


def _close_pg_connection_locked() -> None:
    global _PG_CONN, _PG_CONNECTED
    if _PG_CONN is not None:
        try:
            _PG_CONN.close()
        except Exception:
            pass
    _PG_CONN = None
    _PG_CONNECTED = False


def _init_pg_schema_locked(pg_conn: Any) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(_PG_SCHEMA_SQL)
    pg_conn.commit()


def _fetch_all_from_pg_locked() -> dict[str, list[dict[str, Any]]]:
    global _PG_CONNECTED, _PG_LAST_ERROR
    pg_conn = _get_pg_connection_locked()
    try:
        _init_pg_schema_locked(pg_conn)
        tabs: dict[str, list[dict[str, Any]]] = {}
        with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for tab_name, table_name in TAB_TABLE_ORDER:
                cur.execute(f"SELECT * FROM {table_name}")
                tabs[tab_name] = [dict(r) for r in cur.fetchall()]
        pg_conn.commit()
        _PG_CONNECTED = True
        _PG_LAST_ERROR = ""
        return tabs
    except Exception as exc:
        _close_pg_connection_locked()
        _PG_LAST_ERROR = str(exc)
        raise


def _sync_to_master_postgres_locked(conn: sqlite3.Connection) -> None:
    """Persist mutations to the Master Render PostgreSQL database first inside one ACID tx."""
    global _PG_CONNECTED, _PG_LAST_ERROR, _PG_LAST_SYNC_IST
    if not _is_postgres_enabled():
        return

    for attempt in range(2):
        pg_conn = _get_pg_connection_locked()
        try:
            with pg_conn.cursor() as cur:
                # 1. Remove deleted rows in reverse dependency order (children first)
                for _, table_name in reversed(TAB_TABLE_ORDER):
                    pk_col = TABLE_PRIMARY_KEYS[table_name]
                    local_pks = [r[0] for r in conn.execute(f"SELECT {pk_col} FROM {table_name}").fetchall()]
                    if not local_pks:
                        cur.execute(f"DELETE FROM {table_name}")
                    else:
                        cur.execute(
                            f"DELETE FROM {table_name} WHERE NOT ({pk_col} = ANY(%s))",
                            (local_pks,),
                        )

                # 2. Upsert active rows in parent-to-child dependency order
                for _, table_name in TAB_TABLE_ORDER:
                    pk_col = TABLE_PRIMARY_KEYS[table_name]
                    rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()
                    if not rows:
                        continue
                    cols = list(rows[0].keys())
                    cols_sql = ", ".join(cols)
                    update_cols = [c for c in cols if c != pk_col]
                    update_sql = (
                        ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                        if update_cols
                        else f"{pk_col} = EXCLUDED.{pk_col}"
                    )
                    values = [tuple(r[c] for c in cols) for r in rows]
                    psycopg2.extras.execute_values(
                        cur,
                        f"INSERT INTO {table_name} ({cols_sql}) VALUES %s "
                        f"ON CONFLICT ({pk_col}) DO UPDATE SET {update_sql}",
                        values,
                    )
            pg_conn.commit()
            _PG_CONNECTED = True
            _PG_LAST_ERROR = ""
            _PG_LAST_SYNC_IST = timestamp_ist_str()
            return
        except Exception as exc:
            try:
                pg_conn.rollback()
            except Exception:
                pass
            _close_pg_connection_locked()
            _PG_LAST_ERROR = str(exc)
            if attempt == 1:
                raise


def _create_fresh_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _seed_defaults_locked(conn: sqlite3.Connection) -> None:
    now_ist = timestamp_ist_str()
    conn.executemany(
        """
        INSERT OR IGNORE INTO membership_plans
            (plan_id, plan_name, duration_days, price, max_freeze_days, description, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        PLAN_SEED_ROWS,
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO gym_settings (setting_key, setting_value, updated_at_ist)
        VALUES (?, ?, ?)
        """,
        [(k, v, now_ist) for k, v in DEFAULT_GYM_SETTINGS.items()],
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO workflow_registry (
            workflow_code, name, description, trigger_type, cron_expression,
            timezone, webhook_path, n8n_workflow_id, is_enabled, updated_at_ist
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(*row, now_ist) for row in WORKFLOW_SEED_ROWS],
    )
    for wf_row in WORKFLOW_SEED_ROWS:
        conn.execute(
            """
            UPDATE workflow_registry
            SET n8n_workflow_id = ?, description = ?, updated_at_ist = ?
            WHERE workflow_code = ? AND n8n_workflow_id = ''
            """,
            (wf_row[7], wf_row[2], now_ist, wf_row[0]),
        )
    conn.commit()


def _hydrate_conn_from_dict_locked(conn: sqlite3.Connection, workbook_data: dict[str, Any]) -> None:
    tabs = workbook_data.get("tabs") if isinstance(workbook_data.get("tabs"), dict) else workbook_data
    if not isinstance(tabs, dict):
        return

    now_str = timestamp_ist_str()
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        for _, table_name in reversed(TAB_TABLE_ORDER):
            tab_key = next(
                (k for k in tabs if k.lower() == next(t.lower() for t, tbl in TAB_TABLE_ORDER if tbl == table_name)),
                None,
            )
            if tab_key is not None and isinstance(tabs[tab_key], list):
                conn.execute(f"DELETE FROM {table_name}")

        for tab_name, table_name in TAB_TABLE_ORDER:
            matching_key = next((k for k in tabs if k.lower() == tab_name.lower()), None)
            if matching_key is None or not isinstance(tabs[matching_key], list):
                continue
            rows = tabs[matching_key]
            if not rows:
                continue

            col_info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            valid_cols = [c["name"] for c in col_info]
            not_null_cols = {c["name"] for c in col_info if c["notnull"] and c["dflt_value"] is None}

            for raw_row in rows:
                if not isinstance(raw_row, dict):
                    continue
                filtered: dict[str, Any] = {}
                for col in valid_cols:
                    if col in raw_row:
                        val = raw_row[col]
                        if isinstance(val, (dict, list)):
                            val = json.dumps(val, ensure_ascii=False)
                        filtered[col] = val
                    elif col in not_null_cols:
                        if col in ("created_at_ist", "updated_at_ist", "dispatched_at_ist", "checkin_time_ist"):
                            filtered[col] = now_str
                        elif col == "last_payment_date":
                            filtered[col] = ""
                        else:
                            filtered[col] = ""
                if not filtered:
                    continue
                cols_sql = ", ".join(filtered.keys())
                placeholders = ", ".join("?" for _ in filtered)
                conn.execute(
                    f"INSERT OR REPLACE INTO {table_name} ({cols_sql}) VALUES ({placeholders})",
                    list(filtered.values()),
                )
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _build_snapshot_locked(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = get_settings()
    tabs: dict[str, list[dict[str, Any]]] = {}
    for tab_name, table_name in TAB_TABLE_ORDER:
        rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()
        tabs[tab_name] = [dict(r) for r in rows]

    return {
        "storage_backend": "postgresql_master" if _is_postgres_enabled() else settings.storage_backend,
        "master_database": "postgresql" if _is_postgres_enabled() else "google_sheets_workbook",
        "google_sheets_mode": settings.google_sheets_mode,
        "spreadsheet_id": settings.google_sheets_spreadsheet_id,
        "spreadsheet_name": settings.google_sheets_spreadsheet_name,
        "workbook_path": str(_resolved_workbook_path()),
        "updated_at_ist": timestamp_ist_str(),
        "counts": {tab: len(rows) for tab, rows in tabs.items()},
        "tabs": tabs,
    }


def _save_workbook_locked(conn: sqlite3.Connection) -> dict[str, Any]:
    snapshot = _build_snapshot_locked(conn)
    wb_path = _resolved_workbook_path()
    wb_path.parent.mkdir(parents=True, exist_ok=True)
    wb_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    return snapshot


def _ensure_store_locked() -> sqlite3.Connection:
    global _MEM_CONN, _ACTIVE_STORE_KEY, _PG_LAST_ERROR
    wb_path = _resolved_workbook_path()
    dsn = _configured_database_url()
    store_key = (dsn, str(wb_path))

    if _MEM_CONN is None or _ACTIVE_STORE_KEY != store_key:
        if _MEM_CONN is not None:
            _MEM_CONN.close()
        _close_pg_connection_locked()
        _MEM_CONN = _create_fresh_memory_conn()
        _ACTIVE_STORE_KEY = store_key

        if _is_postgres_enabled():
            try:
                pg_tabs = _fetch_all_from_pg_locked()
                _hydrate_conn_from_dict_locked(_MEM_CONN, {"tabs": pg_tabs})
                changes_before = _MEM_CONN.total_changes
                _seed_defaults_locked(_MEM_CONN)
                if _MEM_CONN.total_changes > changes_before:
                    _sync_to_master_postgres_locked(_MEM_CONN)
                _save_workbook_locked(_MEM_CONN)
                return _MEM_CONN
            except Exception as exc:
                _PG_LAST_ERROR = str(exc)

        if wb_path.exists():
            try:
                loaded = json.loads(wb_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    _hydrate_conn_from_dict_locked(_MEM_CONN, loaded)
            except Exception:
                pass
        _seed_defaults_locked(_MEM_CONN)
        _save_workbook_locked(_MEM_CONN)
    return _MEM_CONN


def reset_sheets_memory_store() -> None:
    """Clear the active store mirror and re-seed from defaults."""
    global _MEM_CONN, _ACTIVE_STORE_KEY
    with _STORE_LOCK:
        if _MEM_CONN is not None:
            _MEM_CONN.close()
        _close_pg_connection_locked()
        _MEM_CONN = _create_fresh_memory_conn()
        _ACTIVE_STORE_KEY = (_configured_database_url(), str(_resolved_workbook_path()))
        _seed_defaults_locked(_MEM_CONN)
        _save_workbook_locked(_MEM_CONN)


def pull_from_master_postgres() -> dict[str, Any]:
    """Re-hydrate the runtime mirror and Google Sheets presentation directly from Render PostgreSQL."""
    with _STORE_LOCK:
        conn = _ensure_store_locked()
        if not _is_postgres_enabled():
            return {"refreshed": False, "reason": "DATABASE_URL not configured", "snapshot": _build_snapshot_locked(conn)}
        pg_tabs = _fetch_all_from_pg_locked()
        _hydrate_conn_from_dict_locked(conn, {"tabs": pg_tabs})
        changes_before = conn.total_changes
        _seed_defaults_locked(conn)
        if conn.total_changes > changes_before:
            _sync_to_master_postgres_locked(conn)
        snap = _save_workbook_locked(conn)
        return {"refreshed": True, "counts": snap["counts"], "updated_at_ist": snap["updated_at_ist"]}


def get_master_db_status() -> dict[str, Any]:
    """Return master Render PostgreSQL health, sanitized connection info, and table counts."""
    with _STORE_LOCK:
        conn = _ensure_store_locked()
        snap = _build_snapshot_locked(conn)

    dsn = _configured_database_url()
    parsed = urlparse(dsn) if dsn else None
    pg_enabled = _is_postgres_enabled()
    live_ping_ok = False
    ping_err = _PG_LAST_ERROR
    if pg_enabled:
        try:
            with _STORE_LOCK:
                pg_conn = _get_pg_connection_locked()
                with pg_conn.cursor() as cur:
                    cur.execute("SELECT 1")
                pg_conn.commit()
                live_ping_ok = True
                ping_err = ""
        except Exception as exc:
            live_ping_ok = False
            ping_err = str(exc)

    return {
        "master_engine": "PostgreSQL (Render)" if pg_enabled else "Google Sheets Workbook Mirror",
        "postgresql_configured": pg_enabled,
        "postgresql_connected": live_ping_ok,
        "host": parsed.hostname if parsed else "",
        "database": parsed.path.lstrip("/") if parsed else "",
        "write_pipeline": (
            "1. FastAPI Automation -> 2. Master PostgreSQL (Render) ACID Commit -> 3. Executive Google Sheets / Excel Projection"
            if pg_enabled
            else "1. FastAPI Automation -> 2. Google Sheets Workbook"
        ),
        "last_pg_sync_ist": _PG_LAST_SYNC_IST or snap["updated_at_ist"],
        "last_error": ping_err,
        "counts": snap["counts"],
    }


def get_sheets_workbook_snapshot() -> dict[str, Any]:
    """Return all tabs from the active master-backed Google Sheets snapshot."""
    with _STORE_LOCK:
        conn = _ensure_store_locked()
        return _build_snapshot_locked(conn)


def save_sheets_workbook_snapshot() -> dict[str, Any]:
    """Flush the active snapshot to Master PostgreSQL (if configured) and GOOGLE_SHEETS_WORKBOOK_PATH."""
    with _STORE_LOCK:
        conn = _ensure_store_locked()
        if _is_postgres_enabled():
            _sync_to_master_postgres_locked(conn)
        return _save_workbook_locked(conn)


def load_sheets_workbook_snapshot(workbook_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Hydrate tables from a snapshot, commit to Master PostgreSQL first, and flush workbook."""
    with _STORE_LOCK:
        conn = _ensure_store_locked()
        if workbook_data is None:
            wb_path = _resolved_workbook_path()
            if wb_path.exists():
                workbook_data = json.loads(wb_path.read_text(encoding="utf-8"))
            else:
                workbook_data = {}
        _hydrate_conn_from_dict_locked(conn, workbook_data)
        _seed_defaults_locked(conn)
        if _is_postgres_enabled():
            _sync_to_master_postgres_locked(conn)
        return _save_workbook_locked(conn)


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Yield a transactional cursor; commits writes to Master PostgreSQL FIRST, then updates Sheets."""
    global _TX_DEPTH
    with _STORE_LOCK:
        conn = _ensure_store_locked()
        changes_before = conn.total_changes
        _TX_DEPTH += 1
        try:
            yield conn
            conn.commit()
            if _TX_DEPTH == 1 and conn.total_changes > changes_before:
                if _is_postgres_enabled():
                    _sync_to_master_postgres_locked(conn)
                _save_workbook_locked(conn)
        except Exception:
            conn.rollback()
            raise
        finally:
            _TX_DEPTH = max(0, _TX_DEPTH - 1)


def init_db() -> None:
    """Initialize Master PostgreSQL (if configured), seed defaults, and flush executive snapshot."""
    global _PG_LAST_ERROR
    with _STORE_LOCK:
        conn = _ensure_store_locked()
        changes_before = conn.total_changes
        _seed_defaults_locked(conn)
        if _is_postgres_enabled() and (conn.total_changes > changes_before or not _PG_LAST_SYNC_IST):
            try:
                _sync_to_master_postgres_locked(conn)
            except Exception as exc:
                _PG_LAST_ERROR = str(exc)
        _save_workbook_locked(conn)
