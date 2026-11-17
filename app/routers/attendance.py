"""Attendance check-in and Cult.fit-style workout streak endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..config import today_ist_str
from ..database import get_db
from ..schemas import AttendanceCheckInRequest
from ..services import membership_service

router = APIRouter(prefix="/api/v1/attendance", tags=["Attendance"])


@router.post("/check-in")
def member_checkin(payload: AttendanceCheckInRequest):
    """Check in a member by member_id or phone and update workout streak counters."""
    try:
        return membership_service.record_checkin(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/today")
def get_today_checkins():
    """Return today's check-in feed in Asia/Kolkata timezone."""
    today = today_ist_str()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT a.*, m.name, m.phone, m.plan, m.current_streak
            FROM attendance a
            JOIN members m ON a.member_id = m.member_id
            WHERE a.checkin_date = ?
            ORDER BY a.checkin_time_ist DESC
            """,
            (today,),
        ).fetchall()
    return {"date": today, "count": len(rows), "checkins": [dict(r) for r in rows]}


@router.get("/history")
def list_attendance_history(
    member_id: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    workout_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """List filterable attendance history joined with member streak metadata."""
    items = membership_service.get_attendance_history(
        member_id=member_id,
        date_from=date_from,
        date_to=date_to,
        workout_type=workout_type,
        limit=limit,
    )
    return {"count": len(items), "checkins": items}


@router.get("/leaderboard")
def get_streak_leaderboard(limit: int = Query(default=10, ge=1, le=100)):
    """Return top gym members ranked by workout consistency streak."""
    leaders = membership_service.get_attendance_leaderboard(limit=limit)
    return {"count": len(leaders), "leaderboard": leaders}
