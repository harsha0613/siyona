"""Phone number helpers.

Numbers are extracted deterministically with libphonenumber. The assistant only
ever dials a number that appears in the user's own words.
"""

from __future__ import annotations

import re

import phonenumbers
from phonenumbers import PhoneNumberFormat


class PhoneNumberError(ValueError):
    pass


def whatsapp_to_e164(wa_id: str) -> str:
    """WhatsApp sends ``14155550100``; store everything as ``+14155550100``."""
    digits = re.sub(r"\D", "", wa_id)
    if not digits:
        raise PhoneNumberError("empty WhatsApp id")
    return f"+{digits}"


def region_of(e164: str) -> str | None:
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(e164))
    except phonenumbers.NumberParseException:
        return None


def display_number(e164: str) -> str:
    """``+14155550123`` -> ``+1 415-555-0123``."""
    try:
        return phonenumbers.format_number(phonenumbers.parse(e164), PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return e164


def find_numbers(text: str, default_region: str | None) -> list[str]:
    """Every phone number written in ``text``, as E.164, in order of appearance."""
    found: list[str] = []
    for match in phonenumbers.PhoneNumberMatcher(text, default_region or "US"):
        e164 = phonenumbers.format_number(match.number, PhoneNumberFormat.E164)
        if e164 not in found:
            found.append(e164)
    return found
