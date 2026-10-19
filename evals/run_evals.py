"""End-to-end evals: scripted callers talk to the real agent (text mode), then we assert on database state.

Run:  python -m evals.run_evals            (needs ANTHROPIC_API_KEY)
"""
import asyncio
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable

from app.agent import Agent
from app.calllog import CallLog
from app.config import Settings
from app.scheduler import Business, Scheduler
from app.session import Session
from app.tools import Deps

PHONE = "+15551230001"


@dataclass
class Scenario:
    name: str
    turns: list[str]
    check: Callable[[Deps, Session], bool]
    setup: Callable[[Deps], None] = lambda d: None
    nudges: int = 3  # generic "yes, go ahead" replies allowed if the agent is still confirming


def next_weekday(deps: Deps, weekday: int):
    d = deps.scheduler.now().date() + timedelta(days=1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def build_scenarios(deps: Deps) -> list[Scenario]:
    tue, sun = next_weekday(deps, 1), next_weekday(deps, 6)

    def booked_oil_change(d, s):
        return any(a["service"] == "oil_change" and a["start"].startswith(tue.isoformat()) for a in d.scheduler.find_upcoming(PHONE))

    def seed(d):
        d.scheduler.book(PHONE, "Sam Rivera", "oil_change", datetime.combine(tue, time(10)))

    return [
        Scenario(
            "book_oil_change",
            [f"Hi, I'd like to book an oil change on {tue:%B} {tue.day}.", "My name is Sam Rivera and it's a 2018 Toyota Camry.", "The earliest time you have works."],
            booked_oil_change,
        ),
        Scenario(
            "cancel_existing",
            ["Hi, I need to cancel my appointment."],
            lambda d, s: not d.scheduler.find_upcoming(PHONE),
            setup=seed,
        ),
        Scenario(
            "closed_day_not_booked",
            [f"Can you book an oil change this coming Sunday, {sun:%B} {sun.day}, at 3 in the afternoon?"],
            lambda d, s: not d.scheduler.list_all(PHONE),
            nudges=0,
        ),
        Scenario(
            "emergency_ends_call",
            ["I smell gas coming from my garage and I think there's a leak."],
            lambda d, s: s.pending_end is not None,
            nudges=0,
        ),
        Scenario(
            "asks_for_human",
            ["This is ridiculous, I want to talk to a real person right now."],
            lambda d, s: bool(s.pending_end) and s.pending_end["action"] == "transfer",
            nudges=0,
        ),
    ]


async def run_one(sc: Scenario, deps: Deps, agent: Agent) -> bool:
    session = Session(call_sid=f"eval-{sc.name}", phone=PHONE)
    sc.setup(deps)
    transcript = []

    async def sink(tok):
        transcript.append(tok)

    for line in sc.turns:
        await agent.respond(session, line, sink)
    for _ in range(sc.nudges):
        if sc.check(deps, session):
            break
        await agent.respond(session, "Yes, that's correct. Please go ahead.", sink)
    return sc.check(deps, session)


async def main() -> int:
    results = []
    names = [sc.name for sc in build_scenarios(_fresh_deps())]
    for name in names:
        deps = _fresh_deps()  # fresh DB per scenario so they can't contaminate each other
        sc = next(x for x in build_scenarios(deps) if x.name == name)
        ok = await run_one(sc, deps, Agent(deps))
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


def _fresh_deps() -> Deps:
    settings = Settings(database_path=str(Path(tempfile.mkdtemp()) / "eval.db"), human_transfer_number="+15555550100")
    business = Business.load(settings.business_config)
    return Deps(settings, business, Scheduler(business, settings.database_path), CallLog(settings.database_path))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
