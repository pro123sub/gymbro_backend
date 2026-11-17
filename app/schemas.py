"""Pydantic v2 request and response schemas for Crush Lifestyle Fitness Studio."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import today_ist_str

PLAN_DURATIONS: dict[str, int] = {
    "Monthly": 30,
    "Quarterly": 90,
    "Half-Yearly": 180,
    "Yearly": 365,
}
VALID_GENDERS = {"Male", "Female", "Other", "Prefer not to say"}
VALID_PAYMENT_STATUSES = {"Paid", "Pending"}
VALID_WORKOUT_TYPES = {
    "Gym Floor",
    "Strength & Conditioning",
    "HIIT / Burn",
    "Yoga / Mobility",
    "Personal Training",
}


def normalize_indian_mobile(raw: str) -> str:
    """Normalize an Indian mobile number into canonical 91XXXXXXXXXX format."""
    cleaned = re.sub(r"\D", "", str(raw or "").strip())
    if len(cleaned) == 12 and cleaned.startswith("91"):
        cleaned = cleaned[2:]
    elif len(cleaned) == 11 and cleaned.startswith("0"):
        cleaned = cleaned[1:]
    if not re.fullmatch(r"[6-9]\d{9}", cleaned):
        raise ValueError(
            "Phone number must be a valid Indian mobile number (10 digits starting with 6-9, optionally prefixed with +91, 91, or 0)."
        )
    return f"91{cleaned}"


def validate_iso_date(raw: str, field_name: str) -> str:
    """Validate YYYY-MM-DD calendar date string."""
    val = str(raw or "").strip()[:10]
    try:
        date.fromisoformat(val)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid calendar date in YYYY-MM-DD format.") from exc
    return val


class MemberCreateRequest(BaseModel):
    """Member registration payload (supports both API snake_case and n8n Form Trigger labels)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(..., alias="Full Name", min_length=1)
    phone: str = Field(..., alias="Phone Number")
    email: str = Field(default="", alias="Email")
    gender: str = Field(default="", alias="Gender")
    date_of_birth: str = Field(default="", alias="Date of Birth")
    address: str = Field(default="", alias="Address")
    emergency_contact_name: str = Field(default="", alias="Emergency Contact Name")
    emergency_contact_phone: str = Field(default="", alias="Emergency Contact Phone")
    plan: Literal["Monthly", "Quarterly", "Half-Yearly", "Yearly"] = Field(..., alias="Membership Plan")
    fee: float = Field(..., alias="Membership Fee", ge=0)
    start_date: str = Field(..., alias="Start Date")
    payment_status: Literal["Paid", "Pending"] = Field(..., alias="Payment Status")
    notes: str = Field(default="", alias="Notes")

    # Cult.fit-grade extensions
    fitness_goal: str = Field(default="General Fitness")
    batch_slot: str = Field(default="Flexible")
    assigned_trainer: str = Field(default="Unassigned")
    medical_notes: str = Field(default="")
    discount_amount: float = Field(default=0.0, ge=0)
    payment_mode: str = Field(default="UPI")
    sync_to_n8n: bool = Field(default=True)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Full Name cannot be empty.")
        return trimmed

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        return normalize_indian_mobile(v)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        trimmed = v.strip().lower()
        if trimmed and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", trimmed):
            raise ValueError("Email must be a valid email address.")
        return trimmed

    @field_validator("gender")
    @classmethod
    def _validate_gender(cls, v: str) -> str:
        trimmed = v.strip()
        if trimmed and trimmed not in VALID_GENDERS:
            raise ValueError(f"Gender must be one of: {', '.join(sorted(VALID_GENDERS))}.")
        return trimmed

    @field_validator("date_of_birth")
    @classmethod
    def _validate_dob(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            return ""
        parsed = validate_iso_date(trimmed, "Date of Birth")
        if parsed > today_ist_str():
            raise ValueError("Date of Birth cannot be in the future.")
        return parsed

    @field_validator("emergency_contact_phone")
    @classmethod
    def _validate_emergency_phone(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            return ""
        return normalize_indian_mobile(trimmed)

    @field_validator("start_date")
    @classmethod
    def _validate_start_date(cls, v: str) -> str:
        return validate_iso_date(v, "Start Date")

    @field_validator("address", "emergency_contact_name", "notes", "fitness_goal", "batch_slot", "assigned_trainer", "medical_notes")
    @classmethod
    def _trim_strings(cls, v: str) -> str:
        return str(v or "").strip()


class MemberResponse(BaseModel):
    """Canonical member record matching the 18 Google Sheets columns plus SaaS extensions."""

    member_id: str
    name: str
    phone: str
    email: str = ""
    gender: str = ""
    date_of_birth: str = ""
    address: str = ""
    emergency_contact_name: str = ""
    emergency_contact_phone: str = ""
    plan: str
    fee: float
    start_date: str
    expiry_date: str
    payment_status: str
    last_payment_date: str = ""
    last_reminder_date: str = ""
    member_status: str
    notes: str = ""

    fitness_goal: str = "General Fitness"
    batch_slot: str = "Flexible"
    assigned_trainer: str = "Unassigned"
    medical_notes: str = ""
    discount_amount: float = 0.0
    current_streak: int = 0
    longest_streak: int = 0
    last_checkin_date: str = ""
    total_checkins: int = 0
    freeze_days_used: int = 0
    frozen_until: str = ""
    days_remaining: int = 0
    next_followup_type: str | None = None
    next_followup_due: str | None = None


class FreezeMembershipRequest(BaseModel):
    """Request to freeze/pause an active membership (Cult.fit style)."""

    freeze_days: int = Field(..., ge=1, le=60)
    reason: str = Field(default="Travel / Medical Pause", min_length=2)
    start_date: str | None = None

    @field_validator("start_date")
    @classmethod
    def _validate_freeze_start(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_iso_date(v, "start_date")


class PaymentRecordRequest(BaseModel):
    """Record a payment or renewal for an existing member."""

    member_id: str = Field(..., min_length=4)
    amount_paid: float = Field(..., gt=0)
    payment_mode: Literal["UPI", "Cash", "Card", "Razorpay", "Bank Transfer"] = "UPI"
    transaction_ref: str = Field(default="")
    plan: Literal["Monthly", "Quarterly", "Half-Yearly", "Yearly"] | None = None
    extend_membership: bool = True
    notes: str = ""


class AttendanceCheckInRequest(BaseModel):
    """Check in a member using either member_id or phone number."""

    member_id: str | None = None
    phone: str | None = None
    workout_type: str = Field(default="Gym Floor")
    batch_slot: str = Field(default="Flexible")

    @field_validator("workout_type")
    @classmethod
    def _validate_workout(cls, v: str) -> str:
        trimmed = v.strip() or "Gym Floor"
        if trimmed not in VALID_WORKOUT_TYPES:
            raise ValueError(f"workout_type must be one of: {', '.join(sorted(VALID_WORKOUT_TYPES))}.")
        return trimmed


class FollowUpItem(BaseModel):
    """Prioritized Cult.fit-style retention / follow-up task."""

    member_id: str
    name: str
    phone: str
    plan: str
    expiry_date: str
    payment_status: str
    member_status: str
    followup_type: str
    priority: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    channel: str = "Email"
    template_name: str
    message_text: str
    days_until_expiry: int
    days_since_last_checkin: int | None = None


class FollowUpDispatchRequest(BaseModel):
    """Optional filter for dispatching due follow-ups to n8n."""

    member_ids: list[str] | None = None
    followup_types: list[str] | None = None
    dry_run: bool = False


class N8NMemberSyncPayload(BaseModel):
    """Inbound payload sent from n8n GM-01 after form submission."""

    model_config = ConfigDict(extra="allow")

    member_id: str | None = None
    name: str
    phone: str
    email: str = ""
    gender: str = ""
    date_of_birth: str = ""
    address: str = ""
    emergency_contact_name: str = ""
    emergency_contact_phone: str = ""
    plan: Literal["Monthly", "Quarterly", "Half-Yearly", "Yearly"]
    fee: float = Field(..., ge=0)
    start_date: str
    expiry_date: str | None = None
    payment_status: Literal["Paid", "Pending"]
    last_payment_date: str = ""
    last_reminder_date: str = ""
    member_status: str = "Active"
    notes: str = ""


class N8NFollowUpLogPayload(BaseModel):
    """Inbound callback from n8n GM-02 confirming reminder dispatch."""

    member_id: str
    followup_type: str
    channel: str = "Email"
    status: str = "SENT"
    message: str = ""
    last_reminder_date: str | None = None
    new_member_status: str | None = None


class DashboardAnalyticsResponse(BaseModel):
    """Executive studio KPIs for Cult.fit-grade owner console."""

    as_of_ist: str
    total_members: int
    active_members: int
    expiring_in_7_days: int
    expired_members: int
    frozen_members: int
    pending_dues_count: int
    pending_dues_amount: float
    total_revenue_collected: float
    collections_today: float
    checkins_today: int
    at_risk_inactive_count: int
    due_followups_count: int
    plan_breakdown: dict[str, int]
    recent_automation_logs: list[dict[str, Any]]


VALID_EXPENSE_CATEGORIES = {
    "Rent",
    "Trainer Salaries",
    "Electricity & Utilities",
    "Equipment Maintenance",
    "Marketing",
    "Supplements & Inventory",
    "Software & SaaS",
    "Other",
}


class ExpenseCreateRequest(BaseModel):
    """Record a studio operating expense for P&L accounting."""

    category: Literal[
        "Rent",
        "Trainer Salaries",
        "Electricity & Utilities",
        "Equipment Maintenance",
        "Marketing",
        "Supplements & Inventory",
        "Software & SaaS",
        "Other",
    ] = "Other"
    amount: float = Field(..., gt=0)
    vendor: str = Field(default="")
    payment_mode: Literal["UPI", "Cash", "Card", "Bank Transfer"] = "UPI"
    expense_date: str | None = None
    notes: str = Field(default="")

    @field_validator("expense_date")
    @classmethod
    def _validate_expense_date(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_iso_date(v, "expense_date")


class AccountsSummaryResponse(BaseModel):
    """Gym CFO financial ledger summary including GST, expenses, and receivables aging."""

    as_of_ist: str
    gross_collections: float
    total_gst_collected: float
    cgst_collected: float
    sgst_collected: float
    net_revenue_ex_gst: float
    total_expenses: float
    net_profit: float
    profit_margin_pct: float
    collections_today: float
    expenses_today: float
    pending_receivables_amount: float
    pending_receivables_count: int
    receivables_aging: dict[str, float]
    revenue_by_plan: dict[str, float]
    expenses_by_category: dict[str, float]
    recent_payments: list[dict[str, Any]]
    recent_expenses: list[dict[str, Any]]


class AgentChatRequest(BaseModel):
    """Command or conversational prompt sent to the Autonomous AI Gym Manager."""

    message: str = Field(..., min_length=1)
    actor_role: Literal["owner", "staff", "member", "n8n_autopilot"] = "owner"
    actor_phone: str = Field(default="")
    member_id: str | None = None
    sync_to_n8n: bool = True


class AgentToolExecution(BaseModel):
    """Trace entry for a single tool invocation made by the AI Agent."""

    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class AgentChatResponse(BaseModel):
    """Structured response from the AI Gym Manager & CFO Agent."""

    run_id: str
    intent: str
    reply: str
    tools_executed: list[AgentToolExecution]
    google_sheets_sync: dict[str, Any]
    n8n_dispatch: dict[str, Any]
    timestamp_ist: str


class MemberUpdateRequest(BaseModel):
    """Partial update payload for an existing gym member."""

    name: str | None = Field(default=None, min_length=1)
    phone: str | None = None
    email: str | None = None
    gender: str | None = None
    date_of_birth: str | None = None
    address: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    fitness_goal: str | None = None
    batch_slot: str | None = None
    assigned_trainer: str | None = None
    medical_notes: str | None = None
    member_status: Literal["Active", "Expiring Soon", "Expired", "Frozen", "Cancelled"] | None = None
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_upd_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Full Name cannot be empty.")
        return trimmed

    @field_validator("phone")
    @classmethod
    def _validate_upd_phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalize_indian_mobile(v)

    @field_validator("email")
    @classmethod
    def _validate_upd_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip().lower()
        if trimmed and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", trimmed):
            raise ValueError("Email must be a valid email address.")
        return trimmed

    @field_validator("gender")
    @classmethod
    def _validate_upd_gender(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if trimmed and trimmed not in VALID_GENDERS:
            raise ValueError(f"Gender must be one of: {', '.join(sorted(VALID_GENDERS))}.")
        return trimmed

    @field_validator("date_of_birth")
    @classmethod
    def _validate_upd_dob(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            return ""
        parsed = validate_iso_date(trimmed, "Date of Birth")
        if parsed > today_ist_str():
            raise ValueError("Date of Birth cannot be in the future.")
        return parsed

    @field_validator("emergency_contact_phone")
    @classmethod
    def _validate_upd_ec_phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            return ""
        return normalize_indian_mobile(trimmed)

    @field_validator("address", "emergency_contact_name", "fitness_goal", "batch_slot", "assigned_trainer", "medical_notes", "notes")
    @classmethod
    def _trim_upd_strings(cls, v: str | None) -> str | None:
        return None if v is None else v.strip()


class MemberRenewRequest(BaseModel):
    """Renew or upgrade a member's plan with optional trainer/batch/discount updates."""

    plan: Literal["Monthly", "Quarterly", "Half-Yearly", "Yearly"] | None = None
    amount_paid: float = Field(..., gt=0)
    payment_mode: Literal["UPI", "Cash", "Card", "Razorpay", "Bank Transfer"] = "UPI"
    transaction_ref: str = ""
    discount_amount: float = Field(default=0.0, ge=0)
    assigned_trainer: str | None = None
    batch_slot: str | None = None
    notes: str = ""
    sync_to_n8n: bool = True


class MembershipPlanCreateRequest(BaseModel):
    """Create a custom or standard studio membership plan."""

    plan_id: str | None = None
    plan_name: str = Field(..., min_length=2)
    duration_days: int = Field(..., ge=1, le=1095)
    price: float = Field(..., ge=0)
    max_freeze_days: int = Field(default=0, ge=0, le=90)
    description: str = ""
    is_active: bool = True

    @field_validator("plan_name", "description")
    @classmethod
    def _trim_plan_fields(cls, v: str) -> str:
        return v.strip()


class MembershipPlanUpdateRequest(BaseModel):
    """Partial update payload for an existing membership plan."""

    price: float | None = Field(default=None, ge=0)
    duration_days: int | None = Field(default=None, ge=1, le=1095)
    max_freeze_days: int | None = Field(default=None, ge=0, le=90)
    description: str | None = None
    is_active: bool | None = None


class PaymentRefundRequest(BaseModel):
    """Mark a confirmed payment transaction as refunded."""

    reason: str = Field(default="Customer refund request", min_length=2)


class GymSettingsUpdateRequest(BaseModel):
    """Bulk update studio settings key-value pairs."""

    settings: dict[str, str]


VALID_WORKFLOW_CODES = {"GM-01", "GM-02", "GM-03", "GM-04", "GM-05"}


class WorkflowConfigUpdateRequest(BaseModel):
    """Update workflow registry settings and optionally sync activation state with n8n REST API."""

    cron_expression: str | None = None
    is_enabled: bool | None = None
    n8n_workflow_id: str | None = None
    webhook_path: str | None = None


class WorkflowTriggerRequest(BaseModel):
    """Trigger an automation workflow on demand via API (with optional n8n webhook dispatch)."""

    payload: dict[str, Any] = Field(default_factory=dict)
    execute_local_pipeline: bool = True
    dispatch_n8n_webhook: bool = True
    triggered_by: str = "NextJS_API"


class AutomationScheduleCreateRequest(BaseModel):
    """Create a recurring or one-time scheduled workflow job."""

    workflow_code: Literal["GM-01", "GM-02", "GM-03", "GM-04", "GM-05"]
    job_name: str = Field(..., min_length=2)
    schedule_type: Literal["cron", "one_time", "interval_minutes"] = "cron"
    cron_expression: str = ""
    run_at_ist: str = ""
    interval_minutes: int = Field(default=0, ge=0, le=43200)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "NextJS_Admin"


class AutomationScheduleUpdateRequest(BaseModel):
    """Pause, resume, cancel, or adjust an existing scheduled workflow job."""

    status: Literal["ACTIVE", "PAUSED", "CANCELLED"] | None = None
    cron_expression: str | None = None
    run_at_ist: str | None = None
    interval_minutes: int | None = Field(default=None, ge=0, le=43200)
    payload: dict[str, Any] | None = None


class N8NWorkflowCallbackPayload(BaseModel):
    """Inbound signed execution callback from n8n back into FastAPI control plane."""

    workflow_code: Literal["GM-01", "GM-02", "GM-03", "GM-04", "GM-05"]
    execution_id: str = ""
    status: Literal["SUCCESS", "COMPLETED", "FAILED", "ERROR", "RUNNING"] = "COMPLETED"
    member_id: str = "STUDIO"
    summary: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)


class N8NFlowGenerateRequest(BaseModel):
    """Generate configured n8n workflow JSONs (GM-01..GM-05) from .env or overrides."""

    workflow_codes: list[Literal["GM-01", "GM-02", "GM-03", "GM-04", "GM-05"]] | None = None
    spreadsheet_id: str | None = None
    fastapi_url: str | None = None
    whatsapp_phone_number_id: str | None = None
    inject_credentials: bool = False


class N8NFlowDeployRequest(BaseModel):
    """Deploy or update workflows onto a live n8n instance using N8N_API_KEY."""

    workflow_codes: list[Literal["GM-01", "GM-02", "GM-03", "GM-04", "GM-05"]] | None = None
    activate: bool = False
    dry_run: bool = False
    spreadsheet_id: str | None = None
    fastapi_url: str | None = None
    whatsapp_phone_number_id: str | None = None


class SheetsRowAppendRequest(BaseModel):
    """Append or upsert a structured row into a Google Sheets workbook tab."""

    row: dict[str, Any] = Field(default_factory=dict)


class SheetsSyncPullRequest(BaseModel):
    """Hydrate backend Google Sheets mirror from an incoming workbook payload or remote source."""

    workbook: dict[str, Any] | None = None

