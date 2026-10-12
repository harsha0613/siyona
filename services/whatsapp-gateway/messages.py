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


def confirm(goal: str, callee: str | None, display: str, when: str) -> str:
    """The who / what / when summary the user must approve before anything is dialled."""
    who = f"{callee} on {display}" if callee else display
    return "\n".join(
        [
            "Before I call, please check:",
            f"• Who: {who}",
            f"• What: {goal}",
            f"• When: {when}",
            "",
            "Reply YES to call or NO to cancel.",
        ]
    )


CALLING_NOW = "📞 Calling now. I'll message you when I'm done."
DECLINED = "Okay, I won't call. Send me something else whenever you like."
CALL_NOT_STARTED = "Sorry, I couldn't start that call. Nothing was done; please try again later."


def confirmation_expired(minutes: int) -> str:
    return (
        f"I didn't hear back within {minutes} minutes, so I've cancelled that request and "
        "won't call. Send it again whenever you're ready."
    )
