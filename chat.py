"""Type to your voice agent in the terminal (no phone or Twilio needed).

Run:  python chat.py
Type 'quit' to stop. Tool calls are shown in [brackets] so you can see what the agent does.
"""
import asyncio
import json

import app.agent as agent_mod
from app.agent import Agent
from app.calllog import CallLog
from app.config import Settings
from app.scheduler import Business, Scheduler
from app.session import Session
from app.tools import Deps

PHONE = "+15551234567"  # pretend caller ID

settings = Settings()
business = Business.load(settings.business_config)
scheduler = Scheduler(business, settings.database_path)
deps = Deps(settings, business, scheduler, CallLog(settings.database_path))

_orig_run_tool = agent_mod.run_tool


async def _traced(d, session, name, args):
    out = await _orig_run_tool(d, session, name, args)
    print(f"\n   [tool] {name} {json.dumps(args)}\n   [result] {json.dumps(out)[:300]}")
    return out


agent_mod.run_tool = _traced


async def main():
    agent = Agent(deps)
    session = Session(call_sid="chat-test", phone=PHONE)
    print(f"Thanks for calling {business.name}. I'm the shop's virtual assistant. How can I help you today?\n")
    while True:
        try:
            text = (await asyncio.to_thread(input, "You: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit"}:
            break
        if not text:
            continue
        print("Agent: ", end="", flush=True)

        async def show(tok):
            print(tok, end="", flush=True)

        await agent.respond(session, text, show)
        print()
        if session.pending_end:
            print(f"\n[call ended: {session.pending_end['action']}]")
            break


if __name__ == "__main__":
    asyncio.run(main())