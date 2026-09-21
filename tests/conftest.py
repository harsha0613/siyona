"""Make the hyphenated service directories importable from the tests."""

from __future__ import annotations

import sys
from pathlib import Path

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
