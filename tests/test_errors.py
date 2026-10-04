"""The errorCode → ui/advice table must cover every documented code."""

from __future__ import annotations

from app.errors import (
    ADVICE,
    CONNECTION_LOST,
    RETRYABLE,
    UI_STATE,
    DemoError,
    advice,
    is_retryable,
    ui_state,
)

HTTP_CODES = [
    "API_KEY_MISSING",
    "API_KEY_INVALID",
    "API_KEY_EXPIRED",
    "API_USER_INACTIVE",
    "API_KEY_SCOPE",
    "API_CLIENT_DISABLED",
    "API_CLIENT_NOT_ALLOWED",
    "API_CLIENT_IP_DENIED",
    "API_ACCESS_DISABLED",
    "WORKSPACE_FORBIDDEN",
    "FORBIDDEN",
    "CONVERSATION_NOT_FOUND",
    "CONVERSATION_BUSY",
    "CONVERSATION_AWAITING_APPROVAL",
    "REQUEST_TOO_LARGE",
    "VALIDATION_ERROR",
    "RATE_LIMITED",
    "CLIENT_RATE_LIMITED",
    "PROMPT_REFUSED",
    "INTERNAL_ERROR",
]

TURN_CODES = [
    "PREMIUM_QUOTA_EXHAUSTED",
    "MODEL_RATE_LIMITED",
    "MODEL_PROVIDER_UNAVAILABLE",
    "MODEL_AUTH_FAILED",
    "MODEL_NOT_FOUND",
    "MODEL_CONTEXT_LIMIT",
]

DEMO_CODES = [
    "STREAM_TRUNCATED",
    "NETWORK_ERROR",
    "DEMO_RATE_LIMITED",
    "NOT_CONNECTED",
    "DEMO_UNAUTHENTICATED",
    "DEMO_BAD_CREDENTIALS",
    "DEMO_USERNAME_TAKEN",
    "DEMO_REGISTRATION_DISABLED",
    "DEMO_CSRF",
    "DEMO_KEY_FORMAT",
    "DEMO_NO_WORKSPACE",
]

UI_STATES = {"auth", "reconnect", "approval", "busy", "rephrase", "retryable", "workspace", "fatal"}


def test_every_documented_code_has_ui_and_advice():
    for code in HTTP_CODES + TURN_CODES + DEMO_CODES:
        assert code in UI_STATE, f"{code} has no ui state"
        assert ADVICE.get(code), f"{code} has no advice"
        assert ui_state(code) in UI_STATES


def test_unknown_code_is_fatal_and_not_retryable():
    assert ui_state("SOMETHING_NEW") == "fatal"
    assert advice("SOMETHING_NEW") == ""
    assert not is_retryable("SOMETHING_NEW")


def test_connection_lost_codes_are_reconnect():
    assert CONNECTION_LOST == {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_USER_INACTIVE"}
    for code in CONNECTION_LOST:
        assert ui_state(code) == "reconnect"


def test_retryable_codes_never_map_to_fatal():
    for code in RETRYABLE:
        assert ui_state(code) in {"busy", "retryable"}, code


def test_quota_exhausted_is_fatal_but_model_throttles_are_retryable():
    assert ui_state("PREMIUM_QUOTA_EXHAUSTED") == "fatal"
    assert ui_state("MODEL_RATE_LIMITED") == "retryable"
    assert ui_state("MODEL_PROVIDER_UNAVAILABLE") == "retryable"


def test_demo_error_payload():
    error = DemoError(error_code="DEMO_RATE_LIMITED", message="Slow down.", retry_after_ms=1500)
    payload = error.to_payload()
    assert payload["errorCode"] == "DEMO_RATE_LIMITED"
    assert payload["ui"] == "busy"
    assert payload["retry_after_ms"] == 1500
    assert payload["advice"]
    assert "continue_url" not in payload


def test_demo_error_payload_keeps_continue_url():
    error = DemoError(
        error_code="CONVERSATION_AWAITING_APPROVAL",
        message="Approve it.",
        http_status=409,
        continue_url="https://mcip.example.com/approvals/1",
    )
    payload = error.to_payload()
    assert payload["ui"] == "approval"
    assert payload["continue_url"] == "https://mcip.example.com/approvals/1"
