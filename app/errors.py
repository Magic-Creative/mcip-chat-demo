"""The errorCode → UI state table (Guide §6).

Every error the demo can show — HTTP errors from MCip (§6.1), failures inside
a turn (§6.2) and the demo's own transport errors — is mapped here to

* ``ui``: what the browser should do (the frontend switches on this, not on
  ``message``, which is only ever shown to the user), and
* ``advice``: the one-line fix shown next to the error.

UI states:

``auth``       the demo session is gone — show the sign-in screen.
``reconnect``  the MCip connection ended — banner + Reconnect.
``approval``   an approval waits in MCip — card with Open in MCip.
``busy``       retryable rate limit / busy — retried with a countdown.
``rephrase``   the prompt guard refused the message — keep the text.
``retryable``  a transient failure — inline error with Retry.
``workspace``  the chosen workspace is not usable — reopen the picker.
``fatal``      anything else — inline error with advice, no Retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Codes that end the MCip connection: clear the stored key, show Reconnect.
CONNECTION_LOST = frozenset(
    # 401 family (Guide §6.1, §11): the key can never work again.
    {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_USER_INACTIVE"}
)

#: Codes the caller may retry (Guide §8) with the same client_request_id.
RETRYABLE = frozenset(
    {
        "RATE_LIMITED",
        "CLIENT_RATE_LIMITED",
        "CONVERSATION_BUSY",
        "INTERNAL_ERROR",
        "MODEL_RATE_LIMITED",
        "MODEL_PROVIDER_UNAVAILABLE",
        # Demo-level: a stream cut before done, or the transport failed.
        "STREAM_TRUNCATED",
        "NETWORK_ERROR",
    }
)

UI_STATE: dict[str, str] = {
    # §6.1 HTTP errors
    "API_KEY_MISSING": "fatal",
    "API_KEY_INVALID": "reconnect",
    "API_KEY_EXPIRED": "reconnect",
    "API_USER_INACTIVE": "reconnect",
    "API_KEY_SCOPE": "fatal",
    "API_CLIENT_DISABLED": "fatal",
    "API_CLIENT_NOT_ALLOWED": "fatal",
    "API_CLIENT_IP_DENIED": "fatal",
    "API_ACCESS_DISABLED": "workspace",
    "WORKSPACE_FORBIDDEN": "workspace",
    "FORBIDDEN": "fatal",
    "CONVERSATION_NOT_FOUND": "fatal",
    "CONVERSATION_BUSY": "busy",
    "CONVERSATION_AWAITING_APPROVAL": "approval",
    "REQUEST_TOO_LARGE": "fatal",
    "VALIDATION_ERROR": "fatal",
    "RATE_LIMITED": "busy",
    "CLIENT_RATE_LIMITED": "busy",
    "PROMPT_REFUSED": "rephrase",
    "INTERNAL_ERROR": "retryable",
    # §6.2 errors inside a turn
    "PREMIUM_QUOTA_EXHAUSTED": "fatal",
    "MODEL_RATE_LIMITED": "retryable",
    "MODEL_PROVIDER_UNAVAILABLE": "retryable",
    "MODEL_AUTH_FAILED": "fatal",
    "MODEL_NOT_FOUND": "fatal",
    "MODEL_CONTEXT_LIMIT": "fatal",
    # Demo-level
    "STREAM_TRUNCATED": "retryable",
    "NETWORK_ERROR": "retryable",
    "DEMO_RATE_LIMITED": "busy",
    "NOT_CONNECTED": "reconnect",
    "DEMO_UNAUTHENTICATED": "auth",
    "DEMO_BAD_CREDENTIALS": "auth",
    "DEMO_USERNAME_TAKEN": "auth",
    "DEMO_REGISTRATION_DISABLED": "auth",
    "DEMO_CSRF": "auth",
    "DEMO_KEY_FORMAT": "reconnect",
    "DEMO_NO_WORKSPACE": "workspace",
}

ADVICE: dict[str, str] = {
    "API_KEY_MISSING": "Fix the integration: send 'Authorization: Bearer ss_pat_...'.",
    "API_KEY_INVALID": "The key is unknown or was revoked. Connect a new key.",
    "API_KEY_EXPIRED": "The key expired. Create a new one in MCip and connect it.",
    "API_USER_INACTIVE": "Your MCip account is deactivated. Contact the MCip admin.",
    "API_KEY_SCOPE": "Use an 'External system' (chat) key, not a full-access key.",
    "API_CLIENT_DISABLED": "The API client is disabled. Contact the MCip admin.",
    "API_CLIENT_NOT_ALLOWED": "You left the client's organization. Contact the MCip admin.",
    "API_CLIENT_IP_DENIED": "This server's IP is not on the client's allowlist.",
    # Reserved: current MCip servers never emit this; older releases sent it
    # when a workspace's API-access toggle (since removed) was off.
    "API_ACCESS_DISABLED": (
        "This MCip server is an older release: a workspace admin must turn on "
        "'API key access' for this workspace."
    ),
    "WORKSPACE_FORBIDDEN": "Pick a workspace from the list your key can use.",
    "FORBIDDEN": "Your workspace role does not allow this. Ask a workspace admin.",
    "CONVERSATION_NOT_FOUND": "Start a new chat (this conversation no longer exists).",
    "CONVERSATION_BUSY": "A turn is still running on this conversation.",
    "CONVERSATION_AWAITING_APPROVAL": "Approve or reject the pending action in MCip.",
    "REQUEST_TOO_LARGE": "Keep the message under 32,000 characters.",
    "VALIDATION_ERROR": "Fix the request — see the message for the field.",
    "RATE_LIMITED": "The key's rate limit was hit.",
    "CLIENT_RATE_LIMITED": "The API client's rate limit was hit (all its users).",
    "PROMPT_REFUSED": "This message can't be sent. Please rephrase.",
    "INTERNAL_ERROR": "MCip had an unexpected error. Retrying is safe.",
    "PREMIUM_QUOTA_EXHAUSTED": "The workspace is out of credits. Ask the MCip admin.",
    "MODEL_RATE_LIMITED": "The model provider is throttling. Retry in a moment.",
    "MODEL_PROVIDER_UNAVAILABLE": "The model provider is down. Retry later.",
    "MODEL_AUTH_FAILED": "MCip's model credentials are broken. Report to the MCip admin.",
    "MODEL_NOT_FOUND": "The workspace's model is misconfigured. Report to the MCip admin.",
    "MODEL_CONTEXT_LIMIT": "The conversation is too long for the model. Start a new chat.",
    "STREAM_TRUNCATED": "The stream ended before the answer finished.",
    "NETWORK_ERROR": "Could not reach MCip. Check the connection and retry.",
    "DEMO_RATE_LIMITED": "Too many requests in this demo. Wait a moment.",
    "NOT_CONNECTED": "Connect your MCip key first.",
    "DEMO_UNAUTHENTICATED": "Your demo session expired. Sign in again.",
    "DEMO_BAD_CREDENTIALS": "Check the username and password.",
    "DEMO_USERNAME_TAKEN": "Pick a different username.",
    "DEMO_REGISTRATION_DISABLED": ("Registration is closed. Ask the demo admin for an account."),
    "DEMO_CSRF": "Reload the page and try again.",
    "DEMO_KEY_FORMAT": "An MCip chat key starts with 'ss_pat_'.",
    "DEMO_NO_WORKSPACE": "Choose a workspace before sending a message.",
}


def ui_state(error_code: str) -> str:
    """The UI action for a code; unknown codes are fatal (Guide §5: ignore
    unknown *fields*, but never pretend an unknown error is retryable)."""
    return UI_STATE.get(error_code, "fatal")


def advice(error_code: str) -> str:
    return ADVICE.get(error_code, "")


def is_retryable(error_code: str) -> bool:
    return error_code in RETRYABLE


@dataclass
class DemoError(Exception):
    """A normalized error handed to the browser (never carries the key)."""

    error_code: str
    message: str
    http_status: int = 400
    ui: str = ""
    retry_after_ms: int | None = None
    continue_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ui:
            self.ui = ui_state(self.error_code)
        super().__init__(f"{self.error_code}: {self.message}")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "errorCode": self.error_code,
            "message": self.message,
            "ui": self.ui,
            "advice": advice(self.error_code),
        }
        if self.retry_after_ms is not None:
            payload["retry_after_ms"] = self.retry_after_ms
        if self.continue_url is not None:
            payload["continue_url"] = self.continue_url
        payload.update(self.extra)
        return payload
