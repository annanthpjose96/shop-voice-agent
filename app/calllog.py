import json
from contextlib import closing
from datetime import datetime, timezone

from .db import connect


class CallLog:
    """Append-only event log: transcript turns, tool calls and their results, errors."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def log(self, call_sid: str, kind: str, payload) -> None:
        with closing(connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO events (call_sid, ts, kind, payload) VALUES (?,?,?,?)",
                (call_sid, datetime.now(timezone.utc).isoformat(), kind, json.dumps(payload, default=str)),
            )

    def save_message(self, call_sid, phone, caller_name, message, callback) -> None:
        with closing(connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO messages (call_sid, phone, caller_name, message, callback, created_at) VALUES (?,?,?,?,?,?)",
                (call_sid, phone, caller_name, message, callback, datetime.now(timezone.utc).isoformat()),
            )
