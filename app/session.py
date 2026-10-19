from dataclasses import dataclass, field


@dataclass
class Session:
    call_sid: str
    phone: str = ""
    messages: list = field(default_factory=list)
    pending_end: dict | None = None  # {"action": "transfer" | "hangup", "reason": str}
    errors: int = 0
    offered: set = field(default_factory=set)  # slots returned by check_availability this call

    def truncate_last_assistant(self, spoken: str) -> None:
        """After a barge-in, keep only what the caller actually heard."""
        if not spoken or not self.messages:
            return
        last = self.messages[-1]
        if last["role"] == "assistant" and not last.get("tool_calls"):
            last["content"] = f"{spoken} [caller interrupted]"
