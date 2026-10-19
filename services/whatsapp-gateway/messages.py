"""Every message the assistant sends on WhatsApp, in one place for review and translation."""

from __future__ import annotations

CLARIFY = (
    "Sorry, I couldn't read that message. I can only work with text right now. "
    "Please tell me who to call, their number, and what you need, e.g.\n"
    "“Book a table for 4 at Bella Vista tonight at 7pm, their number is +1 415 555 0123.”"
)


def ask_number(callee: str | None) -> str:
    target = f" for {callee}" if callee else ""
    return (
        f"What number should I call{target}? Please send it with the country code if it's not "
        "local, e.g. +1 415 555 0123."
    )


def confirm(
    goal: str, callee: str | None, display: str, when: str, details: str | None = None
) -> str:
    """The who / what / when summary the user must approve before anything is dialled."""
    who = f"{callee} on {display}" if callee else display
    lines = ["Before I call, please check:", f"• Who: {who}", f"• What: {goal}"]
    if details:
        lines.append(f"• I'll tell them: {details}")
    lines += [f"• When: {when}", "", "Reply YES to call or NO to cancel."]
    return "\n".join(lines)


CALLING_NOW = "📞 Calling now. I'll message you when I'm done."
DECLINED = "Okay, I won't call. Send me something else whenever you like."
CALL_NOT_STARTED = "Sorry, I couldn't start that call. Nothing was done; please try again later."
ASSISTANT_UNAVAILABLE = "Sorry, I couldn't work that out just now. Please try again in a moment."


def confirmation_expired(minutes: int) -> str:
    return (
        f"I didn't hear back within {minutes} minutes, so I've cancelled that request and "
        "won't call. Send it again whenever you're ready."
    )


# --- call status updates --------------------------------------------------------


def _who(callee: str | None) -> str:
    return callee or "them"


def call_succeeded(summary: str | None, callee: str | None) -> str:
    detail = (summary or "").strip() or f"The call with {_who(callee)} went through."
    return f"✅ Done. {detail}"


def call_no_answer(callee: str | None) -> str:
    return f"📵 No one answered when I called {_who(callee)}. Nothing was done."


def call_busy(callee: str | None) -> str:
    return f"📵 The line was busy when I called {_who(callee)}. Nothing was done."


def call_hung_up(callee: str | None) -> str:
    who = callee or "They"
    return f"📴 {who} hung up before anything was confirmed. Nothing was done."


CALL_FAILED = (
    "⚠️ Something went wrong on my side and the call didn't go through. Nothing was done. "
    "Please try again later."
)


def retrying(
    outcome: str, callee: str | None, attempt: int, max_attempts: int, minutes: int
) -> str:
    why = {"busy": "The line was busy", "no_answer": "No one answered"}.get(
        outcome, "The call didn't go through"
    )
    return (
        f"🔁 {why} when I called {_who(callee)}. I'll try again in about {minutes} min "
        f"(attempt {attempt + 1} of {max_attempts})."
    )


_SPEAKERS = {"agent": "Siyona", "callee": "Them"}


def call_transcript(lines: list[tuple[str, str]], callee: str | None) -> str:
    who = callee or "the call"
    body = "\n".join(f"{_SPEAKERS.get(speaker, speaker)}: {text}" for speaker, text in lines)
    return f"📝 Transcript of {who}:\n{body}"


def still_on_the_line(callee: str | None) -> str:
    return (
        f"⏳ Still on the line with {_who(callee)}. It's taking longer than usual; "
        "I'll message you as soon as it's done."
    )
