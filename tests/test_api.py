"""Comprehensive 27-test end-to-end pytest suite for Crush Lifestyle Fitness Studio.

Uses an in-process ASGI runner (AsgiTestClient) so all tests execute cleanly without
opening loopback sockets.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

from app.config import get_settings, now_ist, today_ist_str
from app.database import init_db, reset_sheets_memory_store
from app.main import create_app


class _InlineEventLoop(asyncio.AbstractEventLoop):
    """Socket-free, IOCP-free pure-Python event loop for in-process ASGI tests on Windows."""

    def __init__(self) -> None:
        self._ready: collections.deque[asyncio.Handle] = collections.deque()
        self._scheduled: list[tuple[float, asyncio.TimerHandle]] = []
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._running = False
        self._closed = False
        self._task_factory = None

    def get_debug(self) -> bool:
        return False

    def time(self) -> float:
        return time.monotonic()

    def is_running(self) -> bool:
        return self._running

    def is_closed(self) -> bool:
        return self._closed

    def create_future(self) -> asyncio.Future:
        return asyncio.Future(loop=self)

    def create_task(self, coro, *, name=None, context=None) -> asyncio.Task:
        if self._task_factory is not None:
            return self._task_factory(self, coro, context=context)
        return asyncio.Task(coro, loop=self, name=name, context=context)

    def set_task_factory(self, factory) -> None:
        self._task_factory = factory

    def get_task_factory(self):
        return self._task_factory

    def call_soon(self, callback, *args, context=None) -> asyncio.Handle:
        handle = asyncio.Handle(callback, args, self, context=context)
        with self._cond:
            self._ready.append(handle)
            self._cond.notify_all()
        return handle

    def call_soon_threadsafe(self, callback, *args, context=None) -> asyncio.Handle:
        return self.call_soon(callback, *args, context=context)

    def call_later(self, delay, callback, *args, context=None) -> asyncio.TimerHandle:
        return self.call_at(self.time() + float(delay), callback, *args, context=context)

    def call_at(self, when, callback, *args, context=None) -> asyncio.TimerHandle:
        handle = asyncio.TimerHandle(when, callback, args, self, context=context)
        with self._cond:
            self._scheduled.append((float(when), handle))
            self._scheduled.sort(key=lambda item: item[0])
            self._cond.notify_all()
        return handle

    def run_in_executor(self, executor, func, *args) -> asyncio.Future:
        fut = self.create_future()
        try:
            fut.set_result(func(*args))
        except BaseException as exc:
            fut.set_exception(exc)
        return fut

    def call_exception_handler(self, context: dict[str, Any]) -> None:
        pass

    async def shutdown_asyncgens(self) -> None:
        return None

    async def shutdown_default_executor(self, timeout=None) -> None:
        return None

    def stop(self) -> None:
        self._running = False

    def close(self) -> None:
        self._closed = True

    def run_until_complete(self, future):
        prev_running = asyncio._get_running_loop()
        asyncio.set_event_loop(self)
        asyncio._set_running_loop(self)
        self._running = True
        try:
            fut = asyncio.ensure_future(future, loop=self)
            deadline = time.monotonic() + 15.0
            while not fut.done():
                if time.monotonic() > deadline:
                    raise TimeoutError("In-process ASGI test exceeded 15s deadline.")
                with self._cond:
                    now = self.time()
                    while self._scheduled and self._scheduled[0][0] <= now:
                        _, th = self._scheduled.pop(0)
                        if not th.cancelled():
                            self._ready.append(th)
                    if self._ready:
                        handle = self._ready.popleft()
                    else:
                        handle = None
                        wait_s = 0.005
                        if self._scheduled:
                            wait_s = max(0.0005, min(0.01, self._scheduled[0][0] - now))
                        self._cond.wait(timeout=wait_s)
                if handle is not None and not handle.cancelled():
                    handle._run()
            return fut.result()
        finally:
            self._running = False
            asyncio._set_running_loop(prev_running)
            asyncio.set_event_loop(None)


def _run_inline(coro: Any) -> Any:
    loop = _InlineEventLoop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class AsgiTestClient:
    """In-process ASGI HTTP client compatible with httpx.Response without loopback sockets."""

    def __init__(self, asgi_app: Any):
        self.app = asgi_app

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        parsed = urlsplit(url)
        body_bytes = b""
        req_headers = dict(headers or {})
        if json is not None:
            body_bytes = json_dumps(json)
            req_headers.setdefault("content-type", "application/json")
        elif content is not None:
            body_bytes = content.encode("utf-8") if isinstance(content, str) else content

        raw_headers: list[tuple[bytes, bytes]] = [
            (k.lower().encode("latin-1"), str(v).encode("latin-1"))
            for k, v in req_headers.items()
        ]
        raw_headers.append((b"host", b"testserver"))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": parsed.path or "/",
            "raw_path": (parsed.path or "/").encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }

        async def _call_asgi() -> httpx.Response:
            sent_request = False
            status_code = 500
            resp_headers: list[tuple[bytes, bytes]] = []
            resp_chunks: list[bytes] = []

            async def receive() -> dict[str, Any]:
                nonlocal sent_request
                if not sent_request:
                    sent_request = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                nonlocal status_code, resp_headers
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                    resp_headers = list(message.get("headers") or [])
                elif message["type"] == "http.response.body":
                    chunk = message.get("body") or b""
                    if chunk:
                        resp_chunks.append(chunk)

            await self.app(scope, receive, send)
            decoded_headers = [
                (k.decode("latin-1"), v.decode("latin-1"))
                for k, v in resp_headers
            ]
            req = httpx.Request(method.upper(), f"http://testserver{url}")
            return httpx.Response(
                status_code=status_code,
                headers=decoded_headers,
                content=b"".join(resp_chunks),
                request=req,
            )

        return _run_inline(_call_asgi())

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        return self.request("GET", url, headers=headers)

    def post(
        self,
        url: str,
        *,
        json: Any = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.request("POST", url, json=json, content=content, headers=headers)

    def patch(
        self,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.request("PATCH", url, json=json, headers=headers)

    def put(
        self,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.request("PUT", url, json=json, headers=headers)

    def delete(self, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        return self.request("DELETE", url, headers=headers)


def json_dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _offset_ist_date(days: int) -> str:
    return (now_ist().date() + timedelta(days=days)).isoformat()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsgiTestClient:
    workbook_file = tmp_path / "sheets_workbook.json"
    db_file = tmp_path / "should_not_be_created.db"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("GYMBRO_DATABASE_URL", "")
    monkeypatch.setenv("GYMBRO_STORAGE_BACKEND", "google_sheets")
    monkeypatch.setenv("GOOGLE_SHEETS_MODE", "n8n_bridge")
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "REPLACE_WITH_GOOGLE_SHEET_ID")
    monkeypatch.setenv("GOOGLE_SHEETS_WORKBOOK_PATH", str(workbook_file))
    monkeypatch.setenv("GYMBRO_DB_PATH", str(db_file))
    monkeypatch.setenv("GYMBRO_API_KEY", "")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test-rzp-secret")
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", "test-n8n-secret")
    monkeypatch.setenv("N8N_BASE_URL", "https://test-n8n.example.com")
    monkeypatch.setenv("N8N_API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    for var in (
        "GOOGLE_SHEETS_BRIDGE_WEBHOOK_URL",
        "N8N_GM01_SYNC_WEBHOOK_URL",
        "N8N_GM02_DISPATCH_WEBHOOK_URL",
        "N8N_GM03_PAYMENT_WEBHOOK_URL",
        "N8N_GM04_DIGEST_WEBHOOK_URL",
        "N8N_GM05_AGENT_WEBHOOK_URL",
    ):
        monkeypatch.setenv(var, "")

    get_settings.cache_clear()
    reset_sheets_memory_store()
    init_db()
    app = create_app()
    yield AsgiTestClient(app)
    get_settings.cache_clear()


def _create_member(
    client: AsgiTestClient,
    *,
    name: str = "Aarav Sharma",
    phone: str = "9876543210",
    plan: str = "Quarterly",
    fee: float = 4000,
    start_date: str | None = None,
    payment_status: str = "Paid",
) -> dict[str, Any]:
    res = client.post(
        "/api/v1/members",
        json={
            "name": name,
            "phone": phone,
            "plan": plan,
            "fee": fee,
            "start_date": start_date or today_ist_str(),
            "payment_status": payment_status,
            "sync_to_n8n": False,
        },
    )
    assert res.status_code == 201
    return res.json()


def test_01_health_and_system_info_headers(client: AsgiTestClient):
    home = client.get("/")
    assert home.status_code == 200
    assert "text/html" in home.headers.get("content-type", "")
    assert "Welcome to Gymbero your app is healthy" in home.text

    h = client.get("/health", headers={"X-Request-ID": "req-custom-001", "X-Tenant-ID": "crush-hyd-01"})
    assert h.status_code == 200
    assert h.json()["status"] == "ok"
    assert h.json()["timezone"] == "Asia/Kolkata"
    assert h.headers.get("X-Request-ID") == "req-custom-001"
    assert h.headers.get("X-Tenant-ID") == "crush-hyd-01"
    assert float(h.headers.get("X-Process-Time-Ms", "0")) >= 0.0

    info = client.get("/api/v1/system/info")
    assert info.status_code == 200
    body = info.json()
    assert body["workflows_count"] == 5
    assert body["routes_count"] >= 35


def test_02_env_checklist_and_sheets_status(client: AsgiTestClient):
    env_res = client.get("/api/v1/automations/n8n/env-checklist")
    assert env_res.status_code == 200
    checklist = env_res.json()
    assert checklist["ready_for_local_sheets_mode"] is True
    assert checklist["ready_for_live_n8n_deploy"] is False
    assert "N8N_API_KEY" in checklist["missing_for_live_n8n_deploy"]
    assert "GOOGLE_SHEETS_SPREADSHEET_ID" in checklist["missing_for_live_n8n_deploy"]

    st = client.get("/api/v1/sheets/status")
    assert st.status_code == 200
    assert st.json()["storage_backend"] == "google_sheets"
    assert st.json()["counts"]["Membership Plans"] == 4
    assert "Members" in st.json()["tabs"]


def test_03_member_registration_and_18_col_sheets_projection(client: AsgiTestClient):
    res = client.post(
        "/api/v1/members",
        json={
            "Full Name": "Rohit Kumar",
            "Phone Number": "+91 98765-43210",
            "Email": "Rohit@Example.com",
            "Gender": "Male",
            "Address": "Indiranagar, Bengaluru",
            "Emergency Contact Name": "Suman Kumar",
            "Emergency Contact Phone": "09811122334",
            "Membership Plan": "Quarterly",
            "Membership Fee": 4000,
            "Start Date": today_ist_str(),
            "Payment Status": "Paid",
            "sync_to_n8n": False,
        },
    )
    assert res.status_code == 201
    body = res.json()
    member = body["member"]
    assert member["member_id"].startswith("MEM-")
    assert len(member["member_id"]) == 12
    assert member["phone"] == "919876543210"
    assert member["emergency_contact_phone"] == "919811122334"
    assert member["email"] == "rohit@example.com"
    assert len(body["google_sheets_row"]) == 18
    assert body["payment_receipt"]["gst_amount"] > 0

    settings = get_settings()
    assert Path(settings.google_sheets_workbook_path).exists()
    assert not Path(settings.db_path).exists()


def test_04_member_phone_normalization_variants(client: AsgiTestClient):
    variants = ["9876543211", "09876543212", "+91-98765-43213", "919876543214"]
    expected = ["919876543211", "919876543212", "919876543213", "919876543214"]
    for raw_phone, exp in zip(variants, expected):
        created = _create_member(client, name="Phone Variant", phone=raw_phone, plan="Monthly", fee=1500)
        assert created["member"]["phone"] == exp


def test_05_member_registration_validation_errors(client: AsgiTestClient):
    bad_payloads = [
        {"name": "   ", "phone": "9876543210", "plan": "Monthly", "fee": 1500, "start_date": today_ist_str(), "payment_status": "Paid"},
        {"name": "Bad Phone", "phone": "5123456789", "plan": "Monthly", "fee": 1500, "start_date": today_ist_str(), "payment_status": "Paid"},
        {"name": "Neg Fee", "phone": "9876543210", "plan": "Monthly", "fee": -10, "start_date": today_ist_str(), "payment_status": "Paid"},
        {"name": "Future DOB", "phone": "9876543210", "date_of_birth": _offset_ist_date(10), "plan": "Monthly", "fee": 1500, "start_date": today_ist_str(), "payment_status": "Paid"},
        {"name": "Bad Email", "phone": "9876543210", "email": "not-an-email", "plan": "Monthly", "fee": 1500, "start_date": today_ist_str(), "payment_status": "Paid"},
        {"name": "Bad Gender", "phone": "9876543210", "gender": "Invalid", "plan": "Monthly", "fee": 1500, "start_date": today_ist_str(), "payment_status": "Paid"},
    ]
    for payload in bad_payloads:
        res = client.post("/api/v1/members", json=payload)
        assert res.status_code == 422


def test_06_member_360_profile_and_filtering(client: AsgiTestClient):
    m1 = _create_member(client, name="Ananya Rao", phone="9811111111", plan="Monthly", fee=1500, payment_status="Paid")
    _create_member(client, name="Vikram Malhotra", phone="9822222222", plan="Yearly", fee=13500, payment_status="Pending")

    all_res = client.get("/api/v1/members")
    assert all_res.status_code == 200
    assert all_res.json()["count"] == 2

    search_res = client.get("/api/v1/members?search=Ananya&plan=Monthly")
    assert search_res.status_code == 200
    assert search_res.json()["count"] == 1

    pending_res = client.get("/api/v1/members?member_status=Pending")
    assert pending_res.status_code == 200
    assert pending_res.json()["count"] == 1
    assert pending_res.json()["members"][0]["name"] == "Vikram Malhotra"

    prof = client.get(f"/api/v1/members/{m1['member']['member_id']}")
    assert prof.status_code == 200
    assert len(prof.json()["payments"]) == 1

    sheets_row = client.get(f"/api/v1/members/{m1['member']['member_id']}/sheets-row")
    assert sheets_row.status_code == 200
    assert len(sheets_row.json()) == 18


def test_07_member_profile_patch_update(client: AsgiTestClient):
    mid = _create_member(client, name="Karan Singh", phone="9876501234")["member"]["member_id"]
    upd = client.patch(
        f"/api/v1/members/{mid}",
        json={"assigned_trainer": "Coach Vikram", "batch_slot": "Morning 7AM", "email": "karan@gym.in"},
    )
    assert upd.status_code == 200
    m = upd.json()["member"]
    assert m["assigned_trainer"] == "Coach Vikram"
    assert m["batch_slot"] == "Morning 7AM"
    assert m["email"] == "karan@gym.in"


def test_08_membership_freeze_and_expiry_extension(client: AsgiTestClient):
    created = _create_member(client, name="Neha Sharma", phone="9123456789", plan="Quarterly", fee=4000)
    mid = created["member"]["member_id"]
    orig_expiry = date.fromisoformat(created["member"]["expiry_date"])

    frz = client.post(f"/api/v1/members/{mid}/freeze", json={"freeze_days": 5, "reason": "Medical travel"})
    assert frz.status_code == 200
    assert frz.json()["member"]["member_status"] == "Frozen"
    assert date.fromisoformat(frz.json()["new_expiry"]) == orig_expiry + timedelta(days=5)


def test_09_membership_freeze_quota_exceeded_error(client: AsgiTestClient):
    mid = _create_member(client, name="Monthly Freeze", phone="9876123456", plan="Monthly", fee=1500)["member"]["member_id"]
    res = client.post(f"/api/v1/members/{mid}/freeze", json={"freeze_days": 3, "reason": "Vacation"})
    assert res.status_code == 400
    assert "at most 0 freeze days" in res.json()["detail"]


def test_10_attendance_blocked_when_frozen(client: AsgiTestClient):
    mid = _create_member(client, name="Frozen Checkin", phone="9876123457", plan="Quarterly", fee=4000)["member"]["member_id"]
    client.post(f"/api/v1/members/{mid}/freeze", json={"freeze_days": 4, "reason": "Trip"})
    blocked = client.post("/api/v1/attendance/check-in", json={"member_id": mid})
    assert blocked.status_code == 400
    assert "frozen" in blocked.json()["detail"].lower()


def test_11_membership_early_unfreeze_reclaims_days(client: AsgiTestClient):
    created = _create_member(client, name="Early Resume", phone="9876123458", plan="Half-Yearly", fee=7500)
    mid = created["member"]["member_id"]
    orig_expiry = created["member"]["expiry_date"]

    client.post(f"/api/v1/members/{mid}/freeze", json={"freeze_days": 10, "reason": "Work travel"})
    unfrz = client.post(f"/api/v1/members/{mid}/unfreeze", json={"reason": "Returned early"})
    assert unfrz.status_code == 200
    assert unfrz.json()["reclaimed_freeze_days"] == 10
    assert unfrz.json()["new_expiry"] == orig_expiry
    assert unfrz.json()["member"]["member_status"] == "Active"


def test_12_attendance_checkin_streak_and_duplicate_guard(client: AsgiTestClient):
    mid = _create_member(client, name="Streak Member", phone="9876123459")["member"]["member_id"]
    ci1 = client.post("/api/v1/attendance/check-in", json={"phone": "9876123459", "workout_type": "HIIT / Burn"})
    assert ci1.status_code == 200
    assert ci1.json()["already_checked_in_today"] is False
    assert ci1.json()["current_streak"] == 1

    ci2 = client.post("/api/v1/attendance/check-in", json={"member_id": mid})
    assert ci2.status_code == 200
    assert ci2.json()["already_checked_in_today"] is True
    assert ci2.json()["current_streak"] == 1

    today_res = client.get("/api/v1/attendance/today")
    assert today_res.status_code == 200
    assert today_res.json()["count"] == 1


def test_13_attendance_history_and_streak_leaderboard(client: AsgiTestClient):
    m1 = _create_member(client, name="Sneha Nair", phone="9765432109", plan="Yearly", fee=13500)["member"]["member_id"]
    client.post("/api/v1/attendance/check-in", json={"member_id": m1, "workout_type": "Strength & Conditioning"})

    hist = client.get(f"/api/v1/attendance/history?member_id={m1}&workout_type=Strength%20%26%20Conditioning")
    assert hist.status_code == 200
    assert hist.json()["count"] == 1

    lb = client.get("/api/v1/attendance/leaderboard?limit=5")
    assert lb.status_code == 200
    assert lb.json()["leaderboard"][0]["member_id"] == m1
    assert lb.json()["leaderboard"][0]["rank"] == 1


def test_14_pending_dues_and_followup_priority_queue(client: AsgiTestClient):
    _create_member(
        client,
        name="Pending Dues Member",
        phone="9800011122",
        plan="Monthly",
        fee=1500,
        start_date=_offset_ist_date(-2),
        payment_status="Pending",
    )
    _create_member(
        client,
        name="Expiring Urgent Member",
        phone="9800011123",
        plan="Monthly",
        fee=1500,
        start_date=_offset_ist_date(-28),
        payment_status="Paid",
    )

    due = client.get("/api/v1/follow-ups/due")
    assert due.status_code == 200
    types = {item["followup_type"] for item in due.json()["follow_ups"]}
    assert "PENDING_DUES_RECOVERY" in types
    assert "EXPIRY_T3_URGENT" in types


def test_15_followup_dry_run_and_live_dispatch(client: AsgiTestClient):
    mid = _create_member(
        client,
        name="Dispatch Target",
        phone="9800022233",
        plan="Monthly",
        fee=1500,
        start_date=_offset_ist_date(-2),
        payment_status="Pending",
    )["member"]["member_id"]

    dry = client.post("/api/v1/follow-ups/dispatch", json={"dry_run": True})
    assert dry.status_code == 200
    assert dry.json()["dry_run"] is True
    assert dry.json()["would_dispatch_count"] >= 1

    live = client.post("/api/v1/follow-ups/dispatch", json={"member_ids": [mid], "dry_run": False})
    assert live.status_code == 200
    assert live.json()["dispatched_count"] == 1

    hist = client.get("/api/v1/follow-ups/history")
    assert hist.status_code == 200
    assert hist.json()["count"] >= 1


def test_16_n8n_inbound_member_and_followup_webhooks(client: AsgiTestClient):
    inbound = client.post(
        "/api/v1/webhooks/n8n/member-registered",
        json={
            "member_id": "MEM-FORM1234",
            "name": "Priya Nair",
            "phone": "9845012345",
            "plan": "Quarterly",
            "fee": 4000,
            "start_date": today_ist_str(),
            "payment_status": "Paid",
        },
    )
    assert inbound.status_code == 200
    assert inbound.json()["synced"] is True
    assert inbound.json()["member"]["phone"] == "919845012345"

    cb = client.post(
        "/api/v1/webhooks/n8n/followup-logged",
        json={
            "member_id": "MEM-FORM1234",
            "followup_type": "ONBOARDING_DAY_1_WELCOME",
            "status": "SENT",
            "message": "Welcome to Crush Studio!",
        },
    )
    assert cb.status_code == 200
    assert cb.json()["status"] == "SENT"


def test_17_direct_payment_renewal_and_idempotency(client: AsgiTestClient):
    created = _create_member(client, name="Renew Direct", phone="9876000011", plan="Monthly", fee=1500, payment_status="Paid")
    mid = created["member"]["member_id"]
    orig_expiry = created["member"]["expiry_date"]

    pay1 = client.post(
        "/api/v1/payments",
        json={"member_id": mid, "amount_paid": 1500, "payment_mode": "UPI", "transaction_ref": "UPI-REF-001"},
    )
    assert pay1.status_code == 200
    assert pay1.json()["already_processed"] is False
    assert pay1.json()["member"]["expiry_date"] > orig_expiry

    pay2 = client.post(
        "/api/v1/payments",
        json={"member_id": mid, "amount_paid": 1500, "payment_mode": "UPI", "transaction_ref": "UPI-REF-001"},
    )
    assert pay2.status_code == 200
    assert pay2.json()["already_processed"] is True


def test_18_member_renew_endpoint_with_plan_upgrade(client: AsgiTestClient):
    created = _create_member(client, name="Upgrade Member", phone="9876000022", plan="Monthly", fee=1500)
    mid = created["member"]["member_id"]

    renew = client.post(
        f"/api/v1/members/{mid}/renew",
        json={
            "plan": "Half-Yearly",
            "amount_paid": 7000,
            "payment_mode": "Card",
            "discount_amount": 500,
            "assigned_trainer": "Coach Meera",
            "sync_to_n8n": False,
        },
    )
    assert renew.status_code == 200
    assert renew.json()["member"]["plan"] == "Half-Yearly"
    assert renew.json()["member"]["assigned_trainer"] == "Coach Meera"
    assert renew.json()["member"]["discount_amount"] == 500.0


def test_19_razorpay_webhook_signature_and_idempotency(client: AsgiTestClient):
    mid = _create_member(client, name="Amit Verma", phone="9988776655", plan="Monthly", fee=1500, payment_status="Pending")["member"]["member_id"]
    payload = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {"id": "plink_TEST123", "reference_id": mid, "amount_paid": 150000}}},
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"test-rzp-secret", raw, hashlib.sha256).hexdigest()

    bad = client.post("/api/v1/payments/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": "bad"})
    assert bad.status_code == 401

    wh1 = client.post("/api/v1/payments/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": sig})
    assert wh1.status_code == 200
    assert wh1.json()["already_processed"] is False
    assert wh1.json()["member"]["payment_status"] == "Paid"

    wh2 = client.post("/api/v1/payments/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": sig})
    assert wh2.status_code == 200
    assert wh2.json()["already_processed"] is True


def test_20_gst_tax_invoice_and_payment_refund(client: AsgiTestClient):
    created = _create_member(client, name="Invoice Member", phone="9844455566", plan="Monthly", fee=1500, payment_status="Paid")
    payment_id = created["payment_receipt"]["payment_id"]

    inv_res = client.get(f"/api/v1/payments/{payment_id}/invoice")
    assert inv_res.status_code == 200
    inv = inv_res.json()
    assert inv["cgst_rate_pct"] == 9.0
    assert inv["sgst_rate_pct"] == 9.0
    assert round(inv["cgst_amount"] + inv["sgst_amount"], 2) == inv["total_gst"]
    assert round(inv["taxable_value"] + inv["total_gst"], 2) == inv["total_amount_paid"]

    ref = client.post(f"/api/v1/payments/{payment_id}/refund", json={"reason": "Duplicate charge"})
    assert ref.status_code == 200
    assert ref.json()["payment"]["status"] == "Refunded"

    dup_ref = client.post(f"/api/v1/payments/{payment_id}/refund", json={"reason": "Retry"})
    assert dup_ref.status_code == 400


def test_21_accounts_expenses_crud_pnl_and_receivables_aging(client: AsgiTestClient):
    _create_member(client, name="Paid User", phone="9877711100", plan="Quarterly", fee=4000, payment_status="Paid")
    _create_member(client, name="Pending 10d", phone="9877711101", plan="Monthly", fee=1500, start_date=_offset_ist_date(-10), payment_status="Pending")

    exp = client.post(
        "/api/v1/accounts/expenses",
        json={"category": "Rent", "amount": 1500, "vendor": "Studio Landlord", "payment_mode": "Bank Transfer"},
    )
    assert exp.status_code == 201
    exp_id = exp.json()["expense"]["expense_id"]

    recv = client.get("/api/v1/accounts/receivables")
    assert recv.status_code == 200
    assert recv.json()["aging_totals"]["8_30_days"] == 1500.0
    assert "wa.me/919877711101" in recv.json()["receivables"][0]["whatsapp_reminder_url"]

    summary = client.get("/api/v1/accounts/summary")
    assert summary.status_code == 200
    assert summary.json()["gross_collections"] == 4000.0
    assert summary.json()["total_expenses"] == 1500.0

    del_exp = client.delete(f"/api/v1/accounts/expenses/{exp_id}")
    assert del_exp.status_code == 200
    assert del_exp.json()["deleted"] is True


def test_22_ai_gym_manager_agent_tools_and_rbac(client: AsgiTestClient):
    reg = client.post(
        "/api/v1/agent/chat",
        json={"message": "Register Priya Mehta phone 9822334455 quarterly paid 4000", "actor_role": "owner", "sync_to_n8n": False},
    )
    assert reg.status_code == 200
    assert reg.json()["intent"] == "REGISTER_MEMBER"

    exp = client.post(
        "/api/v1/agent/chat",
        json={"message": "Record electricity expense Rs. 3500", "actor_role": "owner", "sync_to_n8n": False},
    )
    assert exp.status_code == 200
    assert exp.json()["intent"] == "RECORD_EXPENSE"

    lb = client.post(
        "/api/v1/agent/chat",
        json={"message": "Show workout streak leaderboard", "actor_role": "member", "actor_phone": "9822334455", "sync_to_n8n": False},
    )
    assert lb.status_code == 200
    assert lb.json()["intent"] == "LEADERBOARD"

    blocked = client.post(
        "/api/v1/agent/chat",
        json={"message": "Show accounts P&L", "actor_role": "member", "actor_phone": "9822334455", "sync_to_n8n": False},
    )
    assert blocked.status_code == 200
    assert "restricted" in blocked.json()["reply"].lower()

    auto = client.post("/api/v1/agent/autonomous-cycle")
    assert auto.status_code == 200
    assert auto.json()["intent"] == "AUTOPILOT"

    runs = client.get("/api/v1/agent/runs")
    assert runs.status_code == 200
    assert runs.json()["count"] >= 5


def test_23_plans_crud_gym_settings_and_member_archive_delete(client: AsgiTestClient):
    plan_res = client.post(
        "/api/v1/plans",
        json={"plan_name": "Transformation 45", "duration_days": 45, "price": 2500, "max_freeze_days": 3},
    )
    assert plan_res.status_code == 201
    plan_id = plan_res.json()["plan"]["plan_id"]

    upd_plan = client.patch(f"/api/v1/plans/{plan_id}", json={"price": 2800})
    assert upd_plan.status_code == 200
    assert upd_plan.json()["plan"]["price"] == 2800.0

    put_set = client.put("/api/v1/settings", json={"settings": {"branch_name": "Koramangala Studio"}})
    assert put_set.status_code == 200
    assert put_set.json()["settings"]["branch_name"] == "Koramangala Studio"

    mid = _create_member(client, name="Archive Target", phone="9877700099")["member"]["member_id"]
    soft = client.delete(f"/api/v1/members/{mid}")
    assert soft.status_code == 200
    assert soft.json()["deleted"] is False
    assert soft.json()["member"]["member_status"] == "Cancelled"

    hard = client.delete(f"/api/v1/members/{mid}?hard=true")
    assert hard.status_code == 200
    assert hard.json()["deleted"] is True
    assert client.get(f"/api/v1/members/{mid}").status_code == 404


def test_24_google_sheets_tabs_sync_push_pull_and_export(client: AsgiTestClient):
    append_m = client.post(
        "/api/v1/sheets/tabs/Members/append",
        json={
            "row": {
                "name": "Sheet Synced Member",
                "phone": "9812345670",
                "plan": "Monthly",
                "fee": 1500,
                "start_date": today_ist_str(),
                "payment_status": "Paid",
            }
        },
    )
    assert append_m.status_code == 201
    assert append_m.json()["row"]["phone"] == "919812345670"

    tab_m = client.get("/api/v1/sheets/tabs/Members")
    assert tab_m.status_code == 200
    assert tab_m.json()["count"] == 1

    push = client.post("/api/v1/sheets/sync/push")
    assert push.status_code == 200
    assert push.json()["counts"]["Members"] == 1
    assert Path(push.json()["workbook_path"]).exists()

    pull = client.post(
        "/api/v1/sheets/sync/pull",
        json={
            "workbook": {
                "tabs": {
                    "Gym Settings": [
                        {"setting_key": "gym_name", "setting_value": "Crush Koramangala Cloud", "updated_at_ist": "2026-11-16 12:00:00 IST"}
                    ]
                }
            }
        },
    )
    assert pull.status_code == 200
    assert client.get("/api/v1/settings").json()["settings"]["gym_name"] == "Crush Koramangala Cloud"

    export = client.get("/api/v1/sheets/workbook-export")
    assert export.status_code == 200
    for tab in ("Members", "Membership Plans", "Payments", "Attendance", "Automation Log", "Gym Settings"):
        assert tab in export.json()


def test_25_enterprise_n8n_workflows_trigger_and_schedules(client: AsgiTestClient):
    wf_upd = client.patch("/api/v1/automations/workflows/GM-02", json={"cron_expression": "30 8 * * *"})
    assert wf_upd.status_code == 200
    assert wf_upd.json()["workflow"]["cron_expression"] == "30 8 * * *"

    trig = client.post(
        "/api/v1/automations/workflows/GM-04/trigger",
        json={"triggered_by": "NextJS_Dashboard", "execute_local_pipeline": True, "dispatch_n8n_webhook": True},
    )
    assert trig.status_code == 200
    log_id = trig.json()["n8n_webhook"]["log_id"]

    sched = client.post(
        "/api/v1/automations/schedules",
        json={"workflow_code": "GM-02", "job_name": "Daily Morning Retention", "schedule_type": "cron", "cron_expression": "0 9 * * *"},
    )
    assert sched.status_code == 201
    job_id = sched.json()["schedule"]["job_id"]

    assert client.patch(f"/api/v1/automations/schedules/{job_id}", json={"status": "PAUSED"}).json()["schedule"]["status"] == "PAUSED"
    assert client.patch(f"/api/v1/automations/schedules/{job_id}", json={"status": "ACTIVE"}).json()["schedule"]["status"] == "ACTIVE"
    assert client.post(f"/api/v1/automations/schedules/{job_id}/run-now").json()["schedule"]["run_count"] == 1

    client.post(
        "/api/v1/automations/schedules",
        json={"workflow_code": "GM-05", "job_name": "Due Job", "schedule_type": "one_time", "run_at_ist": "2020-01-01 00:00:00 IST", "payload": {"message": "Show accounts P&L"}},
    )
    assert client.post("/api/v1/automations/schedules/tick").json()["executed_count"] >= 1
    assert client.post(f"/api/v1/automations/logs/{log_id}/retry").json()["retried_log_id"] == log_id

    cb_payload = {"workflow_code": "GM-02", "execution_id": "exec-99", "status": "SUCCESS", "summary": "Sent 5 messages"}
    raw_cb = json.dumps(cb_payload).encode("utf-8")
    cb_sig = hmac.new(b"test-n8n-secret", raw_cb, hashlib.sha256).hexdigest()

    assert client.post("/api/v1/webhooks/n8n/workflow-callback", content=raw_cb, headers={"X-Crush-Signature": "wrong"}).status_code == 401
    cb_ok = client.post("/api/v1/webhooks/n8n/workflow-callback", content=raw_cb, headers={"X-Crush-Signature": cb_sig})
    assert cb_ok.status_code == 200
    assert cb_ok.json()["acknowledged"] is True


def test_26_automatic_n8n_workflow_generator_and_deployer(client: AsgiTestClient):
    gen = client.post(
        "/api/v1/automations/n8n/generate",
        json={
            "spreadsheet_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz",
            "fastapi_url": "https://api.crushfitness.in",
            "whatsapp_phone_number_id": "998877665544332",
        },
    )
    assert gen.status_code == 200
    body = gen.json()
    assert body["count"] == 5
    gm01_str = json.dumps(body["workflows"]["GM-01"])
    gm02_str = json.dumps(body["workflows"]["GM-02"])
    gm05_str = json.dumps(body["workflows"]["GM-05"])
    assert "1AbCdEfGhIjKlMnOpQrStUvWxYz" in gm01_str
    assert "REPLACE_WITH_GOOGLE_SHEET_ID" not in gm01_str
    assert "n8n-nodes-base.gmail" in gm02_str
    assert "1AbCdEfGhIjKlMnOpQrStUvWxYz" in gm02_str
    assert "https://api.crushfitness.in" in gm05_str

    deploy_plan = client.post(
        "/api/v1/automations/n8n/deploy",
        json={"activate": True, "spreadsheet_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz"},
    )
    assert deploy_plan.status_code == 200
    assert deploy_plan.json()["mode"] == "dry_run"
    assert deploy_plan.json()["planned_count"] == 5


def test_27_api_key_authentication_guard(client: AsgiTestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GYMBRO_API_KEY", "enterprise-secret-key")
    get_settings.cache_clear()

    unauth = client.get("/api/v1/members")
    assert unauth.status_code == 401
    assert "X-Request-ID" in unauth.headers

    auth = client.get("/api/v1/members", headers={"X-API-Key": "enterprise-secret-key"})
    assert auth.status_code == 200

    health_open = client.get("/health")
    assert health_open.status_code == 200


def test_28_docker_and_railway_deployment_artifacts():
    backend_root = Path(__file__).resolve().parents[1]
    required_files = [
        backend_root / "Dockerfile",
        backend_root / "railway.json",
        backend_root / "docker-compose.yml",
        backend_root / ".dockerignore",
        backend_root / ".gitignore",
        backend_root / "README.md",
        backend_root / "workflows" / "GM-01-member-registration.json",
        backend_root / "workflows" / "GM-02-expiry-and-followup-cadence.json",
        backend_root / "workflows" / "GM-03-payment-and-renewal-webhook.json",
        backend_root / "workflows" / "GM-04-attendance-streak-and-owner-digest.json",
        backend_root / "workflows" / "GM-05-ai-gym-manager-agent.json",
    ]
    for path in required_files:
        assert path.exists(), f"Missing backend deployment artifact: {path}"

    rj = json.loads((backend_root / "railway.json").read_text(encoding="utf-8"))
    assert rj["build"]["builder"] == "DOCKERFILE"
    assert rj["build"]["dockerfilePath"] == "Dockerfile"
    assert rj["deploy"]["healthcheckPath"] == "/health"

    df_text = (backend_root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY workflows /app/workflows" in df_text
    assert "COPY app /app/app" in df_text
    assert "uvicorn app.main:app" in df_text
    assert "/health" in df_text
    assert "USER appuser" in df_text

    dockerignore_text = (backend_root / ".dockerignore").read_text(encoding="utf-8")
    gitignore_text = (backend_root / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore_text
    assert ".env" in gitignore_text


def test_29_master_db_trends_and_executive_sheets_presentation(client: AsgiTestClient):
    _create_member(client, name="Rohan Mehta", phone="9876543219", plan="Quarterly", fee=4000, payment_status="Paid")
    _create_member(client, name="Neha Verma", phone="9811122233", plan="Monthly", fee=1500, start_date=_offset_ist_date(-10), payment_status="Pending")

    master = client.get("/api/v1/database/master-status")
    assert master.status_code == 200
    assert "write_pipeline" in master.json()
    assert master.json()["counts"]["Members"] == 2

    trends = client.get("/api/v1/analytics/trends")
    assert trends.status_code == 200
    tbody = trends.json()
    assert 0 <= tbody["studio_health_score"] <= 100
    assert tbody["summary_counts"]["total_members"] == 2
    assert tbody["summary_counts"]["pending_dues_count"] == 1
    assert len(tbody["automated_insights"]) >= 1

    exec_view = client.get("/api/v1/sheets/executive-view")
    assert exec_view.status_code == 200
    ev = exec_view.json()
    assert "Executive Summary & KPIs" in ev["tabs"]
    assert "Customer Master Directory" in ev["tabs"]
    assert len(ev["tabs"]["Customer Master Directory"]) == 2
    assert "Automated Trends & Cohort Insights" in ev["tabs"]

    pipeline = client.post("/api/v1/automations/master-sync-and-analyze")
    assert pipeline.status_code == 200
    pbody = pipeline.json()
    assert "master_database" in pbody
    assert "trends" in pbody
    assert pbody["executive_sheets_counts"]["Customer Master Directory"] == 2
