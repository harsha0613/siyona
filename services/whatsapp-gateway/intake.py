"""Turn a WhatsApp instruction into a structured task: who, what, when.

Rule-based and deterministic, so it runs with no model and no keys. One rule is
non-negotiable: **every number we dial must appear in the user's own words.**
Numbers are extracted with libphonenumber; nothing is inferred or looked up.
The only other source is the WhatsApp assistant's ``create_call_request``: the
number Google Places lists for a store it found, never one the model typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from phone import find_numbers

ASAP = "as soon as possible"


@dataclass
class TaskSpec:
    goal: str
    callee_name: str | None
    numbers: list[str]
    when: str = ASAP
    details: dict[str, str] = field(default_factory=dict)

    @property
    def missing_number(self) -> bool:
        return not self.numbers


_CALLEE_RE = re.compile(
    r"\b(?i:call|ring|phone|contact)\s+(?:(?i:the)\s+)?([A-Z][\w'&]*(?:\s+[A-Z][\w'&]*){0,3})"
    r"|\bat\s+([A-Z][\w'&]*(?:\s+[A-Z][\w'&]*){0,3})"
)
_PARTY_RE = re.compile(r"\bfor\s+(\d{1,2}|two|three|four|five|six|seven|eight)\b", re.I)
_TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|midnight)\b", re.I)
_DAY_RE = re.compile(
    r"\b(today|tonight|tomorrow|this (?:morning|afternoon|evening|week)|next week|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_NUMBERISH = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_TRAILING_NUMBER_PHRASE = re.compile(
    r"[\s,;:(-]*(?:(?:their|the|his|her|its|whose)\s+)?(?:phone\s+)?"
    r"(?:number|no\.?|tel|phone)(?:\s+is)?[\s:]*(?:\)|\s)*$|\s+(?:on|at)\s*$",
    re.IGNORECASE,
)
# Words left stranded at the end once numbers are cut out: "..., try or", "call them on".
_DANGLING_WORDS = re.compile(
    r"(?:[\s,;:]+(?:try|or|and|on|at|call|them|ring|either|dial|then))+[\s,;:.]*$",
    re.IGNORECASE,
)


def _when(text: str) -> tuple[str, dict[str, str]]:
    details: dict[str, str] = {}
    day = _DAY_RE.search(text)
    time_match = _TIME_RE.search(text)
    if day:
        details["date"] = day.group(1).lower()
    if time_match:
        details["time"] = time_match.group(1).lower().replace(" ", "")
    if day and time_match:
        return f"{details['date']} at {details['time']}", details
    if time_match:
        return f"at {details['time']}", details
    if day:
        return details["date"], details
    return ASAP, details


def parse_instruction(instruction: str, user_region: str | None) -> TaskSpec:
    numbers = find_numbers(instruction, user_region)
    text = _NUMBERISH.sub(" ", instruction)
    callee = None
    if match := _CALLEE_RE.search(text):
        callee = (match.group(1) or match.group(2) or "").strip() or None
    when, details = _when(text)
    if party := _PARTY_RE.search(text):
        details["party_size"] = party.group(1)
    goal = " ".join(text.split())
    goal = _TRAILING_NUMBER_PHRASE.sub("", goal)
    goal = _DANGLING_WORDS.sub("", goal).strip(" ,;:.")
    return TaskSpec(
        goal=goal[:500] or instruction.strip()[:500],
        callee_name=callee,
        numbers=numbers,
        when=when,
        details=details,
    )
