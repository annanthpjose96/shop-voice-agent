import asyncio
import json
from typing import Awaitable, Callable

from openai import AsyncOpenAI, BadRequestError

from .prompts import build_system_prompt
from .session import Session
from .tools import TOOLS, Deps, run_tool

SendToken = Callable[[str], Awaitable[None]]
FALLBACK = "Sorry, I'm having trouble with that. Could you say it again?"

# Works with any OpenAI-compatible provider: Groq, Gemini, OpenRouter, Ollama, OpenAI...
OPENAI_TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS
]


class Agent:
    def __init__(self, deps: Deps):
        self.deps = deps
        s = deps.settings
        self.client = AsyncOpenAI(api_key=s.llm_api_key or "none", base_url=s.llm_base_url, max_retries=2, timeout=60.0)

    async def _call_model(self, session: Session, messages: list, emit: SendToken):
        """Stream one model response. Returns (assistant_message, tool_calls)."""
        system = build_system_prompt(self.deps.business, self.deps.scheduler.now(), bool(session.phone))
        payload = [{"role": "system", "content": system}] + messages
        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        for attempt in range(2):
            try:
                stream = await self.client.chat.completions.create(
                    model=self.deps.settings.model,
                    messages=payload,
                    tools=OPENAI_TOOLS,
                    tool_choice="auto",
                    max_tokens=400,
                    temperature=0.3,
                    extra_body={"reasoning_effort": "low"} if self.deps.settings.model.startswith("openai/gpt-oss") else None,
                    stream=True,
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        text_parts.append(delta.content)
                        await emit(delta.content)  # tokens go to TTS as they arrive
                    for tc in delta.tool_calls or []:
                        slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] += tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments
                break
            except BadRequestError:
                # Smaller open models occasionally emit a malformed tool call. Retry once if nothing was spoken yet.
                if attempt == 1 or text_parts:
                    raise
                calls.clear()

        tool_calls = [
            {"id": s["id"] or f"call_{i}", "name": s["name"], "arguments": s["arguments"] or "{}"}
            for i, s in sorted(calls.items())
            if s["name"]
        ]
        msg: dict = {"role": "assistant", "content": "".join(text_parts) or None}
        if tool_calls:
            msg["tool_calls"] = [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in tool_calls
            ]
        return msg, tool_calls

    async def respond(self, session: Session, user_text: str, send_token: SendToken) -> str:
        """Run one caller turn: stream speech, execute tools, loop until the model stops calling tools.

        Conversation history is committed only at consistent points, so a barge-in
        (task cancellation) can never leave a dangling tool call in the history.
        """
        log = self.deps.calllog
        log.log(session.call_sid, "user", user_text)
        session.pending_end = None
        base_len = len(session.messages)
        working = list(session.messages) + [{"role": "user", "content": user_text}]
        streamed: list[str] = []

        async def emit(tok: str):
            streamed.append(tok)
            await send_token(tok)

        try:
            for _ in range(self.deps.settings.max_tool_rounds):
                msg, tool_calls = await self._call_model(session, working, emit)
                if msg["content"] or tool_calls:
                    working.append(msg)
                if not tool_calls:
                    break
                for c in tool_calls:
                    try:
                        args = json.loads(c["arguments"])
                        if not isinstance(args, dict):
                            raise ValueError("arguments must be an object")
                    except ValueError as e:
                        out = {"ok": False, "error": f"Invalid tool arguments: {e}"}
                    else:
                        # shield: a half-finished booking must not be abandoned mid-write
                        out = await asyncio.shield(run_tool(self.deps, session, c["name"], args))
                    working.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out)})
                session.messages = list(working)  # safe commit point
            else:
                await emit(" " + FALLBACK)
            session.messages = list(working)
        except asyncio.CancelledError:
            if len(session.messages) == base_len:
                session.messages.append({"role": "user", "content": user_text})
            partial = "".join(streamed).strip()
            if partial:
                session.messages.append({"role": "assistant", "content": partial})
            log.log(session.call_sid, "assistant_partial", partial)
            raise
        reply = "".join(streamed).strip()
        log.log(session.call_sid, "assistant", reply)
        return reply
