from datetime import date, datetime

import pytest

from app.scheduler import Business, BookingError, Scheduler

NOW = datetime(2026, 10, 19, 7, 0)  # Monday 7:00
TUE = date(2026, 10, 20)
PHONE = "+15551230001"
OTHER = "+15551230002"


@pytest.fixture
def sched(tmp_path):
    biz = Business.load("business.json")
    return Scheduler(biz, str(tmp_path / "t.db"), now_fn=lambda: NOW)


def at(h, m=0, day=TUE):
    return datetime(day.year, day.month, day.day, h, m)


def test_availability_basic(sched):
    slots = sched.availability("oil_change", TUE)
    assert slots[0] == at(8) and slots[-1] == at(16, 30)


def test_closed_sunday(sched):
    assert sched.availability("oil_change", date(2026, 10, 25)) == []
    with pytest.raises(BookingError):
        sched.book(PHONE, "Sam", "oil_change", at(10, day=date(2026, 10, 25)))


def test_job_must_finish_before_close(sched):
    with pytest.raises(BookingError):
        sched.book(PHONE, "Sam", "ac_service", at(16))  # 90 min, closes at 17:00
    assert at(16) not in sched.availability("ac_service", TUE)


def test_capacity_two_bays(sched):
    sched.book(PHONE, "A", "brake_inspection", at(10))
    sched.book(OTHER, "B", "brake_inspection", at(10))
    assert at(10) not in sched.availability("oil_change", TUE)
    with pytest.raises(BookingError):
        sched.book("+15551230003", "C", "oil_change", at(10, 30))  # overlaps both 60-min jobs
    assert at(11) in sched.availability("oil_change", TUE)


def test_capacity_exact_not_overcounted(sched):
    sched.book(PHONE, "A", "oil_change", at(10))        # 10:00-10:30
    sched.book(OTHER, "B", "oil_change", at(10, 30))    # 10:30-11:00
    # A 60 minute job at 10:00 sees at most one busy bay at any instant, so it fits.
    sched.book("+15551230003", "C", "brake_inspection", at(10))


def test_idempotent_booking(sched):
    a = sched.book(PHONE, "Sam", "oil_change", at(9))
    b = sched.book(PHONE, "Sam", "oil_change", at(9))
    assert a["id"] == b["id"] and b["already_booked"]
    assert len(sched.find_upcoming(PHONE)) == 1


def test_min_notice_and_alignment(sched):
    sched.now = lambda: datetime(2026, 10, 19, 7, 30)  # 8:00 is now only 30 minutes away
    with pytest.raises(BookingError):
        sched.book(PHONE, "Sam", "oil_change", at(8, day=date(2026, 10, 19)))
    with pytest.raises(BookingError):
        sched.book(PHONE, "Sam", "oil_change", at(9, 15))


def test_exactly_min_notice_allowed(sched):
    appt = sched.book(PHONE, "Sam", "oil_change", at(8, day=date(2026, 10, 19)))
    assert appt["start"].endswith("08:00")


def test_per_phone_limit(sched):
    for h in (9, 10, 11):
        sched.book(PHONE, "Sam", "oil_change", at(h))
    with pytest.raises(BookingError):
        sched.book(PHONE, "Sam", "oil_change", at(12))


def test_ownership_enforced(sched):
    appt = sched.book(PHONE, "Sam", "oil_change", at(9))
    with pytest.raises(BookingError):
        sched.cancel(appt["id"], OTHER)
    with pytest.raises(BookingError):
        sched.reschedule(appt["id"], OTHER, at(10))
    assert len(sched.find_upcoming(PHONE)) == 1


def test_reschedule_and_cancel(sched):
    appt = sched.book(PHONE, "Sam", "oil_change", at(9))
    moved = sched.reschedule(appt["id"], PHONE, at(14))
    assert moved["start"].endswith("14:00")
    sched.cancel(appt["id"], PHONE)
    assert sched.find_upcoming(PHONE) == []
    assert at(14) in sched.availability("oil_change", TUE)


def test_reschedule_into_own_slot_ignores_self(sched):
    appt = sched.book(PHONE, "Sam", "brake_inspection", at(9))
    sched.book(OTHER, "B", "brake_inspection", at(9))
    sched.reschedule(appt["id"], PHONE, at(9, 30))  # overlaps its own old time only
