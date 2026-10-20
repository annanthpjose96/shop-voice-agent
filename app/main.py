import asyncio
import html
import json
import logging
from pathlib import Path
from xml.sax.saxutils import quoteattr

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from twilio.request_validator import RequestValidator

from .agent import Agent
from .calllog import CallLog
from .config import Settings
from .scheduler import Business, Scheduler
from .session import Session
from .tools import Deps

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice")

settings = Settings()
business = Business.load(settings.business_config)
scheduler = Scheduler(business, settings.database_path)
deps = Deps(settings, business, scheduler, CallLog(settings.database_path))
agent = Agent(deps)
app = FastAPI(title="Shop voice agent")


def _signature_ok(signature: str | None, url: str, params: dict) -> bool:
    if not settings.validate_twilio_signature:
        return True
    return RequestValidator(settings.twilio_auth_token).validate(url, params, signature or "")


def _greeting() -> str:
    return (
        f"Thanks for calling {business.name}. I'm the shop's virtual assistant, and this call may be "
        "logged to help us improve. How can I help you today?"
    )


def _xml(body: str) -> Response:
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="text/xml")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/voice")
async def voice(request: Request):
    params = dict(await request.form())
    if not _signature_ok(request.headers.get("X-Twilio-Signature"), f"https://{settings.public_host}/voice", params):
        return Response("forbidden", status_code=403)
    greeting = _greeting()
    attrs = {
        "url": f"wss://{settings.public_host}/ws",
        "welcomeGreeting": greeting,
        "ttsProvider": settings.tts_provider,
        "transcriptionProvider": settings.stt_provider,
        "language": "en-US",
        "interruptible": "true",
    }
    if settings.tts_voice:
        attrs["voice"] = settings.tts_voice
    attr_str = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
    action = quoteattr(f"https://{settings.public_host}/handoff")
    return _xml(f"<Connect action={action}><ConversationRelay {attr_str}/></Connect>")


@app.post("/handoff")
async def handoff(request: Request):
    """Twilio calls this when the relay session ends. We either forward the call or hang up."""
    params = dict(await request.form())
    if not _signature_ok(request.headers.get("X-Twilio-Signature"), f"https://{settings.public_host}/handoff", params):
        return Response("forbidden", status_code=403)
    try:
        data = json.loads(params.get("HandoffData") or "{}")
    except json.JSONDecodeError:
        data = {}
    if data.get("action") == "transfer" and settings.human_transfer_number:
        return _xml(f"<Dial>{settings.human_transfer_number}</Dial>")
    return _xml("<Hangup/>")


async def handle_turn(send, session: Session, text: str) -> None:
    async def send_token(tok: str):
        await send({"type": "text", "token": tok, "last": False})

    try:
        await agent.respond(session, text, send_token)
        session.errors = 0
    except asyncio.CancelledError:
        raise  # caller barged in; don't close the utterance
    except Exception as e:
        log.exception("turn failed")
        deps.calllog.log(session.call_sid, "error", repr(e))
        session.errors += 1
        msg = "Sorry, I'm having trouble on my end."
        if session.errors >= 2:
            if settings.human_transfer_number:
                session.pending_end = {"action": "transfer", "reason": "system errors"}
                msg += " Let me connect you with someone."
            else:
                session.pending_end = {"action": "hangup", "reason": "system errors"}
                msg += " Please call back in a few minutes."
        await send_token(msg)
    await send({"type": "text", "token": "", "last": True})
    if session.pending_end:
        await asyncio.sleep(settings.end_delay_seconds)
        deps.calllog.log(session.call_sid, "end", session.pending_end)
        await send({"type": "end", "handoffData": json.dumps(session.pending_end)})


@app.get("/test")
async def test_page():
    """Browser voice test. Development only: enable with ENABLE_TEST_UI=true."""
    if not settings.enable_test_ui:
        return Response("Test page is disabled. Set ENABLE_TEST_UI=true in .env and restart.", status_code=404)
    page = (Path(__file__).parent / "voice_test.html").read_text(encoding="utf-8")
    page = page.replace("__GREETING__", json.dumps(_greeting()).replace("</", "<\\/"))
    return HTMLResponse(page.replace("__BUSINESS__", html.escape(business.name)))


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    if not _signature_ok(websocket.headers.get("X-Twilio-Signature"), f"wss://{settings.public_host}/ws", {}):
        await websocket.close(code=1008)
        return
    await _serve_call(websocket)


@app.websocket("/ws-test")
async def ws_test(websocket: WebSocket):
    """Same call logic as /ws, without Twilio's signature, for the browser test page only."""
    if not settings.enable_test_ui:
        await websocket.close(code=1008)
        return
    await _serve_call(websocket)


async def _serve_call(websocket: WebSocket):
    await websocket.accept()
    session: Session | None = None
    task: asyncio.Task | None = None

    async def send(obj: dict):
        await websocket.send_text(json.dumps(obj))

    async def cancel_turn():
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            kind = msg.get("type")
            if kind == "setup":
                session = Session(call_sid=msg.get("callSid", "unknown"), phone=msg.get("from", ""))
                deps.calllog.log(session.call_sid, "start", {"from": session.phone})
            elif kind == "prompt" and session:
                text = (msg.get("voicePrompt") or "").strip()
                if text:
                    await cancel_turn()
                    task = asyncio.create_task(handle_turn(send, session, text))
            elif kind == "interrupt" and session:
                await cancel_turn()
                session.truncate_last_assistant(msg.get("utteranceUntilInterrupt", ""))
            elif kind == "error":
                deps.calllog.log(session.call_sid if session else "unknown", "twilio_error", msg)
    except WebSocketDisconnect:
        pass
    finally:
        await cancel_turn()
        if session:
            deps.calllog.log(session.call_sid, "disconnect", {})
