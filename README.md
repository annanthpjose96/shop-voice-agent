# Shop Voice Agent

An AI phone receptionist for auto shops and home-service businesses. It answers calls 24/7, books, reschedules and cancels appointments, takes messages, and hands off to a human when it should.

**Stack:** FastAPI · Twilio ConversationRelay (speech-to-text, text-to-speech, barge-in) · any OpenAI-compatible LLM (free Groq by default; Gemini, OpenRouter, Ollama also work) · SQLite

```
Caller ─ Twilio ─ ConversationRelay ──ws──▶ /ws (FastAPI)
                                              │ caller text
                                              ▼
                                   Agent loop (LLM, streaming)
                                     │ tool calls          ▲ tokens → spoken live
                                     ▼
                              Scheduler (business rules in code)
                                     │
                                   SQLite (appointments, call events, messages)
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add LLM_API_KEY (free Groq key), TWILIO_AUTH_TOKEN, PUBLIC_HOST
uvicorn app.main:app --port 8000
ngrok http 8000                 # copy the host into PUBLIC_HOST (no https://)
```

In the Twilio console, set your number's **"A call comes in"** webhook to `POST https://<PUBLIC_HOST>/voice`. Call the number.

Run `pytest` for the scheduler tests (no API key needed). Run `python -m evals.run_evals` for end-to-end conversation evals against your configured model.

## Make it yours

Everything business-specific is in `business.json`: name, timezone, number of bays/technicians, hours, services and durations. For a home-services business, swap the services (e.g. `plumbing_visit`, `hvac_tuneup`) and set `bays` to the number of crews.

## Why it's built this way

- **Rules in code, not the prompt.** Hours, capacity, notice periods and booking limits are enforced in `scheduler.py`. The model proposes; code decides. Bookings use `BEGIN IMMEDIATE` transactions and are idempotent, so retries never double-book.
- **Caller-ID scoping.** The model can only list, move or cancel appointments belonging to the calling number. It can't be talked into touching someone else's.
- **Barge-in safe.** Conversation history is committed only at consistent points, so interrupting the agent mid-tool-call can't corrupt the dialogue. After an interrupt, the history keeps only what the caller actually heard.
- **Escalation paths.** Emergencies end the call with a safety message. Requests for a person, or two consecutive system errors, forward to `HUMAN_TRANSFER_NUMBER`; with no number configured it takes a message instead.
- **Everything is logged.** Transcript turns, every tool call with arguments and results, and errors go to the `events` table, keyed by `call_sid`.
- **Webhooks and the websocket are signature-checked** with your Twilio auth token.

## Before real customers

- [ ] Run evals; add a scenario for every failure you hear in real calls.
- [ ] Decide retention for the `events` table (it holds transcripts and phone numbers) and add a purge job.
- [ ] Check call-recording and AI-disclosure rules for your region. The greeting discloses AI and logging; add consent wording if you record audio.
- [ ] Move SQLite to Postgres, or sync to your real calendar/CRM, if you run more than one instance. `Scheduler` is the single seam to swap.
- [ ] Add alerting on the `error` events and a daily summary of bookings, transfers and messages.
- [ ] Test with real accents, background noise and speakerphone; tune `TTS_VOICE` and the system prompt in `app/prompts.py`.
- [ ] Outbound reminder calls are a natural phase two on the same core, but they need consent handling (e.g. TCPA in the US).

## Choosing a model

Set `LLM_API_KEY`, `LLM_BASE_URL` and `MODEL` in `.env`. The default is Groq's free tier (get a key at console.groq.com, no card needed) with `llama-3.3-70b-versatile`. Free tiers are rate-limited (roughly 30 requests/minute at the time of writing), which is fine for testing but not for heavy production traffic. Smaller models make more tool-calling mistakes, so always run the evals after switching models. `.env.example` lists settings for Gemini, OpenRouter and local Ollama.

## Notes

- On Windows, `tzdata` (in requirements.txt) is required for timezone lookups.
- After `transfer_to_human` / `end_call`, the server waits `END_DELAY_SECONDS` so the final sentence finishes speaking before Twilio ends the session.
