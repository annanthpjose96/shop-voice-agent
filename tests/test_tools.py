import asyncio
from datetime import datetime

from app.calllog import CallLog
from app.config import Settings
from app.scheduler import Business, Scheduler
from app.session import Session
from app.tools import Deps, run_tool

NOW = datetime(2026, 10, 19, 7, 0)  # Monday


def make(tmp_path):
    s = Settings(database_path=str(tmp_path / "t.db"))
    b = Business.load("business.json")
    d = Deps(s, b, Scheduler(b, s.database_path, now_fn=lambda: NOW), CallLog(s.database_path))
    return d, Session("CAtest", "+15551230001")


def run(d, sess, name, args):
    return asyncio.run(run_tool(d, sess, name, args))


def test_cannot_book_unchecked_time(tmp_path):
    d, sess = make(tmp_path)
    out = run(d, sess, "book_appointment", {"customer_name": "Sam", "service": "oil_change", "start": "2026-10-22T09:30"})
    assert out["ok"] is False and "check_availability" in out["error"]
    assert d.scheduler.find_upcoming(sess.phone) == []


def test_wrong_date_is_rejected_then_corrected(tmp_path):
    d, sess = make(tmp_path)
    r = run(d, sess, "check_availability", {"service": "oil_change", "date": "2026-10-20"})
    assert r["ok"] and r["day"] == "Tuesday, October 20"
    bad = run(d, sess, "book_appointment", {"customer_name": "Sam", "service": "oil_change", "start": "2026-10-22T09:30"})
    assert bad["ok"] is False  # model offered Tuesday but tried to book Thursday
    good = run(d, sess, "book_appointment", {"customer_name": "Sam", "service": "oil_change", "start": "2026-10-20T09:30"})
    assert good["ok"] and good["appointment"]["spoken"].startswith("Tuesday, October 20")
