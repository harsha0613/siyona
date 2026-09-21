"""Contract tests for the voice agent tool layer."""

from __future__ import annotations

import json

import pytest
from tools import (
    END_CALL_REASONS,
    TOOL_REGISTRY,
    TOOLS,
    ToolError,
    dispatch,
    end_call,
    send_dtmf,
    tool_schemas,
)


def test_every_tool_is_registered_under_its_name() -> None:
    assert set(TOOL_REGISTRY) == {tool.name for tool in TOOLS}
    assert all(name == tool.name for name, tool in TOOL_REGISTRY.items())


@pytest.mark.parametrize("schema", tool_schemas(), ids=lambda s: s["function"]["name"])
def test_schema_shape_matches_the_model_api(schema: dict) -> None:
    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] and function["description"]

    parameters = function["parameters"]
    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert parameters["required"], "every tool must declare required arguments"
    assert set(parameters["required"]) <= set(parameters["properties"])

    for name, prop in parameters["properties"].items():
        assert prop.get("type"), f"{name} is missing a type"
        assert prop.get("description"), f"{name} is missing a description"

    # The definition has to survive the trip to the model as JSON.
    assert json.loads(json.dumps(schema)) == schema


def test_end_call_reason_enum_matches_the_constant() -> None:
    schema = TOOL_REGISTRY["end_call"].as_openai_schema()
    assert schema["function"]["parameters"]["properties"]["reason"]["enum"] == list(
        END_CALL_REASONS
    )


@pytest.mark.parametrize("digits", ["1", "0", "*", "#", "4085551234#", "1w2"])
def test_send_dtmf_accepts_valid_sequences(digits: str) -> None:
    result = send_dtmf(digits=digits, reason="selecting the reservations menu")
    assert result["status"] == "sent"
    assert result["digits"] == digits
    assert result["duration_ms"] == len(digits) * 120


@pytest.mark.parametrize("digits", ["", "abc", "1 2", "9" * 33, "1;drop"])
def test_send_dtmf_rejects_invalid_sequences(digits: str) -> None:
    with pytest.raises(ToolError):
        send_dtmf(digits=digits, reason="why not")


def test_send_dtmf_requires_a_reason() -> None:
    with pytest.raises(ToolError):
        send_dtmf(digits="1", reason="   ")


@pytest.mark.parametrize("reason", END_CALL_REASONS)
def test_end_call_accepts_every_declared_reason(reason: str) -> None:
    result = end_call(reason=reason, outcome_summary="Table booked for four at 7pm.")
    assert result["status"] == "ended"
    assert result["reason"] == reason


def test_end_call_rejects_unknown_reason() -> None:
    with pytest.raises(ToolError):
        end_call(reason="hung_up_angrily", outcome_summary="nope")


def test_end_call_requires_a_summary() -> None:
    with pytest.raises(ToolError):
        end_call(reason="task_complete", outcome_summary="")


def test_dispatch_routes_by_name() -> None:
    assert dispatch("send_dtmf", {"digits": "1", "reason": "reservations"})["digits"] == "1"


def test_dispatch_rejects_unknown_tool() -> None:
    with pytest.raises(ToolError):
        dispatch("launch_missiles", {})
