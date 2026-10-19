"""Make the hyphenated service directories importable from the tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

for relative in (
    "services/whatsapp-gateway",
    "services/voice-agent",
    "services/orchestrator",
    "services/api",
    "eval",
):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)


# Keys a developer may have exported must never send a test to a live vendor.
LIVE_VENDOR_KEYS = ("OPENAI_API_KEY", "GOOGLE_MAPS_API_KEY")


@pytest.fixture(autouse=True)
def _no_live_vendors(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIVE_VENDOR_KEYS:
        monkeypatch.delenv(name, raising=False)
