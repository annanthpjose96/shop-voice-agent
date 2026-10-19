import asyncio
import re
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta

from .calllog import CallLog
from .config import Settings
from .scheduler import Business, BookingError, Scheduler, normalize_phone, speak_dt, speak_time
from .session import Session


@dataclass
class Deps:
    settings: Settings
    business: Business
    scheduler: Scheduler
    calllog: CallLog


TOOLS = [
    {
        "name": "check_availability",
        "description": "List open appointment times for a service on a date. Always call this before offering times.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service key"},
                "date": {"type": "string", "description": "YYYY-MM-DD in shop local time"},
            },
            "required": ["service", "date"],
        },
    },
    {
        "name": "book_appointment",
        "description": "Book an appointment after the caller has confirmed the details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "service": {"type": "string", "description": "Service key"},
                "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM, shop local time, from check_availability"},
                "notes": {"type": "string", "description": "Vehicle or problem description"},
                "callback_number": {"type": "string", "description": "Only if caller ID is unavailable or caller wants a different number"},
            },
            "required": ["customer_name", "service", "start"],
        },
    },
    {
        "name": "list_my_appointments",
        "description": "List this caller's upcoming appointments (matched by caller ID).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reschedule_appointment",
        "description": "Move one of the caller's appointments to a new time, after confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {"appointment_id": {"type": "integer"}, "new_start": {"type": "string", "description": "YYYY-MM-DDTHH:MM"}},
            "required": ["appointment_id", "new_start"],
        },
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel one of the caller's appointments, after confirmation.",
        "input_schema": {"type": "object", "properties": {"appointment_id": {"type": "integer"}}, "required": ["appointment_id"]},
    },
    {
        "name": "take_message",
        "description": "Record a message for staff to follow up on.",
        "input_schema": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "message": {"type": "string"},
                "callback_number": {"type": "string"},
            },
            "required": ["caller_name", "message"],
        },
    },
    {
        "name": "transfer_to_human",
        "description": "Hand the call to a person. Say a brief sentence like 'One moment, I'll connect you' in the same turn.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "end_call",
        "description": "Hang up. Say goodbye in the same turn.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _caller_phone(session: Session) -> str:
    p = normalize_phone(session.phone)
    if len(re.sub(r"\D", "", p)) < 7:
        raise BookingError("Caller ID is unavailable, so existing appointments can't be accessed. Offer to take a message or transfer.")
    return p


def _day_words(d) -> str:
    return d.strftime("%A, %B %d").replace(" 0", " ")


def _require_checked(session: Session, start: datetime) -> None:
    if start.strftime("%Y-%m-%dT%H:%M") not in session.offered:
        raise BookingError(
            f"{_day_words(start)} at {speak_time(start)} has not been checked. "
            "Call check_availability for that exact date first, then use one of the times it returns."
        )


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(second=0, microsecond=0, tzinfo=None)


def _check_availability(d: Deps, session: Session, args: dict) -> dict:
    day = Date.fromisoformat(args["date"])
    service = args["service"]

    def summarize(slots):
        step = max(1, len(slots) // 6)
        picked = slots[::step][:6]
        return [{"start": s.strftime("%Y-%m-%dT%H:%M"), "spoken": speak_time(s)} for s in picked]

    slots = d.scheduler.availability(service, day)
    session.offered.update(s.strftime("%Y-%m-%dT%H:%M") for s in slots)
    if slots:
        return {"ok": True, "date": args["date"], "day": _day_words(day), "open_slots": len(slots), "sample_slots": summarize(slots)}
    for i in range(1, 15):  # nothing that day: find the next day that has room
        nxt = day + timedelta(days=i)
        later = d.scheduler.availability(service, nxt)
        if later:
            session.offered.update(s.strftime("%Y-%m-%dT%H:%M") for s in later)
            return {
                "ok": True,
                "date": args["date"],
                "day": _day_words(day),
                "open_slots": 0,
                "next_available_date": nxt.isoformat(),
                "next_available_spoken": _day_words(nxt),
                "sample_slots": summarize(later),
            }
    return {"ok": True, "date": args["date"], "open_slots": 0, "note": "Nothing open in the next two weeks."}


def _book(d: Deps, session: Session, args: dict) -> dict:
    phone = normalize_phone(args.get("callback_number") or "") or normalize_phone(session.phone)
    if len(re.sub(r"\D", "", phone)) < 7:
        raise BookingError("No valid phone number. Ask the caller for a callback number.")
    start = _parse_dt(args["start"])
    _require_checked(session, start)
    appt = d.scheduler.book(phone, args["customer_name"], args["service"], start, args.get("notes", ""))
    return {"ok": True, "appointment": appt}


def _list(d: Deps, session: Session, args: dict) -> dict:
    return {"ok": True, "appointments": d.scheduler.find_upcoming(_caller_phone(session))}


def _reschedule(d: Deps, session: Session, args: dict) -> dict:
    new_start = _parse_dt(args["new_start"])
    _require_checked(session, new_start)
    appt = d.scheduler.reschedule(int(args["appointment_id"]), _caller_phone(session), new_start)
    return {"ok": True, "appointment": appt}


def _cancel(d: Deps, session: Session, args: dict) -> dict:
    return {"ok": True, "cancelled": d.scheduler.cancel(int(args["appointment_id"]), _caller_phone(session))}


def _take_message(d: Deps, session: Session, args: dict) -> dict:
    d.calllog.save_message(
        session.call_sid, session.phone, args["caller_name"], args["message"][:1000],
        args.get("callback_number") or session.phone,
    )
    return {"ok": True}


def _transfer(d: Deps, session: Session, args: dict) -> dict:
    if not d.settings.human_transfer_number:
        return {"ok": False, "error": "No one is available to take transfers. Use take_message instead."}
    session.pending_end = {"action": "transfer", "reason": args.get("reason", "")[:200]}
    return {"ok": True, "note": "Transfer will happen after you finish speaking."}


def _end_call(d: Deps, session: Session, args: dict) -> dict:
    session.pending_end = {"action": "hangup", "reason": "completed"}
    return {"ok": True}


HANDLERS = {
    "check_availability": _check_availability,
    "book_appointment": _book,
    "list_my_appointments": _list,
    "reschedule_appointment": _reschedule,
    "cancel_appointment": _cancel,
    "take_message": _take_message,
    "transfer_to_human": _transfer,
    "end_call": _end_call,
}


async def run_tool(deps: Deps, session: Session, name: str, args: dict) -> dict:
    handler = HANDLERS.get(name)
    if not handler:
        result = {"ok": False, "error": f"Unknown tool {name}"}
    else:
        try:
            result = await asyncio.to_thread(handler, deps, session, args)
        except BookingError as e:
            result = {"ok": False, "error": str(e)}
        except (KeyError, ValueError, TypeError) as e:
            result = {"ok": False, "error": f"Invalid input: {e}"}
    deps.calllog.log(session.call_sid, "tool", {"name": name, "args": args, "result": result})
    return result
