"""Business rules live here, in code, not in the prompt. The LLM can only propose; this decides."""
from __future__ import annotations

import json
import re
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .db import connect, init_db, transaction

SLOT_MINUTES = 30
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ISO = "%Y-%m-%dT%H:%M"


class BookingError(Exception):
    """A rule violation whose message is safe to hand back to the model."""


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    minutes: int
    description: str = ""


@dataclass(frozen=True)
class Business:
    name: str
    timezone: str
    bays: int
    hours: dict
    services: dict
    min_notice_minutes: int = 60
    booking_window_days: int = 45
    max_upcoming_per_phone: int = 3

    @classmethod
    def load(cls, path: str) -> "Business":
        raw = json.loads(Path(path).read_text())
        services = {s["key"]: Service(**s) for s in raw.pop("services")}
        return cls(services=services, **raw)

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None, second=0, microsecond=0)


def normalize_phone(raw: str) -> str:
    return re.sub(r"[^\d+]", "", raw or "")


def speak_dt(dt: datetime) -> str:
    return dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")


def speak_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


class Scheduler:
    def __init__(self, business: Business, db_path: str, now_fn: Callable[[], datetime] | None = None):
        self.b = business
        self.db_path = db_path
        self.now = now_fn or business.now
        self._lock = threading.Lock()
        init_db(db_path)

    # ---------- helpers ----------
    def _service(self, key: str) -> Service:
        if key not in self.b.services:
            raise BookingError(f"Unknown service '{key}'. Valid: {', '.join(self.b.services)}")
        return self.b.services[key]

    def _window(self, day: date):
        hours = self.b.hours.get(DAYS[day.weekday()])
        if not hours:
            return None
        o = datetime.strptime(hours[0], "%H:%M").time()
        c = datetime.strptime(hours[1], "%H:%M").time()
        return datetime.combine(day, o), datetime.combine(day, c)

    def _booked(self, conn, lo: datetime, hi: datetime, exclude_id: int | None = None):
        rows = conn.execute(
            "SELECT id, start_at, end_at FROM appointments WHERE status='booked' AND start_at < ? AND end_at > ?",
            (hi.strftime(ISO), lo.strftime(ISO)),
        ).fetchall()
        return [
            (datetime.strptime(r["start_at"], ISO), datetime.strptime(r["end_at"], ISO))
            for r in rows
            if r["id"] != exclude_id
        ]

    def _fits(self, booked, start: datetime, end: datetime) -> bool:
        """True if at no instant in [start, end) are all bays already taken."""
        points = [start] + [s for s, _ in booked if start < s < end]
        for t in points:
            if sum(1 for s, e in booked if s <= t < e) >= self.b.bays:
                return False
        return True

    def _validate(self, svc: Service, start: datetime) -> datetime:
        if start.minute % SLOT_MINUTES or start.second:
            raise BookingError("Appointments start on the hour or half hour.")
        if start < self.now() + timedelta(minutes=self.b.min_notice_minutes):
            raise BookingError(f"We need at least {self.b.min_notice_minutes} minutes notice.")
        if start.date() > self.now().date() + timedelta(days=self.b.booking_window_days):
            raise BookingError(f"We only book up to {self.b.booking_window_days} days ahead.")
        window = self._window(start.date())
        if not window:
            raise BookingError(f"We're closed on {start.strftime('%A')}s.")
        end = start + timedelta(minutes=svc.minutes)
        if start < window[0] or end > window[1]:
            raise BookingError(
                f"That time is outside our hours for {start.strftime('%A')} "
                f"({speak_time(window[0])} to {speak_time(window[1])}, and the job must finish by closing)."
            )
        return end

    def _row(self, r, **extra) -> dict:
        start = datetime.strptime(r["start_at"], ISO)
        svc = self.b.services.get(r["service"])
        return {
            "id": r["id"],
            "service": r["service"],
            "service_name": svc.name if svc else r["service"],
            "customer_name": r["name"],
            "start": r["start_at"],
            "spoken": speak_dt(start),
            "notes": r["notes"],
            **extra,
        }

    # ---------- public API (sync; callers wrap in a thread) ----------
    def availability(self, service_key: str, day: date) -> list[datetime]:
        svc = self._service(service_key)
        window = self._window(day)
        if not window:
            return []
        if day > self.now().date() + timedelta(days=self.b.booking_window_days):
            return []
        open_dt, close_dt = window
        earliest = self.now() + timedelta(minutes=self.b.min_notice_minutes)
        with closing(connect(self.db_path)) as conn:
            booked = self._booked(conn, open_dt, close_dt)
        out, t, dur = [], open_dt, timedelta(minutes=svc.minutes)
        while t + dur <= close_dt:
            if t >= earliest and self._fits(booked, t, t + dur):
                out.append(t)
            t += timedelta(minutes=SLOT_MINUTES)
        return out

    def book(self, phone: str, name: str, service_key: str, start: datetime, notes: str = "") -> dict:
        svc = self._service(service_key)
        end = self._validate(svc, start)
        name = name.strip()
        if not name:
            raise BookingError("A customer name is required.")
        with self._lock, closing(connect(self.db_path)) as conn, transaction(conn):
            dup = conn.execute(
                "SELECT * FROM appointments WHERE phone=? AND service=? AND start_at=? AND status='booked'",
                (phone, service_key, start.strftime(ISO)),
            ).fetchone()
            if dup:  # idempotent: a retried tool call never double-books
                return self._row(dup, already_booked=True)
            upcoming = conn.execute(
                "SELECT COUNT(*) c FROM appointments WHERE phone=? AND status='booked' AND start_at>=?",
                (phone, self.now().strftime(ISO)),
            ).fetchone()["c"]
            if upcoming >= self.b.max_upcoming_per_phone:
                raise BookingError("This number already has the maximum upcoming appointments. Offer a transfer to staff.")
            if not self._fits(self._booked(conn, start, end), start, end):
                raise BookingError("That time was just taken. Check availability again.")
            cur = conn.execute(
                "INSERT INTO appointments (phone, name, service, start_at, end_at, notes, created_at) VALUES (?,?,?,?,?,?,?)",
                (phone, name, service_key, start.strftime(ISO), end.strftime(ISO), notes[:300], self.now().strftime(ISO)),
            )
            row = conn.execute("SELECT * FROM appointments WHERE id=?", (cur.lastrowid,)).fetchone()
            return self._row(row)

    def find_upcoming(self, phone: str) -> list[dict]:
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE phone=? AND status='booked' AND start_at>=? ORDER BY start_at",
                (phone, self.now().strftime(ISO)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def list_all(self, phone: str) -> list[dict]:
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute("SELECT * FROM appointments WHERE phone=? ORDER BY start_at", (phone,)).fetchall()
        return [self._row(r, status=r["status"]) for r in rows]

    def _owned(self, conn, appt_id: int, phone: str):
        row = conn.execute(
            "SELECT * FROM appointments WHERE id=? AND phone=? AND status='booked' AND start_at>=?",
            (appt_id, phone, self.now().strftime(ISO)),
        ).fetchone()
        if not row:
            raise BookingError("No matching upcoming appointment for this caller.")
        return row

    def reschedule(self, appt_id: int, phone: str, new_start: datetime) -> dict:
        with self._lock, closing(connect(self.db_path)) as conn, transaction(conn):
            row = self._owned(conn, appt_id, phone)
            svc = self._service(row["service"])
            end = self._validate(svc, new_start)
            if not self._fits(self._booked(conn, new_start, end, exclude_id=appt_id), new_start, end):
                raise BookingError("That time isn't available. Check availability again.")
            conn.execute(
                "UPDATE appointments SET start_at=?, end_at=? WHERE id=?",
                (new_start.strftime(ISO), end.strftime(ISO), appt_id),
            )
            return self._row(conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone())

    def cancel(self, appt_id: int, phone: str) -> dict:
        with self._lock, closing(connect(self.db_path)) as conn, transaction(conn):
            row = self._owned(conn, appt_id, phone)
            conn.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appt_id,))
            return self._row(row, cancelled=True)
