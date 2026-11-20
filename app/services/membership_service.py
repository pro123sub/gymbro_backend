"""Core business logic for Crush Lifestyle Fitness Studio (Cult.fit-grade SaaS engine)."""

from __future__ import annotations

import secrets
import sqlite3
import string
from datetime import date, timedelta
from typing import Any

from ..config import get_settings, timestamp_ist_str, today_ist_str
from ..database import get_db
from ..schemas import (
    PLAN_DURATIONS,
    AttendanceCheckInRequest,
    DashboardAnalyticsResponse,
    FollowUpItem,
    FreezeMembershipRequest,
    GymSettingsUpdateRequest,
    MemberCreateRequest,
    MemberRenewRequest,
    MemberResponse,
    MembershipPlanCreateRequest,
    MembershipPlanUpdateRequest,
    MemberUpdateRequest,
    N8NFollowUpLogPayload,
    N8NMemberSyncPayload,
    PaymentRecordRequest,
    normalize_indian_mobile,
    validate_iso_date,
)

ID_ALPHABET = string.digits + string.ascii_uppercase


def _get_plan_duration(conn: sqlite3.Connection, plan_name: str) -> int:
    """Resolve plan duration dynamically from membership_plans with static fallback."""
    row = conn.execute(
        "SELECT duration_days FROM membership_plans WHERE plan_name = ?",
        (plan_name,),
    ).fetchone()
    if row:
        return int(row["duration_days"])
    return int(PLAN_DURATIONS.get(plan_name, 30))


def add_days_iso(iso_date: str, days: int) -> str:
    """Add days to a YYYY-MM-DD calendar date and return YYYY-MM-DD."""
    dt = date.fromisoformat(iso_date) + timedelta(days=days)
    return dt.isoformat()


def days_between_iso(later_iso: str, earlier_iso: str) -> int:
    """Return signed day difference (later_iso - earlier_iso)."""
    return (date.fromisoformat(later_iso) - date.fromisoformat(earlier_iso)).days


def generate_member_id(conn: sqlite3.Connection) -> str:
    """Generate a collision-free MEM-XXXXXXXX identifier."""
    for _ in range(25):
        suffix = "".join(secrets.choice(ID_ALPHABET) for _ in range(8))
        candidate = f"MEM-{suffix}"
        exists = conn.execute(
            "SELECT 1 FROM members WHERE member_id = ?", (candidate,)
        ).fetchone()
        if not exists:
            return candidate
    raise RuntimeError("Failed to generate unique MEM-XXXXXXXX identifier.")


def _compute_next_followup_for_row(row: sqlite3.Row, today: str) -> tuple[str | None, str | None]:
    """Compute highest-priority next follow-up hint for a member row."""
    expiry_date = row["expiry_date"]
    days_until_expiry = days_between_iso(expiry_date, today)
    days_since_start = days_between_iso(today, row["start_date"])
    status = row["member_status"]
    payment_status = row["payment_status"]
    last_checkin = row["last_checkin_date"] or ""

    if status == "Frozen":
        return ("MEMBERSHIP_FROZEN", row["frozen_until"] or expiry_date)
    if days_until_expiry == 0:
        return ("EXPIRY_T0_FINAL", today)
    if 0 < days_until_expiry <= 3:
        return ("EXPIRY_T3_URGENT", add_days_iso(expiry_date, -3))
    if 3 < days_until_expiry <= 7:
        return ("EXPIRY_T7_EARLY_BIRD", add_days_iso(expiry_date, -7))
    if payment_status == "Pending":
        return ("PENDING_DUES_RECOVERY", today)
    if days_until_expiry < 0:
        return ("WINBACK_EXPIRED", today)
    if last_checkin and days_between_iso(today, last_checkin) >= 5:
        return ("INACTIVITY_DROPOFF_WE_MISS_YOU", today)
    if days_since_start <= 3:
        return ("ONBOARDING_DAY_3", add_days_iso(row["start_date"], 3))
    return ("EXPIRY_T7_EARLY_BIRD", add_days_iso(expiry_date, -7))


def row_to_member_response(row: sqlite3.Row, today: str | None = None) -> MemberResponse:
    """Convert a SQLite member row to MemberResponse."""
    ref_today = today or today_ist_str()
    days_rem = days_between_iso(row["expiry_date"], ref_today)
    next_type, next_due = _compute_next_followup_for_row(row, ref_today)
    return MemberResponse(
        member_id=row["member_id"],
        name=row["name"],
        phone=row["phone"],
        email=row["email"],
        gender=row["gender"],
        date_of_birth=row["date_of_birth"],
        address=row["address"],
        emergency_contact_name=row["emergency_contact_name"],
        emergency_contact_phone=row["emergency_contact_phone"],
        plan=row["plan"],
        fee=row["fee"],
        start_date=row["start_date"],
        expiry_date=row["expiry_date"],
        payment_status=row["payment_status"],
        last_payment_date=row["last_payment_date"],
        last_reminder_date=row["last_reminder_date"],
        member_status=row["member_status"],
        notes=row["notes"],
        fitness_goal=row["fitness_goal"],
        batch_slot=row["batch_slot"],
        assigned_trainer=row["assigned_trainer"],
        medical_notes=row["medical_notes"],
        discount_amount=row["discount_amount"],
        current_streak=row["current_streak"],
        longest_streak=row["longest_streak"],
        last_checkin_date=row["last_checkin_date"],
        total_checkins=row["total_checkins"],
        freeze_days_used=row["freeze_days_used"],
        frozen_until=row["frozen_until"],
        days_remaining=days_rem,
        next_followup_type=next_type,
        next_followup_due=next_due,
    )


def member_to_sheets_row(member: MemberResponse) -> dict[str, Any]:
    """Return the exact 18-column Google Sheets dictionary for Members tab."""
    return {
        "member_id": member.member_id,
        "name": member.name,
        "phone": member.phone,
        "email": member.email,
        "gender": member.gender,
        "date_of_birth": member.date_of_birth,
        "address": member.address,
        "emergency_contact_name": member.emergency_contact_name,
        "emergency_contact_phone": member.emergency_contact_phone,
        "plan": member.plan,
        "fee": member.fee,
        "start_date": member.start_date,
        "expiry_date": member.expiry_date,
        "payment_status": member.payment_status,
        "last_payment_date": member.last_payment_date,
        "last_reminder_date": member.last_reminder_date,
        "member_status": member.member_status,
        "notes": member.notes,
    }


def _create_payment_entry(
    conn: sqlite3.Connection,
    member_id: str,
    member_name: str,
    phone: str,
    plan: str,
    amount_paid: float,
    payment_mode: str,
    transaction_ref: str,
    payment_date: str,
    previous_expiry: str,
    new_expiry_date: str,
    notes: str = "",
) -> dict[str, Any]:
    """Insert a payment + GST breakdown record into the payments table."""
    settings = get_settings()
    gst_rate = settings.gst_rate
    gst_amount = round(amount_paid - (amount_paid / (1.0 + gst_rate)), 2) if gst_rate > 0 else 0.0
    payment_id = f"PAY-{secrets.token_hex(4).upper()}"
    invoice_number = f"INV-{payment_date.replace('-', '')}-{secrets.token_hex(2).upper()}"
    now_str = timestamp_ist_str()

    conn.execute(
        """
        INSERT INTO payments (
            payment_id, invoice_number, member_id, member_name, phone, plan,
            amount_paid, gst_amount, payment_mode, transaction_ref, payment_date,
            previous_expiry, new_expiry_date, status, notes, created_at_ist
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Confirmed', ?, ?)
        """,
        (
            payment_id,
            invoice_number,
            member_id,
            member_name,
            phone,
            plan,
            amount_paid,
            gst_amount,
            payment_mode,
            transaction_ref,
            payment_date,
            previous_expiry,
            new_expiry_date,
            notes,
            now_str,
        ),
    )
    return {
        "payment_id": payment_id,
        "invoice_number": invoice_number,
        "amount_paid": amount_paid,
        "gst_amount": gst_amount,
        "payment_mode": payment_mode,
        "transaction_ref": transaction_ref,
        "payment_date": payment_date,
        "new_expiry_date": new_expiry_date,
    }


def register_member(payload: MemberCreateRequest) -> tuple[MemberResponse, dict[str, Any] | None]:
    """Create a new member record, compute expiry, and optionally record initial payment."""
    last_payment_date = payload.start_date if payload.payment_status == "Paid" else ""
    onboarding_note = "Onboarding: Day-1 Welcome | Next Follow-Up: +3d"
    combined_notes = f"{payload.notes} | {onboarding_note}" if payload.notes else onboarding_note
    now_str = timestamp_ist_str()

    with get_db() as conn:
        duration_days = _get_plan_duration(conn, payload.plan)
        expiry_date = add_days_iso(payload.start_date, duration_days)
        member_id = generate_member_id(conn)
        conn.execute(
            """
            INSERT INTO members (
                member_id, name, phone, email, gender, date_of_birth, address,
                emergency_contact_name, emergency_contact_phone, plan, fee,
                start_date, expiry_date, payment_status, last_payment_date,
                last_reminder_date, member_status, notes,
                fitness_goal, batch_slot, assigned_trainer, medical_notes,
                discount_amount, created_at_ist, updated_at_ist
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'Active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                member_id,
                payload.name,
                payload.phone,
                payload.email,
                payload.gender,
                payload.date_of_birth,
                payload.address,
                payload.emergency_contact_name,
                payload.emergency_contact_phone,
                payload.plan,
                payload.fee,
                payload.start_date,
                expiry_date,
                payload.payment_status,
                last_payment_date,
                combined_notes,
                payload.fitness_goal,
                payload.batch_slot,
                payload.assigned_trainer,
                payload.medical_notes,
                payload.discount_amount,
                now_str,
                now_str,
            ),
        )

        payment_receipt = None
        if payload.payment_status == "Paid" and payload.fee > 0:
            payment_receipt = _create_payment_entry(
                conn=conn,
                member_id=member_id,
                member_name=payload.name,
                phone=payload.phone,
                plan=payload.plan,
                amount_paid=payload.fee,
                payment_mode=payload.payment_mode,
                transaction_ref=f"REG-{member_id}",
                payment_date=payload.start_date,
                previous_expiry=payload.start_date,
                new_expiry_date=expiry_date,
                notes="Initial registration payment",
            )

        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
    return row_to_member_response(row), payment_receipt


def sync_member_from_n8n(payload: N8NMemberSyncPayload) -> MemberResponse:
    """Upsert a member submitted through n8n Form Trigger (GM-01) into SQLite."""
    normalized_phone = normalize_indian_mobile(payload.phone)
    normalized_ec_phone = normalize_indian_mobile(payload.emergency_contact_phone) if payload.emergency_contact_phone.strip() else ""
    start_date = validate_iso_date(payload.start_date, "start_date")
    now_str = timestamp_ist_str()

    with get_db() as conn:
        expiry_date = (
            validate_iso_date(payload.expiry_date, "expiry_date")
            if payload.expiry_date
            else add_days_iso(start_date, _get_plan_duration(conn, payload.plan))
        )
        last_payment_date = payload.last_payment_date or (start_date if payload.payment_status == "Paid" else "")
        member_id = (payload.member_id or "").strip()
        if not member_id or not member_id.startswith("MEM-"):
            member_id = generate_member_id(conn)

        existing = conn.execute("SELECT 1 FROM members WHERE member_id = ?", (member_id,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE members
                SET name=?, phone=?, email=?, gender=?, date_of_birth=?, address=?,
                    emergency_contact_name=?, emergency_contact_phone=?, plan=?, fee=?,
                    start_date=?, expiry_date=?, payment_status=?, last_payment_date=?,
                    last_reminder_date=?, member_status=?, notes=?, updated_at_ist=?
                WHERE member_id=?
                """,
                (
                    payload.name.strip(),
                    normalized_phone,
                    payload.email.strip().lower(),
                    payload.gender.strip(),
                    payload.date_of_birth.strip(),
                    payload.address.strip(),
                    payload.emergency_contact_name.strip(),
                    normalized_ec_phone,
                    payload.plan,
                    payload.fee,
                    start_date,
                    expiry_date,
                    payload.payment_status,
                    last_payment_date,
                    payload.last_reminder_date.strip(),
                    payload.member_status or "Active",
                    payload.notes.strip(),
                    now_str,
                    member_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO members (
                    member_id, name, phone, email, gender, date_of_birth, address,
                    emergency_contact_name, emergency_contact_phone, plan, fee,
                    start_date, expiry_date, payment_status, last_payment_date,
                    last_reminder_date, member_status, notes, created_at_ist, updated_at_ist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member_id,
                    payload.name.strip(),
                    normalized_phone,
                    payload.email.strip().lower(),
                    payload.gender.strip(),
                    payload.date_of_birth.strip(),
                    payload.address.strip(),
                    payload.emergency_contact_name.strip(),
                    normalized_ec_phone,
                    payload.plan,
                    payload.fee,
                    start_date,
                    expiry_date,
                    payload.payment_status,
                    last_payment_date,
                    payload.last_reminder_date.strip(),
                    payload.member_status or "Active",
                    payload.notes.strip(),
                    now_str,
                    now_str,
                ),
            )
            if payload.payment_status == "Paid" and payload.fee > 0:
                _create_payment_entry(
                    conn=conn,
                    member_id=member_id,
                    member_name=payload.name.strip(),
                    phone=normalized_phone,
                    plan=payload.plan,
                    amount_paid=payload.fee,
                    payment_mode="n8n Form",
                    transaction_ref=f"N8N-{member_id}",
                    payment_date=start_date,
                    previous_expiry=start_date,
                    new_expiry_date=expiry_date,
                    notes="Synced from n8n GM-01",
                )

        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
    return row_to_member_response(row)


def list_members(status: str | None = None, plan: str | None = None, search: str | None = None) -> list[MemberResponse]:
    """List members with optional status/plan/search filters."""
    today = today_ist_str()
    query = "SELECT * FROM members WHERE 1=1"
    params: list[Any] = []

    if plan:
        query += " AND plan = ?"
        params.append(plan)
    if search:
        like = f"%{search.strip()}%"
        query += " AND (name LIKE ? OR phone LIKE ? OR member_id LIKE ?)"
        params.extend([like, like, like])

    query += " ORDER BY created_at_ist DESC"

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()

    members = [row_to_member_response(r, today=today) for r in rows]
    if status:
        target = status.strip().lower()
        if target == "expiring soon":
            members = [m for m in members if 0 <= m.days_remaining <= 7 and m.member_status != "Frozen"]
        elif target == "pending":
            members = [m for m in members if m.payment_status.lower() == "pending"]
        else:
            members = [m for m in members if m.member_status.lower() == target]
    return members


def get_member_360(member_id: str) -> dict[str, Any] | None:
    """Return full member 360 profile including payments, attendance, freezes, and follow-ups."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
        if not row:
            return None
        payments = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM payments WHERE member_id = ? ORDER BY created_at_ist DESC",
                (member_id,),
            ).fetchall()
        ]
        attendance = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM attendance WHERE member_id = ? ORDER BY checkin_date DESC LIMIT 30",
                (member_id,),
            ).fetchall()
        ]
        freezes = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM membership_freezes WHERE member_id = ? ORDER BY created_at_ist DESC",
                (member_id,),
            ).fetchall()
        ]
        followups = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM follow_up_logs WHERE member_id = ? ORDER BY dispatched_at_ist DESC LIMIT 20",
                (member_id,),
            ).fetchall()
        ]

    member = row_to_member_response(row)
    return {
        "member": member,
        "google_sheets_row": member_to_sheets_row(member),
        "payments": payments,
        "attendance": attendance,
        "freezes": freezes,
        "follow_up_logs": followups,
    }


def freeze_membership(member_id: str, req: FreezeMembershipRequest) -> dict[str, Any]:
    """Freeze a member's plan (Cult.fit style) and extend expiry_date."""
    today = today_ist_str()
    freeze_start = req.start_date or today
    freeze_end = add_days_iso(freeze_start, req.freeze_days)
    now_str = timestamp_ist_str()

    with get_db() as conn:
        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
        if not row:
            raise KeyError(f"Member {member_id} not found.")
        if row["member_status"] == "Expired" or row["expiry_date"] < today:
            raise ValueError("Cannot freeze an already expired membership.")

        plan_row = conn.execute(
            "SELECT max_freeze_days FROM membership_plans WHERE plan_name = ?",
            (row["plan"],),
        ).fetchone()
        max_freeze = plan_row["max_freeze_days"] if plan_row else 0
        new_used = int(row["freeze_days_used"]) + req.freeze_days
        if new_used > max_freeze:
            raise ValueError(
                f"{row['plan']} plan allows at most {max_freeze} freeze days (already used {row['freeze_days_used']})."
            )

        prev_expiry = row["expiry_date"]
        new_expiry = add_days_iso(prev_expiry, req.freeze_days)
        freeze_id = f"FRZ-{secrets.token_hex(4).upper()}"
        note_addition = f"Frozen {req.freeze_days}d ({freeze_start}..{freeze_end}): {req.reason}"
        updated_notes = f"{row['notes']} | {note_addition}" if row["notes"] else note_addition

        conn.execute(
            """
            INSERT INTO membership_freezes (
                freeze_id, member_id, freeze_days, start_date, end_date,
                previous_expiry, new_expiry, reason, created_at_ist
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (freeze_id, member_id, req.freeze_days, freeze_start, freeze_end, prev_expiry, new_expiry, req.reason, now_str),
        )
        conn.execute(
            """
            UPDATE members
            SET expiry_date=?, member_status='Frozen', freeze_days_used=?,
                frozen_until=?, notes=?, updated_at_ist=?
            WHERE member_id=?
            """,
            (new_expiry, new_used, freeze_end, updated_notes, now_str, member_id),
        )
        updated = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()

    return {
        "freeze_id": freeze_id,
        "previous_expiry": prev_expiry,
        "new_expiry": new_expiry,
        "frozen_until": freeze_end,
        "member": row_to_member_response(updated),
    }


def record_checkin(req: AttendanceCheckInRequest) -> dict[str, Any]:
    """Record a front-desk / QR check-in and update workout streaks."""
    today = today_ist_str()
    yesterday = add_days_iso(today, -1)
    now_str = timestamp_ist_str()

    if not req.member_id and not req.phone:
        raise ValueError("Provide either member_id or phone to check in.")

    with get_db() as conn:
        if req.member_id:
            row = conn.execute("SELECT * FROM members WHERE member_id = ?", (req.member_id.strip(),)).fetchone()
        else:
            norm_phone = normalize_indian_mobile(req.phone or "")
            row = conn.execute("SELECT * FROM members WHERE phone = ?", (norm_phone,)).fetchone()

        if not row:
            raise KeyError("Member not found for the supplied identifier.")

        if row["member_status"] == "Frozen" and (row["frozen_until"] or "") > today:
            raise ValueError(f"Membership is currently frozen until {row['frozen_until']}.")
        if row["expiry_date"] < today or row["member_status"] == "Expired":
            conn.execute(
                "UPDATE members SET member_status='Expired', updated_at_ist=? WHERE member_id=?",
                (now_str, row["member_id"]),
            )
            raise ValueError(f"Membership expired on {row['expiry_date']}. Renewal required before check-in.")

        already_today = conn.execute(
            "SELECT * FROM attendance WHERE member_id = ? AND checkin_date = ?",
            (row["member_id"], today),
        ).fetchone()
        if already_today:
            return {
                "already_checked_in_today": True,
                "attendance_id": already_today["attendance_id"],
                "member_id": row["member_id"],
                "member_name": row["name"],
                "current_streak": row["current_streak"],
                "longest_streak": row["longest_streak"],
                "milestone": None,
            }

        last_checkin = row["last_checkin_date"] or ""
        new_streak = int(row["current_streak"]) + 1 if last_checkin == yesterday else 1
        new_longest = max(int(row["longest_streak"]), new_streak)
        new_total = int(row["total_checkins"]) + 1
        attendance_id = f"ATT-{secrets.token_hex(4).upper()}"

        conn.execute(
            """
            INSERT INTO attendance (
                attendance_id, member_id, checkin_date, checkin_time_ist,
                workout_type, batch_slot, streak_after_checkin
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (attendance_id, row["member_id"], today, now_str, req.workout_type, req.batch_slot, new_streak),
        )
        conn.execute(
            """
            UPDATE members
            SET member_status='Active', current_streak=?, longest_streak=?,
                last_checkin_date=?, total_checkins=?, updated_at_ist=?
            WHERE member_id=?
            """,
            (new_streak, new_longest, today, new_total, now_str, row["member_id"]),
        )

    milestone = f"{new_streak}-Day Consistency Streak!" if new_streak in {7, 14, 21, 30, 60, 100} else None
    return {
        "already_checked_in_today": False,
        "attendance_id": attendance_id,
        "member_id": row["member_id"],
        "member_name": row["name"],
        "workout_type": req.workout_type,
        "current_streak": new_streak,
        "longest_streak": new_longest,
        "total_checkins": new_total,
        "milestone": milestone,
    }


def record_payment(req: PaymentRecordRequest) -> dict[str, Any]:
    """Process a member payment or renewal idempotently."""
    today = today_ist_str()
    now_str = timestamp_ist_str()
    tx_ref = req.transaction_ref.strip() or f"{req.payment_mode.upper()}-{secrets.token_hex(4).upper()}"

    with get_db() as conn:
        dup = conn.execute("SELECT * FROM payments WHERE transaction_ref = ?", (tx_ref,)).fetchone()
        if dup:
            member_row = conn.execute("SELECT * FROM members WHERE member_id = ?", (dup["member_id"],)).fetchone()
            return {
                "already_processed": True,
                "payment": dict(dup),
                "member": row_to_member_response(member_row),
            }

        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (req.member_id.strip(),)).fetchone()
        if not row:
            raise KeyError(f"Member {req.member_id} not found.")

        active_plan = req.plan or row["plan"]
        duration_days = _get_plan_duration(conn, active_plan)
        prev_expiry = row["expiry_date"]
        is_first_pending_settlement = (not (row["last_payment_date"] or "").strip()) and row["payment_status"] == "Pending"

        if is_first_pending_settlement or not req.extend_membership:
            new_expiry = prev_expiry
        else:
            base_date = prev_expiry if prev_expiry > today else today
            new_expiry = add_days_iso(base_date, duration_days)

        receipt = _create_payment_entry(
            conn=conn,
            member_id=row["member_id"],
            member_name=row["name"],
            phone=row["phone"],
            plan=active_plan,
            amount_paid=req.amount_paid,
            payment_mode=req.payment_mode,
            transaction_ref=tx_ref,
            payment_date=today,
            previous_expiry=prev_expiry,
            new_expiry_date=new_expiry,
            notes=req.notes or "Processed via FastAPI",
        )

        marker = f"Paid:{today} Ref:{tx_ref}"
        updated_notes = f"{row['notes']} | {marker}" if row["notes"] else marker

        conn.execute(
            """
            UPDATE members
            SET plan=?, expiry_date=?, payment_status='Paid', last_payment_date=?,
                member_status='Active', frozen_until='', notes=?, updated_at_ist=?
            WHERE member_id=?
            """,
            (active_plan, new_expiry, today, updated_notes, now_str, row["member_id"]),
        )
        updated = conn.execute("SELECT * FROM members WHERE member_id = ?", (row["member_id"],)).fetchone()

    member_obj = row_to_member_response(updated)
    return {
        "already_processed": False,
        "payment": receipt,
        "member": member_obj,
        "google_sheets_row": member_to_sheets_row(member_obj),
    }


def evaluate_follow_ups(include_already_reminded_today: bool = False) -> list[FollowUpItem]:
    """Evaluate all members against Cult.fit-grade retention & follow-up playbooks."""
    today = today_ist_str()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM members ORDER BY expiry_date ASC").fetchall()

    items: list[FollowUpItem] = []
    priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

    for r in rows:
        if r["member_status"] == "Frozen":
            continue
        if not include_already_reminded_today and (r["last_reminder_date"] or "") == today:
            continue

        days_until_exp = days_between_iso(r["expiry_date"], today)
        days_since_start = days_between_iso(today, r["start_date"])
        last_checkin = r["last_checkin_date"] or ""
        days_since_checkin = days_between_iso(today, last_checkin) if last_checkin else None

        followup_type = None
        priority = "MEDIUM"
        template = "crush_fitness_followup_email"
        msg = ""

        if days_until_exp == 0:
            followup_type = "EXPIRY_T0_FINAL"
            priority = "CRITICAL"
            msg = (
                f"Hi {r['name']}, your {r['plan']} membership at Crush Lifestyle Fitness Studio expires TODAY "
                f"({r['expiry_date']}). Renew now to keep your {r['current_streak']}-day streak going!"
            )
        elif 1 <= days_until_exp <= 3:
            followup_type = "EXPIRY_T3_URGENT"
            priority = "CRITICAL"
            msg = (
                f"Hi {r['name']}, only {days_until_exp} day(s) left on your {r['plan']} plan "
                f"(expires {r['expiry_date']}). Renew today to keep your access uninterrupted."
            )
        elif r["payment_status"] == "Pending" and days_since_start >= 1:
            followup_type = "PENDING_DUES_RECOVERY"
            priority = "HIGH"
            msg = (
                f"Hi {r['name']}, friendly reminder from Crush Lifestyle Fitness Studio: your {r['plan']} "
                f"fee of Rs.{int(r['fee'])} is pending. Clear via UPI or reception desk."
            )
        elif 4 <= days_until_exp <= 7:
            followup_type = "EXPIRY_T7_EARLY_BIRD"
            priority = "HIGH"
            msg = (
                f"Hi {r['name']}, your {r['plan']} membership expires in {days_until_exp} days ({r['expiry_date']}). "
                f"Renew early for priority trainer slots!"
            )
        elif days_until_exp < 0 and abs(days_until_exp) in {1, 2, 3, 7, 14, 30}:
            followup_type = "WINBACK_CHURNED"
            priority = "MEDIUM"
            msg = (
                f"We miss you at Crush Lifestyle Fitness Studio, {r['name']}! Your plan expired on "
                f"{r['expiry_date']}. Restart today with zero re-joining fee."
            )
        elif days_since_checkin is not None and days_since_checkin >= 5 and days_until_exp > 0:
            followup_type = "INACTIVITY_DROPOFF_WE_MISS_YOU"
            priority = "HIGH"
            msg = (
                f"Hey {r['name']}, we haven't seen you in {days_since_checkin} days! "
                f"Book a quick 30-min comeback session with {r['assigned_trainer']} at Crush Lifestyle Fitness Studio."
            )
        elif days_since_start in {1, 3}:
            followup_type = "ONBOARDING_DAY_1_WELCOME" if days_since_start == 1 else "ONBOARDING_DAY_3_CHECKIN"
            priority = "LOW"
            msg = (
                f"Welcome to Crush Lifestyle Fitness Studio, {r['name']}! "
                f"Ready for your {r['fitness_goal']} induction in the {r['batch_slot']} batch?"
            )

        if not followup_type:
            continue

        items.append(
            FollowUpItem(
                member_id=r["member_id"],
                name=r["name"],
                phone=r["phone"],
                plan=r["plan"],
                expiry_date=r["expiry_date"],
                payment_status=r["payment_status"],
                member_status=r["member_status"],
                followup_type=followup_type,
                priority=priority,
                template_name=template,
                message_text=msg,
                days_until_expiry=days_until_exp,
                days_since_last_checkin=days_since_checkin,
            )
        )

    items.sort(key=lambda x: (priority_order[x.priority], x.days_until_expiry))
    return items


def record_followup_callback(payload: N8NFollowUpLogPayload) -> dict[str, Any]:
    """Record a follow-up execution callback from n8n GM-02."""
    today = payload.last_reminder_date or today_ist_str()
    now_str = timestamp_ist_str()
    log_id = f"FLW-{secrets.token_hex(4).upper()}"

    with get_db() as conn:
        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (payload.member_id,)).fetchone()
        if not row:
            raise KeyError(f"Member {payload.member_id} not found.")

        conn.execute(
            """
            INSERT INTO follow_up_logs (log_id, member_id, followup_type, channel, message, status, dispatched_at_ist)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (log_id, payload.member_id, payload.followup_type, payload.channel, payload.message, payload.status, now_str),
        )
        new_status = payload.new_member_status or row["member_status"]
        conn.execute(
            "UPDATE members SET last_reminder_date=?, member_status=?, updated_at_ist=? WHERE member_id=?",
            (today, new_status, now_str, payload.member_id),
        )
    return {"log_id": log_id, "member_id": payload.member_id, "last_reminder_date": today, "status": payload.status}


def get_dashboard_analytics() -> DashboardAnalyticsResponse:
    """Compute live studio KPIs for the owner & front-desk console."""
    today = today_ist_str()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM members").fetchall()
        total_rev_row = conn.execute("SELECT COALESCE(SUM(amount_paid), 0) AS total FROM payments WHERE status='Confirmed'").fetchone()
        today_rev_row = conn.execute(
            "SELECT COALESCE(SUM(amount_paid), 0) AS total FROM payments WHERE status='Confirmed' AND payment_date = ?",
            (today,),
        ).fetchone()
        checkins_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM attendance WHERE checkin_date = ?",
            (today,),
        ).fetchone()
        recent_logs = [
            dict(r)
            for r in conn.execute(
                "SELECT log_id, workflow, event_type, member_id, status, response_status, details, created_at_ist FROM automation_logs ORDER BY created_at_ist DESC LIMIT 12"
            ).fetchall()
        ]

    active_count = 0
    expiring_7d = 0
    expired_count = 0
    frozen_count = 0
    pending_count = 0
    pending_amount = 0.0
    inactive_risk = 0
    plan_breakdown = {k: 0 for k in PLAN_DURATIONS}

    for r in rows:
        plan_breakdown[r["plan"]] = plan_breakdown.get(r["plan"], 0) + 1
        days_left = days_between_iso(r["expiry_date"], today)
        if r["member_status"] == "Frozen":
            frozen_count += 1
        elif days_left < 0 or r["member_status"] == "Expired":
            expired_count += 1
        else:
            active_count += 1
            if 0 <= days_left <= 7:
                expiring_7d += 1
            last_ci = r["last_checkin_date"] or ""
            if last_ci and days_between_iso(today, last_ci) >= 5:
                inactive_risk += 1
        if r["payment_status"] == "Pending":
            pending_count += 1
            pending_amount += float(r["fee"] or 0)

    due_followups = evaluate_follow_ups()
    return DashboardAnalyticsResponse(
        as_of_ist=timestamp_ist_str(),
        total_members=len(rows),
        active_members=active_count,
        expiring_in_7_days=expiring_7d,
        expired_members=expired_count,
        frozen_members=frozen_count,
        pending_dues_count=pending_count,
        pending_dues_amount=round(pending_amount, 2),
        total_revenue_collected=round(float(total_rev_row["total"] or 0), 2),
        collections_today=round(float(today_rev_row["total"] or 0), 2),
        checkins_today=int(checkins_row["cnt"] or 0),
        at_risk_inactive_count=inactive_risk,
        due_followups_count=len(due_followups),
        plan_breakdown=plan_breakdown,
        recent_automation_logs=recent_logs,
    )


ALLOWED_MEMBER_UPDATE_FIELDS = (
    "name",
    "phone",
    "email",
    "gender",
    "date_of_birth",
    "address",
    "emergency_contact_name",
    "emergency_contact_phone",
    "fitness_goal",
    "batch_slot",
    "assigned_trainer",
    "medical_notes",
    "member_status",
    "notes",
)


def update_member(member_id: str, payload: MemberUpdateRequest) -> MemberResponse:
    """Apply partial profile updates to an existing member record."""
    mid = member_id.strip()
    updates = {
        k: v
        for k, v in payload.model_dump(exclude_none=True).items()
        if k in ALLOWED_MEMBER_UPDATE_FIELDS
    }
    now_str = timestamp_ist_str()

    with get_db() as conn:
        existing = conn.execute("SELECT * FROM members WHERE member_id = ?", (mid,)).fetchone()
        if not existing:
            raise KeyError(f"Member {mid} not found.")
        if updates:
            set_clauses = [f"{col} = ?" for col in updates]
            set_clauses.append("updated_at_ist = ?")
            params = list(updates.values()) + [now_str, mid]
            conn.execute(
                f"UPDATE members SET {', '.join(set_clauses)} WHERE member_id = ?",
                params,
            )
            existing = conn.execute("SELECT * FROM members WHERE member_id = ?", (mid,)).fetchone()

    return row_to_member_response(existing)


def unfreeze_membership(member_id: str, reason: str = "Resumed early") -> dict[str, Any]:
    """Resume a frozen membership early and reclaim unused freeze days from expiry."""
    mid = member_id.strip()
    today = today_ist_str()
    now_str = timestamp_ist_str()

    with get_db() as conn:
        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (mid,)).fetchone()
        if not row:
            raise KeyError(f"Member {mid} not found.")
        if row["member_status"] != "Frozen":
            raise ValueError(f"Member {mid} is not currently frozen (status: {row['member_status']}).")

        latest_freeze = conn.execute(
            "SELECT * FROM membership_freezes WHERE member_id = ? ORDER BY created_at_ist DESC LIMIT 1",
            (mid,),
        ).fetchone()

        frozen_until = row["frozen_until"] or ""
        unused_days = 0
        if frozen_until > today:
            unused_days = max(0, days_between_iso(frozen_until, today))
            if latest_freeze:
                unused_days = min(int(latest_freeze["freeze_days"]), unused_days)

        new_expiry = add_days_iso(row["expiry_date"], -unused_days) if unused_days > 0 else row["expiry_date"]
        new_used = max(0, int(row["freeze_days_used"]) - unused_days)
        note_addition = f"Unfrozen {today} (reclaimed {unused_days}d): {reason.strip()}"
        updated_notes = f"{row['notes']} | {note_addition}" if row["notes"] else note_addition

        conn.execute(
            """
            UPDATE members
            SET expiry_date=?, member_status='Active', freeze_days_used=?,
                frozen_until='', notes=?, updated_at_ist=?
            WHERE member_id=?
            """,
            (new_expiry, new_used, updated_notes, now_str, mid),
        )
        updated = conn.execute("SELECT * FROM members WHERE member_id = ?", (mid,)).fetchone()

    return {
        "member_id": mid,
        "reclaimed_freeze_days": unused_days,
        "new_expiry": new_expiry,
        "member": row_to_member_response(updated),
    }


def renew_member(member_id: str, payload: MemberRenewRequest) -> dict[str, Any]:
    """Renew or upgrade a member's plan with optional trainer, batch, and discount updates."""
    mid = member_id.strip()
    now_str = timestamp_ist_str()

    with get_db() as conn:
        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (mid,)).fetchone()
        if not row:
            raise KeyError(f"Member {mid} not found.")
        update_fields: list[str] = []
        params: list[Any] = []
        if payload.assigned_trainer is not None:
            update_fields.append("assigned_trainer = ?")
            params.append(payload.assigned_trainer.strip())
        if payload.batch_slot is not None:
            update_fields.append("batch_slot = ?")
            params.append(payload.batch_slot.strip())
        if payload.discount_amount > 0:
            update_fields.append("discount_amount = ?")
            params.append(payload.discount_amount)
        if update_fields:
            update_fields.append("updated_at_ist = ?")
            params.extend([now_str, mid])
            conn.execute(
                f"UPDATE members SET {', '.join(update_fields)} WHERE member_id = ?",
                params,
            )

    return record_payment(
        PaymentRecordRequest(
            member_id=mid,
            amount_paid=payload.amount_paid,
            payment_mode=payload.payment_mode,
            transaction_ref=payload.transaction_ref,
            plan=payload.plan,
            extend_membership=True,
            notes=payload.notes or "Membership renewal",
        )
    )


def archive_or_delete_member(member_id: str, hard: bool = False) -> dict[str, Any]:
    """Soft-archive (Cancelled) or hard-delete a member record."""
    mid = member_id.strip()
    now_str = timestamp_ist_str()

    with get_db() as conn:
        row = conn.execute("SELECT * FROM members WHERE member_id = ?", (mid,)).fetchone()
        if not row:
            raise KeyError(f"Member {mid} not found.")
        if hard:
            conn.execute("DELETE FROM members WHERE member_id = ?", (mid,))
            return {"member_id": mid, "deleted": True, "mode": "hard"}

        note_addition = f"Cancelled on {today_ist_str()}"
        updated_notes = f"{row['notes']} | {note_addition}" if row["notes"] else note_addition
        conn.execute(
            "UPDATE members SET member_status='Cancelled', notes=?, updated_at_ist=? WHERE member_id=?",
            (updated_notes, now_str, mid),
        )
        updated = conn.execute("SELECT * FROM members WHERE member_id = ?", (mid,)).fetchone()

    return {
        "member_id": mid,
        "deleted": False,
        "mode": "archived",
        "member": row_to_member_response(updated),
    }


def get_attendance_history(
    member_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    workout_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query attendance check-ins with optional member, date range, and workout filters."""
    query = """
        SELECT a.*, m.name, m.phone, m.plan, m.current_streak, m.longest_streak
        FROM attendance a
        JOIN members m ON a.member_id = m.member_id
        WHERE 1=1
    """
    params: list[Any] = []
    if member_id:
        query += " AND a.member_id = ?"
        params.append(member_id.strip())
    if date_from:
        query += " AND a.checkin_date >= ?"
        params.append(validate_iso_date(date_from, "date_from"))
    if date_to:
        query += " AND a.checkin_date <= ?"
        params.append(validate_iso_date(date_to, "date_to"))
    if workout_type:
        query += " AND a.workout_type = ?"
        params.append(workout_type.strip())
    query += " ORDER BY a.checkin_date DESC, a.checkin_time_ist DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_attendance_leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    """Return top gym members ranked by workout consistency streak."""
    safe_limit = max(1, min(limit, 100))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT member_id, name, plan, current_streak, longest_streak,
                   total_checkins, last_checkin_date, assigned_trainer
            FROM members
            ORDER BY current_streak DESC, longest_streak DESC, total_checkins DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [{"rank": idx + 1, **dict(r)} for idx, r in enumerate(rows)]


def get_gym_settings() -> dict[str, str]:
    """Return studio configuration key-value map from gym_settings."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT setting_key, setting_value FROM gym_settings ORDER BY setting_key ASC"
        ).fetchall()
    return {r["setting_key"]: r["setting_value"] for r in rows}


def update_gym_settings(payload: GymSettingsUpdateRequest) -> dict[str, str]:
    """Upsert studio settings key-value pairs and return the updated settings map."""
    now_str = timestamp_ist_str()
    entries = [
        (str(k).strip(), str(v).strip(), now_str)
        for k, v in payload.settings.items()
        if str(k).strip()
    ]
    if entries:
        with get_db() as conn:
            conn.executemany(
                """
                INSERT INTO gym_settings (setting_key, setting_value, updated_at_ist)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at_ist = excluded.updated_at_ist
                """,
                entries,
            )
    return get_gym_settings()


def get_payment_invoice(payment_id: str) -> dict[str, Any]:
    """Generate a structured Indian GST Tax Invoice (CGST + SGST) for a payment."""
    pid = payment_id.strip()
    with get_db() as conn:
        pay = conn.execute(
            "SELECT * FROM payments WHERE payment_id = ? OR invoice_number = ?",
            (pid, pid),
        ).fetchone()
        if not pay:
            raise KeyError(f"Payment {pid} not found.")
        member = conn.execute(
            "SELECT member_id, name, phone, email, address FROM members WHERE member_id = ?",
            (pay["member_id"],),
        ).fetchone()

    settings_map = get_gym_settings()
    gst_rate = float(settings_map.get("gst_rate", get_settings().gst_rate))
    half_rate_pct = round(gst_rate * 50.0, 2)
    total_paid = round(float(pay["amount_paid"] or 0), 2)
    total_gst = round(float(pay["gst_amount"] or 0), 2)
    cgst = round(total_gst / 2.0, 2)
    sgst = round(total_gst - cgst, 2)
    taxable_value = round(total_paid - total_gst, 2)

    return {
        "invoice_number": pay["invoice_number"],
        "payment_id": pay["payment_id"],
        "invoice_date": pay["payment_date"],
        "studio": {
            "name": settings_map.get("gym_name", "Crush Lifestyle Fitness Studio"),
            "branch": settings_map.get("branch_name", "Main Studio"),
            "gstin": settings_map.get("gstin", ""),
            "sac_code": settings_map.get("sac_code", "999723"),
            "timezone": settings_map.get("timezone", "Asia/Kolkata"),
        },
        "billed_to": {
            "member_id": pay["member_id"],
            "name": member["name"] if member else pay["member_name"],
            "phone": member["phone"] if member else pay["phone"],
            "email": member["email"] if member else "",
            "address": member["address"] if member else "",
        },
        "plan": pay["plan"],
        "taxable_value": taxable_value,
        "cgst_rate_pct": half_rate_pct,
        "cgst_amount": cgst,
        "sgst_rate_pct": half_rate_pct,
        "sgst_amount": sgst,
        "total_gst": total_gst,
        "total_amount_paid": total_paid,
        "payment_mode": pay["payment_mode"],
        "transaction_ref": pay["transaction_ref"],
        "status": pay["status"],
        "billing_period": {
            "from": pay["previous_expiry"],
            "to": pay["new_expiry_date"],
        },
    }


def refund_payment(payment_id: str, reason: str) -> dict[str, Any]:
    """Mark a confirmed payment as Refunded and append an audit note to the member."""
    pid = payment_id.strip()
    today = today_ist_str()
    now_str = timestamp_ist_str()

    with get_db() as conn:
        pay = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (pid,)).fetchone()
        if not pay:
            raise KeyError(f"Payment {pid} not found.")
        if pay["status"] == "Refunded":
            raise ValueError(f"Payment {pid} has already been refunded.")

        refund_note = f"Refunded ({today}): {reason.strip()}"
        updated_pay_notes = f"{pay['notes']} | {refund_note}" if pay["notes"] else refund_note
        conn.execute(
            "UPDATE payments SET status='Refunded', notes=? WHERE payment_id=?",
            (updated_pay_notes, pid),
        )

        member = conn.execute(
            "SELECT notes FROM members WHERE member_id = ?",
            (pay["member_id"],),
        ).fetchone()
        if member:
            m_note = f"Refund:{today} {pid} ({reason.strip()})"
            updated_m_notes = f"{member['notes']} | {m_note}" if member["notes"] else m_note
            conn.execute(
                "UPDATE members SET notes=?, updated_at_ist=? WHERE member_id=?",
                (updated_m_notes, now_str, pay["member_id"]),
            )
        updated_pay = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (pid,)).fetchone()

    return dict(updated_pay)


def create_membership_plan(payload: MembershipPlanCreateRequest) -> dict[str, Any]:
    """Create a new membership plan in membership_plans."""
    plan_id = (payload.plan_id or "").strip() or f"PLN-{secrets.token_hex(3).upper()}"
    with get_db() as conn:
        dup = conn.execute(
            "SELECT 1 FROM membership_plans WHERE plan_id = ? OR LOWER(plan_name) = LOWER(?)",
            (plan_id, payload.plan_name),
        ).fetchone()
        if dup:
            raise ValueError(f"Membership plan '{payload.plan_name}' or ID '{plan_id}' already exists.")
        conn.execute(
            """
            INSERT INTO membership_plans
                (plan_id, plan_name, duration_days, price, max_freeze_days, description, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                payload.plan_name,
                payload.duration_days,
                payload.price,
                payload.max_freeze_days,
                payload.description,
                1 if payload.is_active else 0,
            ),
        )
        row = conn.execute("SELECT * FROM membership_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    return dict(row)


def update_membership_plan(plan_id: str, payload: MembershipPlanUpdateRequest) -> dict[str, Any]:
    """Update pricing, duration, freeze quota, description, or active status for a plan."""
    pid = plan_id.strip()
    updates = payload.model_dump(exclude_none=True)
    if "is_active" in updates:
        updates["is_active"] = 1 if updates["is_active"] else 0

    with get_db() as conn:
        existing = conn.execute("SELECT * FROM membership_plans WHERE plan_id = ?", (pid,)).fetchone()
        if not existing:
            raise KeyError(f"Membership plan {pid} not found.")
        if updates:
            allowed = ("price", "duration_days", "max_freeze_days", "description", "is_active")
            clean = {k: v for k, v in updates.items() if k in allowed}
            if clean:
                clauses = [f"{col} = ?" for col in clean]
                params = list(clean.values()) + [pid]
                conn.execute(
                    f"UPDATE membership_plans SET {', '.join(clauses)} WHERE plan_id = ?",
                    params,
                )
        row = conn.execute("SELECT * FROM membership_plans WHERE plan_id = ?", (pid,)).fetchone()
    return dict(row)


def list_automation_logs(
    workflow: str | None = None,
    status: str | None = None,
    member_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query automation audit logs with optional workflow, status, and member filters."""
    query = "SELECT * FROM automation_logs WHERE 1=1"
    params: list[Any] = []
    if workflow:
        query += " AND workflow = ?"
        params.append(workflow.strip())
    if status:
        query += " AND status = ?"
        params.append(status.strip())
    if member_id:
        query += " AND member_id = ?"
        params.append(member_id.strip())
    query += " ORDER BY created_at_ist DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def export_full_workbook() -> dict[str, Any]:
    """Export a full 6-tab Google Sheets compatible snapshot from SQLite."""
    today = today_ist_str()
    with get_db() as conn:
        member_rows = conn.execute("SELECT * FROM members ORDER BY created_at_ist DESC").fetchall()
        plan_rows = conn.execute("SELECT * FROM membership_plans ORDER BY duration_days ASC").fetchall()
        payment_rows = conn.execute("SELECT * FROM payments ORDER BY created_at_ist DESC").fetchall()
        attendance_rows = conn.execute(
            """
            SELECT a.*, m.name AS member_name, m.phone
            FROM attendance a
            JOIN members m ON a.member_id = m.member_id
            ORDER BY a.checkin_date DESC, a.checkin_time_ist DESC
            """
        ).fetchall()
        log_rows = conn.execute("SELECT * FROM automation_logs ORDER BY created_at_ist DESC").fetchall()
        settings_rows = conn.execute("SELECT * FROM gym_settings ORDER BY setting_key ASC").fetchall()

    members_sheet = [member_to_sheets_row(row_to_member_response(r, today=today)) for r in member_rows]
    plans_sheet = [dict(r) for r in plan_rows]
    payments_sheet = [dict(r) for r in payment_rows]
    attendance_sheet = [dict(r) for r in attendance_rows]
    logs_sheet = [dict(r) for r in log_rows]
    settings_sheet = [dict(r) for r in settings_rows]

    return {
        "exported_at_ist": timestamp_ist_str(),
        "counts": {
            "Members": len(members_sheet),
            "Membership Plans": len(plans_sheet),
            "Payments": len(payments_sheet),
            "Attendance": len(attendance_sheet),
            "Automation Log": len(logs_sheet),
            "Gym Settings": len(settings_sheet),
        },
        "Members": members_sheet,
        "Membership Plans": plans_sheet,
        "Payments": payments_sheet,
        "Attendance": attendance_sheet,
        "Automation Log": logs_sheet,
        "Gym Settings": settings_sheet,
    }


def _collect_trend_insights(
    *,
    total_members: int,
    active_members: int,
    pending_dues_count: int,
    pending_dues_amount: float,
    expiring_3d_count: int,
    expiring_7d_count: int,
    inactive_5d_count: int,
    collection_efficiency_pct: float,
    net_profit: float,
    batch_trends: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Generate automated operational & retention insights from current metrics."""
    insights: list[dict[str, str]] = []
    if pending_dues_count > 0:
        insights.append({
            "category": "Revenue Recovery",
            "severity": "HIGH",
            "title": f"Rs.{int(round(pending_dues_amount)):,} pending across {pending_dues_count} member(s)",
            "action_hint": "Run GM-02 Email Retention Cadence or Collect Fee in Finance tab.",
        })
    if expiring_3d_count > 0 or expiring_7d_count > 0:
        insights.append({
            "category": "Renewal Retention",
            "severity": "CRITICAL" if expiring_3d_count > 0 else "MEDIUM",
            "title": f"{expiring_3d_count} urgent (<=3d) and {expiring_7d_count} upcoming (<=7d) renewals",
            "action_hint": "Send early-bird renewal reminders via GM-02 before expiry date.",
        })
    if inactive_5d_count > 0:
        insights.append({
            "category": "Churn Prevention",
            "severity": "HIGH",
            "title": f"{inactive_5d_count} active member(s) haven't checked in for 5+ days",
            "action_hint": "Assign trainer check-in call and trigger 'We Miss You' cadence.",
        })
    if batch_trends:
        top_batch = batch_trends[0]
        insights.append({
            "category": "Capacity Trend",
            "severity": "INFO",
            "title": f"Peak slot: {top_batch['batch_slot']} ({top_batch['members_count']} members, {top_batch['share_pct']}%)",
            "action_hint": "Schedule flagship group classes and senior trainers during peak slot.",
        })
    if total_members > 0:
        insights.append({
            "category": "Financial Health",
            "severity": "POSITIVE" if collection_efficiency_pct >= 80 and net_profit >= 0 else "MEDIUM",
            "title": f"Collection efficiency at {collection_efficiency_pct}% | Active base {active_members}/{total_members}",
            "action_hint": "Keep running Master DB + Trends Autopilot daily at 9PM IST via GM-04.",
        })
    return insights


def get_automated_trends_and_analysis() -> dict[str, Any]:
    """Compute automated studio trends, churn radar, cohort mix, and executive insights."""
    today = today_ist_str()
    settings = get_settings()
    gst_rate = settings.gst_rate

    with get_db() as conn:
        member_rows = conn.execute("SELECT * FROM members ORDER BY created_at_ist DESC").fetchall()
        payment_rows = conn.execute("SELECT * FROM payments WHERE status='Confirmed'").fetchall()
        attendance_rows = conn.execute("SELECT * FROM attendance").fetchall()
        expense_rows = conn.execute("SELECT * FROM expenses").fetchall()

    total_members = len(member_rows)
    active_count = 0
    frozen_count = 0
    expired_count = 0
    pending_count = 0
    pending_amount = 0.0
    total_billed = 0.0

    expiring_3d: list[dict[str, Any]] = []
    expiring_7d: list[dict[str, Any]] = []
    inactive_5d: list[dict[str, Any]] = []
    pending_dues_list: list[dict[str, Any]] = []

    plan_counts: dict[str, int] = {}
    batch_counts: dict[str, int] = {}

    for r in member_rows:
        fee = float(r["fee"] or 0.0)
        total_billed += fee
        plan_name = r["plan"] or "Monthly"
        plan_counts[plan_name] = plan_counts.get(plan_name, 0) + 1
        batch = (r["batch_slot"] or "Flexible").strip() or "Flexible"
        batch_counts[batch] = batch_counts.get(batch, 0) + 1

        days_left = days_between_iso(r["expiry_date"], today)
        brief = {
            "member_id": r["member_id"],
            "name": r["name"],
            "phone": r["phone"],
            "plan": plan_name,
            "fee": fee,
            "expiry_date": r["expiry_date"],
            "days_remaining": days_left,
        }

        if r["payment_status"] == "Pending":
            pending_count += 1
            pending_amount += fee
            pending_dues_list.append(brief)

        if r["member_status"] == "Frozen":
            frozen_count += 1
        elif days_left < 0 or r["member_status"] == "Expired":
            expired_count += 1
        else:
            active_count += 1
            if 0 <= days_left <= 3:
                expiring_3d.append(brief)
            elif 4 <= days_left <= 7:
                expiring_7d.append(brief)
            last_ci = (r["last_checkin_date"] or "").strip()
            if last_ci and days_between_iso(today, last_ci) >= 5:
                inactive_5d.append({**brief, "days_since_checkin": days_between_iso(today, last_ci)})

    gross_collections = round(sum(float(p["amount_paid"] or 0.0) for p in payment_rows), 2)
    total_gst = round(
        sum(float(p["gst_amount"] or 0.0) for p in payment_rows)
        or (gross_collections - gross_collections / (1.0 + gst_rate) if gst_rate > 0 else 0.0),
        2,
    )
    net_revenue_ex_gst = round(gross_collections - total_gst, 2)
    total_expenses = round(sum(float(e["amount"] or 0.0) for e in expense_rows), 2)
    net_profit = round(net_revenue_ex_gst - total_expenses, 2)

    plan_rev: dict[str, float] = {}
    for p in payment_rows:
        pl = p["plan"] or "Other"
        plan_rev[pl] = round(plan_rev.get(pl, 0.0) + float(p["amount_paid"] or 0.0), 2)

    workout_counts: dict[str, int] = {}
    total_checkins = len(attendance_rows)
    for a in attendance_rows:
        wt = (a["workout_type"] or "Gym Floor").strip() or "Gym Floor"
        workout_counts[wt] = workout_counts.get(wt, 0) + 1

    retention_rate_pct = round(((active_count + frozen_count) / total_members) * 100.0, 1) if total_members > 0 else 100.0
    collection_denom = gross_collections + pending_amount
    collection_efficiency_pct = (
        round((gross_collections / collection_denom) * 100.0, 1) if collection_denom > 0 else 100.0
    )
    arpm = round(gross_collections / total_members, 2) if total_members > 0 else 0.0
    checkins_today = sum(1 for a in attendance_rows if a["checkin_date"] == today)
    engagement_ratio = (min(1.0, checkins_today / max(active_count, 1)) if active_count > 0 else 0.5)
    health_score = (
        round(
            min(
                100.0,
                max(
                    0.0,
                    (retention_rate_pct * 0.40)
                    + (collection_efficiency_pct * 0.35)
                    + (engagement_ratio * 100.0 * 0.15)
                    + (10.0 if net_profit >= 0 else 4.0),
                ),
            ),
            1,
        )
        if total_members > 0
        else 100.0
    )

    package_mix = [
        {
            "plan": plan,
            "members_count": cnt,
            "share_pct": round((cnt / max(total_members, 1)) * 100.0, 1),
            "revenue_inr": round(plan_rev.get(plan, 0.0), 2),
        }
        for plan, cnt in sorted(plan_counts.items(), key=lambda x: (-x[1], x[0]))
    ]
    batch_trends = [
        {
            "batch_slot": slot,
            "members_count": cnt,
            "share_pct": round((cnt / max(total_members, 1)) * 100.0, 1),
        }
        for slot, cnt in sorted(batch_counts.items(), key=lambda x: (-x[1], x[0]))
    ]
    workout_trends = [
        {
            "workout_type": wt,
            "checkins_count": cnt,
            "share_pct": round((cnt / max(total_checkins, 1)) * 100.0, 1),
        }
        for wt, cnt in sorted(workout_counts.items(), key=lambda x: (-x[1], x[0]))
    ]

    insights = _collect_trend_insights(
        total_members=total_members,
        active_members=active_count,
        pending_dues_count=pending_count,
        pending_dues_amount=pending_amount,
        expiring_3d_count=len(expiring_3d),
        expiring_7d_count=len(expiring_7d),
        inactive_5d_count=len(inactive_5d),
        collection_efficiency_pct=collection_efficiency_pct,
        net_profit=net_profit,
        batch_trends=batch_trends,
    )

    return {
        "as_of_ist": timestamp_ist_str(),
        "studio_health_score": health_score,
        "retention_rate_pct": retention_rate_pct,
        "collection_efficiency_pct": collection_efficiency_pct,
        "average_revenue_per_member": arpm,
        "summary_counts": {
            "total_members": total_members,
            "active_members": active_count,
            "frozen_members": frozen_count,
            "expired_members": expired_count,
            "pending_dues_count": pending_count,
            "pending_dues_amount": round(pending_amount, 2),
            "checkins_today": checkins_today,
            "total_checkins": total_checkins,
        },
        "financial_pulse": {
            "gross_collections_inr": gross_collections,
            "gst_collected_inr": total_gst,
            "net_revenue_ex_gst_inr": net_revenue_ex_gst,
            "operating_expenses_inr": total_expenses,
            "net_profit_inr": net_profit,
        },
        "package_mix": package_mix,
        "batch_slot_popularity": batch_trends,
        "workout_type_trends": workout_trends,
        "churn_risk_radar": {
            "expiring_3d": expiring_3d,
            "expiring_7d": expiring_7d,
            "inactive_5d": inactive_5d,
            "pending_dues": pending_dues_list,
        },
        "top_streaks": get_attendance_leaderboard(limit=5),
        "automated_insights": insights,
    }


def get_executive_sheets_presentation() -> dict[str, Any]:
    """Build polished Excel & Google Sheets executive tabs for Gym Admins from Master DB."""
    today = today_ist_str()
    generated_at = timestamp_ist_str()
    settings = get_settings()
    trends = get_automated_trends_and_analysis()
    due_followups = evaluate_follow_ups(include_already_reminded_today=True)
    gym_settings = get_gym_settings()

    with get_db() as conn:
        member_rows = conn.execute("SELECT * FROM members ORDER BY created_at_ist DESC").fetchall()
        payment_rows = conn.execute("SELECT * FROM payments ORDER BY created_at_ist DESC").fetchall()

    counts = trends["summary_counts"]
    fin = trends["financial_pulse"]
    exec_summary_rows = [
        {"Metric Category": "Studio", "KPI": "Studio Name", "Current Value": gym_settings.get("gym_name", "Crush Lifestyle Fitness Studio"), "Status / Benchmark": "Master PostgreSQL Synced"},
        {"Metric Category": "Health", "KPI": "Studio Health Score", "Current Value": f"{trends['studio_health_score']} / 100", "Status / Benchmark": "Target >= 80"},
        {"Metric Category": "Members", "KPI": "Active / Total Members", "Current Value": f"{counts['active_members']} / {counts['total_members']}", "Status / Benchmark": f"Retention {trends['retention_rate_pct']}%"},
        {"Metric Category": "Members", "KPI": "Frozen / Expired Members", "Current Value": f"{counts['frozen_members']} Frozen · {counts['expired_members']} Expired", "Status / Benchmark": "Auto-evaluated daily"},
        {"Metric Category": "Finance", "KPI": "Gross Revenue Collected", "Current Value": fin["gross_collections_inr"], "Status / Benchmark": f"ARPM Rs.{trends['average_revenue_per_member']}"},
        {"Metric Category": "Finance", "KPI": "18% GST Collected (CGST+SGST)", "Current Value": fin["gst_collected_inr"], "Status / Benchmark": f"SAC {gym_settings.get('sac_code', '999723')}"},
        {"Metric Category": "Finance", "KPI": "Operating Expenses", "Current Value": fin["operating_expenses_inr"], "Status / Benchmark": "Synced from CFO Ledger"},
        {"Metric Category": "Finance", "KPI": "Net Studio Profit (Ex-GST)", "Current Value": fin["net_profit_inr"], "Status / Benchmark": f"Collection Efficiency {trends['collection_efficiency_pct']}%"},
        {"Metric Category": "Receivables", "KPI": "Pending Dues", "Current Value": f"Rs.{counts['pending_dues_amount']} ({counts['pending_dues_count']} members)", "Status / Benchmark": "Action via GM-02 Email"},
        {"Metric Category": "Engagement", "KPI": "Today's Floor Attendance", "Current Value": counts["checkins_today"], "Status / Benchmark": f"Lifetime Check-Ins: {counts['total_checkins']}"},
    ]

    customer_directory_rows = []
    for r in member_rows:
        m = row_to_member_response(r, today=today)
        customer_directory_rows.append({
            "Member ID": m.member_id,
            "Full Name": m.name,
            "WhatsApp / Phone": f"+{m.phone}",
            "Email": m.email or "—",
            "Package Plan": "Annual" if m.plan == "Yearly" else m.plan,
            "Fee (INR)": m.fee,
            "Start Date": m.start_date,
            "Expiry Date": m.expiry_date,
            "Days Remaining": m.days_remaining,
            "Member Status": m.member_status,
            "Payment Status": m.payment_status,
            "Streak (Days)": m.current_streak,
            "Total Visits": m.total_checkins,
            "Assigned Trainer": m.assigned_trainer,
            "Batch Slot": m.batch_slot,
            "Next Automated Follow-Up": m.next_followup_type or "UP_TO_DATE",
        })

    gst_ledger_rows = []
    for p in payment_rows:
        amt = round(float(p["amount_paid"] or 0.0), 2)
        gst = round(float(p["gst_amount"] or 0.0), 2)
        cgst = round(gst / 2.0, 2)
        sgst = round(gst - cgst, 2)
        gst_ledger_rows.append({
            "Invoice No": p["invoice_number"],
            "Payment Date": p["payment_date"],
            "Member ID": p["member_id"],
            "Member Name": p["member_name"],
            "Plan": "Annual" if p["plan"] == "Yearly" else p["plan"],
            "Taxable Value (INR)": round(amt - gst, 2),
            "CGST 9% (INR)": cgst,
            "SGST 9% (INR)": sgst,
            "Total Paid (INR)": amt,
            "Payment Mode": p["payment_mode"],
            "Status": p["status"],
            "New Expiry": p["new_expiry_date"],
        })

    trends_rows: list[dict[str, Any]] = []
    for pm in trends["package_mix"]:
        trends_rows.append({
            "Analysis Group": "Package Mix",
            "Segment": "Annual" if pm["plan"] == "Yearly" else pm["plan"],
            "Members / Volume": pm["members_count"],
            "Share (%)": pm["share_pct"],
            "Revenue (INR)": pm["revenue_inr"],
            "Recommended Action": "High-LTV tier" if pm["plan"] in ("Yearly", "Half-Yearly") else "Upsell to Quarterly/Annual",
        })
    for bt in trends["batch_slot_popularity"]:
        trends_rows.append({
            "Analysis Group": "Batch Demand",
            "Segment": bt["batch_slot"],
            "Members / Volume": bt["members_count"],
            "Share (%)": bt["share_pct"],
            "Revenue (INR)": "—",
            "Recommended Action": "Optimize trainer coverage for this batch",
        })
    for ins in trends["automated_insights"]:
        trends_rows.append({
            "Analysis Group": f"AI Insight ({ins['severity']})",
            "Segment": ins["category"],
            "Members / Volume": "Live",
            "Share (%)": "—",
            "Revenue (INR)": "—",
            "Recommended Action": f"{ins['title']} -> {ins['action_hint']}",
        })

    followup_queue_rows = [
        {
            "Priority": f.priority,
            "Follow-Up Stage": f.followup_type,
            "Member ID": f.member_id,
            "Member Name": f.name,
            "Phone": f"+{f.phone}",
            "Plan": "Annual" if f.plan == "Yearly" else f.plan,
            "Expiry Date": f.expiry_date,
            "Payment Status": f.payment_status,
            "Channel": f.channel,
            "Automated Message": f.message_text,
        }
        for f in due_followups
    ]

    tabs = {
        "Executive Summary & KPIs": exec_summary_rows,
        "Customer Master Directory": customer_directory_rows,
        "GST Billing & Revenue Ledger": gst_ledger_rows,
        "Automated Trends & Cohort Insights": trends_rows,
        "Retention & Follow-Up Queue": followup_queue_rows,
    }
    return {
        "generated_at_ist": generated_at,
        "spreadsheet_id": settings.google_sheets_spreadsheet_id,
        "spreadsheet_name": settings.google_sheets_spreadsheet_name,
        "tab_order": list(tabs.keys()),
        "counts": {k: len(v) for k, v in tabs.items()},
        "tabs": tabs,
    }
