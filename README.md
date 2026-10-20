# Shop Voice Agent

An AI phone receptionist for auto shops and home-service businesses. It answers calls, checks real availability, and books, reschedules or cancels appointments. It takes messages and hands off to a human when it should.

**Status: working prototype.** The booking logic and the AI conversation are verified by 14 unit tests, 5 live-model conversation evals, a terminal chat tester and a browser voice test. **The Twilio phone-call path is implemented but has not been tested on a live call yet** (no Twilio trial number was available). See [Limitations](#limitations).

<!-- Add a 20-30 second screen recording or GIF of the browser voice test here -->

## What it does

- Greets the caller, discloses that it is an AI assistant and that the call may be logged
- Understands natural speech ("sometime next Tuesday morning") and converts it to exact dates
- Checks availability against opening hours and workshop capacity, and offers up to three times
- Reads the details back and books only after the caller confirms; also reschedules and cancels
- Matches callers to their own appointments by caller ID, so it cannot touch anyone else's booking
- Detects emergencies (for example a gas smell), tells the caller to contact emergency services, and ends the call
- Transfers to a human on request or after repeated errors, or takes a message if no one is available
- Logs every turn, tool call and error

## Architecture

**Stack:** Python, FastAPI, Twilio ConversationRelay (speech-to-text, text-to-speech, barge-in), any OpenAI-compatible LLM (Groq free tier by default), SQLite

```
Caller ─ Twilio ─ ConversationRelay ──ws──▶ /ws (FastAPI)
                                              │ caller text
                                              ▼
                                   Agent loop (LLM, streaming)
                                     │ tool calls          ▲ tokens -> spoken live
                                     ▼
                              Scheduler (business rules in code)
                                     │
                                   SQLite (appointments, call events, messages)
```

| File | Role |
|---|---|
| `app/main.py` | FastAPI app: `/voice` (TwiML), `/handoff`, `/ws` (live call), `/health`, and the dev-only `/test` voice page |
| `app/agent.py` | Streaming LLM loop with tool calling and barge-in-safe history |
| `app/tools.py` | The 8 tools and their guards |
| `app/scheduler.py` | All business rules and database access |
| `app/prompts.py` | System prompt |
| `business.json` | Business name, timezone, bays, hours, services |
| `chat.py`, `evals/`, `tests/` | Terminal chat tester, conversation evals, unit tests |

## Quick start

You need Python 3.10+ and a free Groq key from [console.groq.com](https://console.groq.com) (no card needed).

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

**Mac / Linux**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and set `LLM_API_KEY`. Then pick how to run it:

| Goal | Command | Needs |
|---|---|---|
| Unit tests | `pytest` | nothing |
| Conversation evals | `python -m evals.run_evals` | LLM key |
| Chat in the terminal (shows every tool call) | `python chat.py` | LLM key |
| Browser voice test | see below | LLM key, Chrome or Edge |
| Real phone calls | see below | Twilio number, ngrok |

### Browser voice test (no phone number needed)

1. Set `ENABLE_TEST_UI=true` in `.env`.
2. Start the server: `uvicorn app.main:app --reload --port 8000`
3. Open http://localhost:8000/test in Chrome or Edge, press **Start call**, allow the microphone and talk.

Speech is transcribed by your browser, the real agent and tools run on the server, and replies are spoken aloud. Pick a more natural voice in the **Voice** dropdown (Edge usually has "Natural" voices). This tests the conversation, tools and database by voice; it does **not** test Twilio's phone audio or latency. Keep `ENABLE_TEST_UI` off in production.

### Real phone calls

1. Run `ngrok http 8000` and put the host name (no `https://`) in `PUBLIC_HOST` in `.env`.
2. Set `TWILIO_AUTH_TOKEN` in `.env` and restart the server.
3. In the Twilio console, set your number's "A call comes in" webhook to `POST https://<PUBLIC_HOST>/voice`.

## Configuration

| Setting | Meaning |
|---|---|
| `LLM_API_KEY`, `LLM_BASE_URL`, `MODEL` | Model provider and model. Default: Groq, `openai/gpt-oss-120b` |
| `TWILIO_AUTH_TOKEN`, `PUBLIC_HOST` | Twilio signature checking and public address |
| `VALIDATE_TWILIO_SIGNATURE` | Keep `true` in real use; `false` only for debugging |
| `HUMAN_TRANSFER_NUMBER` | Where "talk to a person" calls go. Empty means take a message |
| `TTS_PROVIDER`, `TTS_VOICE` | Text-to-speech provider and voice for phone calls |
| `ENABLE_TEST_UI` | Enables the browser voice test at `/test` (development only) |
| `DATABASE_PATH`, `BUSINESS_CONFIG` | SQLite file and business definition file |

**Other providers:** change the three LLM settings. `.env.example` lists Gemini, OpenRouter and Ollama. Free model names change often; list what your key can use with the provider's models endpoint. Smaller models make more tool-calling mistakes, so run the evals after switching.

**Another business:** edit `business.json` (name, timezone, number of bays or crews, hours, services and durations). For home services, replace the services with visit types.

## Design decisions

- **Rules in code, not in the prompt.** Hours, capacity, notice, booking limits and the per-number cap are enforced in `scheduler.py`. The model proposes; code decides. Bookings use transactions and are idempotent.
- **Checked-slot guard.** The model can only book or move to a time returned by an availability check in the same call, and tools return the weekday in words. This fixed a real bug where the agent offered Tuesday, booked Thursday and told the caller Tuesday.
- **Caller-ID scoping.** The model can only list, move or cancel appointments that belong to the calling number.
- **Barge-in safe.** History is committed only at consistent points, so interrupting the agent mid-tool-call cannot corrupt the conversation, and after an interrupt only what the caller heard is kept.
- **Escalation paths.** Emergencies end the call; a request for a person, or two consecutive system errors, transfers (or takes a message if no transfer number is set).
- **Provider-agnostic LLM layer.** Only three settings change the provider.
- **Everything is logged** in the `events` table, keyed by call id.

## Testing

- `pytest`: 12 scheduler tests (hours, capacity, notice, limits, idempotency, ownership) and 2 tool-guard tests
- `python -m evals.run_evals`: 5 end-to-end scenarios against the real model (book, cancel, closed day, emergency, transfer), each checked against the database

When you hear or read a bad call, add it as an eval scenario, fix it, and re-run everything.

## Limitations

- The Twilio live-call path has not been tested on a real phone call, so latency, voice quality and exact Twilio message details are unverified
- The free-tier model is slower and less reliable at tool use than a paid one, and free tiers are rate limited
- One location, one timezone, English only; no calendar or CRM sync; no dashboard or alerting
- No knowledge base, so it cannot answer questions about prices or policies (it offers a message or a person instead)
- SQLite and a single server instance; fine for one shop, not for scale

## Before real customers

- [ ] Test real phone calls and add scenarios from what you hear
- [ ] Decide retention for the `events` table (it holds transcripts and phone numbers) and add a purge job
- [ ] Check call-recording, AI-disclosure and outbound-calling rules for your region (the greeting already discloses AI and logging)
- [ ] Move SQLite to Postgres or sync with a real calendar if you run more than one instance
- [ ] Add alerting on `error` events and a daily summary of bookings, transfers and messages

## Notes

- On Windows, `tzdata` (already in `requirements.txt`) is needed for timezone lookups.
- After `transfer_to_human` or `end_call`, the server waits `END_DELAY_SECONDS` (4 s) so the last sentence finishes before Twilio ends the session.
- `.env` is git-ignored. Never commit real keys.
