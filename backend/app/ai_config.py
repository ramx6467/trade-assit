"""Server-side AI configuration, provider selection, and verification state.

Supports Google Gemini (via official `google-genai` SDK) and OpenAI.
"""
from __future__ import annotations

import os
import re
import time
import hashlib
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from app.config import BACKEND_ENV_FILE, ROOT_ENV_FILE, load_dotenv

_verification_lock = Lock()
# A VERIFIED status reflects a real provider request that succeeded. It is not a
# permanent health guarantee: quota can be exhausted by the very next call. Keep
# VERIFIED only for a bounded window so a subsequent failure is surfaced promptly
# instead of a stale green indicator persisting indefinitely.
#
# The window is configurable (AI_VERIFICATION_LIFETIME_SECONDS). It defaults to
# five minutes so a genuine successful verification (or a genuine chat
# generation) reads as VERIFIED for a useful period instead of flickering to
# EXPIRED seconds later. It is deliberately not "forever": a cached success can
# never mask a later quota/rate-limit failure.
_DEFAULT_VERIFICATION_LIFETIME_SECONDS = 300.0
# A successful generation stays "recent" for longer than the strict
# verification TTL. This lets the UI distinguish "never verified / expired long
# ago" from "a real request succeeded recently" without implying permanent
# provider health.
_GENERATION_RECENT_SECONDS = 3600
_verification: dict[str, Any] = {
    "status": "CONNECTING",
    "verified_at": None,
    "verified_expires_at": None,
    "last_success_at": None,
    "message": "No successful API verification has been recorded.",
    "config_fingerprint": None,
    "error_code": None,
    "retry_after_seconds": None,
    "provider": "gemini",
    # True only while a real verification request is actually in flight, so the
    # UI can show CHECKING instead of mistakenly reporting EXPIRED/UNVERIFIED.
    "in_flight": False,
}


_last_env_stats: dict[str, tuple[float, int]] = {}


def reload_env_if_modified() -> None:
    for env_file in (BACKEND_ENV_FILE, ROOT_ENV_FILE):
        if env_file.exists():
            try:
                stat = os.stat(env_file)
                file_key = str(env_file.resolve())
                current_stat = (stat.st_mtime, stat.st_size)
                if current_stat != _last_env_stats.get(file_key):
                    from dotenv import dotenv_values
                    vals = dotenv_values(env_file)
                    for k, v in vals.items():
                        val = (v or "").strip()
                        # Match load_dotenv(override=False) without breaking test
                        # isolation: only fill a key that is present-but-empty in the
                        # process environment (e.g. GEMINI_API_KEY= in backend/.env).
                        # Never override a non-empty value (deployment env, monkeypatch)
                        # and never re-add a key that was explicitly removed.
                        if val and k in os.environ and not os.environ[k].strip():
                            os.environ[k] = val
                    _last_env_stats[file_key] = current_stat
            except Exception:
                pass


def _clean_env_val(val: str | None) -> str:
    if not val:
        return ""
    v = val.strip()
    if len(v) >= 2 and ((v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'"))):
        v = v[1:-1].strip()
    return v


def get_active_provider() -> str:
    """Determine the active AI provider (gemini | openai). Defaults strictly to gemini."""
    reload_env_if_modified()
    explicit = os.getenv("AI_PROVIDER", "").strip().lower()
    if explicit in {"gemini", "google", "google_genai"}:
        return "gemini"
    if explicit in {"openai"}:
        return "openai"

    # Default to Gemini as the project primary provider; never silently fall back to OpenAI
    return "gemini"


def gemini_config() -> tuple[str | None, str | None]:
    """Return configured Gemini API key and model."""
    reload_env_if_modified()
    key = _clean_env_val(os.getenv("GEMINI_API_KEY")) or _clean_env_val(os.getenv("GOOGLE_API_KEY")) or None
    model = _clean_env_val(os.getenv("GEMINI_MODEL")) or "gemini-flash-latest"
    return key, model


def gemini_models() -> list[str]:
    """Return the ordered Gemini model candidates: primary first, then fallbacks.

    Resolution order (config-driven; model names are never invented in code):
    - GEMINI_MODEL             primary model (kept as-is for backward compatibility)
    - GEMINI_FALLBACK_MODEL    a single optional fallback
    - GEMINI_FALLBACK_MODELS   optional comma-separated list of fallbacks

    Duplicate names are removed while preserving order. A fallback is never used
    to fabricate a response; it is only tried when the primary cannot answer.
    """
    reload_env_if_modified()
    primary_model = _clean_env_val(os.getenv("GEMINI_MODEL")) or "gemini-flash-latest"
    raw = ",".join(filter(None, [
        _clean_env_val(os.getenv("GEMINI_FALLBACK_MODEL")),
        _clean_env_val(os.getenv("GEMINI_FALLBACK_MODELS")),
    ]))
    models = [primary_model]
    for item in raw.split(","):
        name = item.strip()
        if name and name not in models:
            models.append(name)
    return models


def _env_number(name: str, default: float, low: float, high: float) -> float:
    """Read a finite numeric setting from the environment, clamped to a range."""
    raw = _clean_env_val(os.getenv(name))
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / infinity guard
        return default
    return max(low, min(value, high))


def verification_lifetime_seconds() -> float:
    """How long a real successful verification keeps reading as VERIFIED.

    Configurable via ``AI_VERIFICATION_LIFETIME_SECONDS`` (seconds, clamped to
    30s..24h, default five minutes). After this window the assistant reports
    EXPIRED until a new real request succeeds - it is never silently extended.
    """
    return _env_number(
        "AI_VERIFICATION_LIFETIME_SECONDS",
        _DEFAULT_VERIFICATION_LIFETIME_SECONDS,
        30.0,
        86400.0,
    )


def gemini_request_timeout_ms() -> int:
    """Per-HTTP-attempt timeout in milliseconds for Gemini calls.

    Bounds an individual connect/read so a stalled socket cannot block a
    request indefinitely. Configurable via ``GEMINI_REQUEST_TIMEOUT_MS``.
    """
    return int(_env_number("GEMINI_REQUEST_TIMEOUT_MS", 20000.0, 1000.0, 600000.0))


def gemini_total_deadline_seconds() -> float:
    """Wall-clock budget for one complete Gemini generation (all attempts and
    model fallbacks). Configurable via ``GEMINI_TOTAL_DEADLINE_SECONDS``."""
    return _env_number("GEMINI_TOTAL_DEADLINE_SECONDS", 30.0, 5.0, 600.0)


def openai_config() -> tuple[str | None, str | None]:
    """Return configured OpenAI API key and model."""
    reload_env_if_modified()
    key = os.getenv("OPENAI_API_KEY", "").strip() or None
    model = os.getenv("OPENAI_MODEL", "").strip() or None
    return key, model


def active_ai_config() -> tuple[str, str | None, str | None]:
    """Return (provider_name, api_key, model) for the currently active provider."""
    provider = get_active_provider()
    if provider == "gemini":
        key, model = gemini_config()
    else:
        key, model = openai_config()
    return provider, key, model


def _calculate_fingerprint(provider: str, key: str | None, model: str | None) -> str:
    raw = f"{provider}\0{key or ''}\0{model or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


# Human-readable headline states for the UI. These are intentionally distinct
# from `verification_status`: `state` merges configuration, connectivity,
# generation, and quota signals into one honest summary.
_STATE_LABELS = {
    "NOT_CONFIGURED": "Not configured",
    "UNVERIFIED": "Not verified",
    "AUTHENTICATED": "Verified",
    "DEGRADED": "Connected - verification expired",
    "QUOTA_EXCEEDED": "Quota exhausted",
    "RATE_LIMITED": "Rate limited",
    "INVALID_KEY": "Invalid API key",
    "MODEL_UNAVAILABLE": "Model unavailable",
    "INVALID_REQUEST": "Invalid request",
    "TIMEOUT": "Request timed out",
    "PROVIDER_UNAVAILABLE": "Provider unavailable",
    "NETWORK_ERROR": "Network error",
    "EMPTY_RESPONSE": "Empty model response",
    "ERROR": "Provider error",
}


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state.replace("_", " ").capitalize())


# Canonical assistant states (PHASE 1 state machine). These are the states the
# UI renders. They are deliberately distinct from the fine-grained
# `verification_status` and from the human headline `state`/`label`, both of
# which are kept for backward compatibility.
_AI_STATE_LABELS = {
    "CHECKING": "Checking",
    "VERIFIED": "Verified",
    "UNVERIFIED": "Not verified",
    "EXPIRED": "Verification expired",
    "ERROR": "Verification error",
    "QUOTA_EXCEEDED": "Quota exhausted",
    "CONFIGURATION_ERROR": "Configuration error",
}

_QUOTA_STATUSES = {"QUOTA_EXCEEDED", "RATE_LIMITED"}
_CONFIG_ERROR_STATUSES = {"NOT_CONFIGURED", "INVALID_KEY", "MODEL_UNAVAILABLE"}
_VERIFICATION_ERROR_STATUSES = {
    "INVALID_REQUEST",
    "TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "NETWORK_ERROR",
    "ERROR",
    "EMPTY_RESPONSE",
}


def _canonical_ai_state(
    *,
    configured: bool,
    status: str,
    is_verified: bool,
    has_prior_success: bool,
    in_flight: bool,
) -> str:
    """Collapse the internal signals into one honest, canonical state.

    Every state is kept distinct so a cached success, a configuration problem,
    and a live provider failure are never conflated.
    """
    if in_flight:
        return "CHECKING"
    if not configured or status in _CONFIG_ERROR_STATUSES or status == "NOT_CONFIGURED":
        return "CONFIGURATION_ERROR"
    if status in _QUOTA_STATUSES:
        return "QUOTA_EXCEEDED"
    if status in _VERIFICATION_ERROR_STATUSES:
        return "ERROR"
    if is_verified:
        return "VERIFIED"
    if has_prior_success:
        return "EXPIRED"
    return "UNVERIFIED"


def get_ai_status() -> dict[str, object]:
    """Report server-side AI configuration and verification status without exposing secrets."""
    provider, key, model = active_ai_config()
    current_fingerprint = _calculate_fingerprint(provider, key, model)

    with _verification_lock:
        status = _verification["status"]
        verified_at = _verification["verified_at"]
        verified_expires_at = _verification.get("verified_expires_at")
        last_success_at = _verification.get("last_success_at")
        message = _verification["message"]
        fingerprint = _verification["config_fingerprint"]
        error_code = _verification["error_code"]
        retry_after_seconds = _verification.get("retry_after_seconds")
        rec_provider = _verification.get("provider", provider)
        in_flight = bool(_verification.get("in_flight"))

    # A VERIFIED status is a snapshot of a real request, not a permanent health
    # guarantee. Expire it so a subsequent quota/rate-limit failure is surfaced
    # instead of a stale green indicator persisting indefinitely.
    if (
        status == "VERIFIED"
        and verified_expires_at is not None
        and time.time() > float(verified_expires_at)
    ):
        status = "CONNECTING"
        verified_at = None
        message = "Previous Gemini verification succeeded but has expired. Run a real verification request to confirm current access."
        error_code = None

    key_var = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
    model_var = "GEMINI_MODEL" if provider == "gemini" else "OPENAI_MODEL"

    if not key or not model:
        status = "NOT_CONFIGURED"
        verified_at = None
        message = f"Set {key_var} and {model_var} in backend/.env or the backend environment."
        error_code = None
    elif fingerprint != current_fingerprint or rec_provider != provider:
        status = "CONNECTING"
        verified_at = None
        message = f"Credentials for {provider.capitalize()} are configured. Run a real verification request to confirm access."
        error_code = None

    configured = bool(key and model)
    is_verified = status == "VERIFIED" and configured and (fingerprint == current_fingerprint)

    # "Recent generation": a real request succeeded within the recent window.
    generation_recent = False
    if configured and last_success_at:
        try:
            stamp = datetime.fromisoformat(str(last_success_at).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            generation_recent = 0 <= age <= _GENERATION_RECENT_SECONDS
        except Exception:
            generation_recent = False

    # Derive a single, honest headline state from the separated signals. A
    # current provider failure takes priority over a previously successful
    # generation so a stale green light can never mask exhausted quota.
    if not configured:
        state = "NOT_CONFIGURED"
    elif status in {"QUOTA_EXCEEDED", "RATE_LIMITED", "INVALID_KEY", "MODEL_UNAVAILABLE", "INVALID_REQUEST", "TIMEOUT", "PROVIDER_UNAVAILABLE", "NETWORK_ERROR", "ERROR", "EMPTY_RESPONSE"}:
        state = status
    elif is_verified:
        state = "AUTHENTICATED"
    elif generation_recent:
        state = "DEGRADED"
    else:
        state = "UNVERIFIED"

    # Canonical state machine for the UI. A prior success that is merely old
    # reads as EXPIRED (re-verify), which is distinct from a live ERROR, a
    # QUOTA_EXCEEDED failure, and a CONFIGURATION_ERROR.
    has_prior_success = bool(last_success_at)
    ai_state = _canonical_ai_state(
        configured=configured,
        status=status,
        is_verified=is_verified,
        has_prior_success=has_prior_success,
        in_flight=in_flight,
    )

    return {
        "provider": provider,
        "configured": configured,
        "model_configured": bool(model),
        "verified": is_verified,
        "verification_status": status,
        "verified_at": verified_at if (configured and status == "VERIFIED") else None,
        "verified_expires_at": verified_expires_at if status == "VERIFIED" else None,
        "last_success_at": last_success_at if configured else None,
        "message": message,
        "model": model,
        "error_code": error_code,
        "retry_after_seconds": retry_after_seconds if status in {"QUOTA_EXCEEDED", "RATE_LIMITED"} else None,
        "state": state,
        "label": _state_label(state),
        # Canonical assistant state (PHASE 1). The frontend renders this and
        # must not keep a second, conflicting verification state of its own.
        "ai_state": ai_state,
        "ai_state_label": _AI_STATE_LABELS.get(ai_state, ai_state),
        "in_flight": in_flight,
        "verification_lifetime_seconds": int(verification_lifetime_seconds()),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "configuration": {
                "present": configured,
                "provider": provider,
                "model": model if configured else None,
            },
            "provider_connectivity": {
                "verified": is_verified,
                "verified_at": verified_at if is_verified else None,
                "verified_expires_at": verified_expires_at if is_verified else None,
                "last_success_at": last_success_at if configured else None,
            },
            "generation": {
                "last_success_at": last_success_at if configured else None,
                "recent": generation_recent,
            },
            "quota": {
                "exhausted": status == "QUOTA_EXCEEDED",
                "rate_limited": status == "RATE_LIMITED",
                "error_code": error_code if status in {"QUOTA_EXCEEDED", "RATE_LIMITED"} else None,
                "retry_after_seconds": retry_after_seconds if status in {"QUOTA_EXCEEDED", "RATE_LIMITED"} else None,
                "message": message if status in {"QUOTA_EXCEEDED", "RATE_LIMITED"} else None,
            },
        },
    }


def record_verification(
    status: str,
    message: str,
    error_code: str | None = None,
    provider: str | None = None,
    retry_after_seconds: float | None = None,
) -> None:
    """Update server-side AI verification state thread-safely."""
    active_prov, key, model = active_ai_config()
    target_prov = provider or active_prov
    fingerprint = _calculate_fingerprint(target_prov, key, model)

    with _verification_lock:
        now = datetime.now(timezone.utc)
        prior_success = _verification.get("last_success_at")
        _verification.update(
            status=status,
            verified_at=now.isoformat() if status == "VERIFIED" else None,
            verified_expires_at=(now.timestamp() + verification_lifetime_seconds()) if status == "VERIFIED" else None,
            last_success_at=now.isoformat() if status == "VERIFIED" else prior_success,
            message=message,
            config_fingerprint=fingerprint,
            error_code=error_code,
            retry_after_seconds=retry_after_seconds,
            provider=target_prov,
            # Any recorded outcome ends an in-flight verification request.
            in_flight=False,
        )


def set_verification_in_flight(value: bool, provider: str | None = None) -> None:
    """Mark whether a real verification request is currently running.

    Purely a UI hint: it lets the assistant show CHECKING while a request is in
    flight instead of mislabelling the in-progress attempt as EXPIRED/UNVERIFIED.
    """
    with _verification_lock:
        _verification["in_flight"] = bool(value)
        if provider:
            _verification["provider"] = provider


def reset_verification_state() -> None:
    """Reset cached verification state to its initial (unverified) value.

    Safe to call at startup or in tests so a previous verification result
    (including a real provider failure) does not leak into a later
    configuration or request.
    """
    with _verification_lock:
        _verification.update(
            status="CONNECTING",
            verified_at=None,
            verified_expires_at=None,
            last_success_at=None,
            message="No successful API verification has been recorded.",
            config_fingerprint=None,
            error_code=None,
            retry_after_seconds=None,
            provider="gemini",
            in_flight=False,
        )


_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", re.I)
_RETRY_HUMAN_RE = re.compile(r"retry in\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", re.I)


def _parse_retry_delay(value: Any) -> float | None:
    """Parse a Google RPC 'retryDelay' such as '58311s' into seconds."""
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(value))
    if match:
        return float(match.group(1))
    return None


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def parse_gemini_error_details(exc: Exception) -> dict[str, Any]:
    """Extract sanitized quota/retry metadata from a Google GenAI exception.

    Only provider quota metadata and retry timing are returned. The API key,
    request payload, and any user data are never included.
    """
    info: dict[str, Any] = {
        "quota_metric": None,
        "quota_id": None,
        "quota_value": None,
        "model": None,
        "retry_delay_seconds": None,
        "is_daily": False,
    }
    details = getattr(exc, "details", None)
    payload = details if isinstance(details, dict) else {}
    err = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    items = err.get("details") if isinstance(err, dict) else None
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            violations = item.get("violations") or []
            first = violations[0] if violations and isinstance(violations[0], dict) else {}
            info["quota_metric"] = first.get("quotaMetric")
            info["quota_id"] = first.get("quotaId")
            info["quota_value"] = first.get("quotaValue")
            dims = first.get("quotaDimensions") or {}
            info["model"] = dims.get("model") if isinstance(dims, dict) else None
        elif kind.endswith("RetryInfo"):
            info["retry_delay_seconds"] = _parse_retry_delay(item.get("retryDelay"))

    text = str(exc)
    if info["retry_delay_seconds"] is None:
        match = _RETRY_DELAY_RE.search(text)
        if match:
            info["retry_delay_seconds"] = float(match.group(1))
    if info["retry_delay_seconds"] is None:
        match = _RETRY_HUMAN_RE.search(text)
        if match and any(match.groups()):
            hours = int(match.group(1) or 0)
            minutes = int(match.group(2) or 0)
            secs = float(match.group(3) or 0)
            info["retry_delay_seconds"] = hours * 3600 + minutes * 60 + secs

    quota_id = str(info["quota_id"] or "").lower()
    info["is_daily"] = any(token in quota_id for token in ("perday", "per_day", "/day")) or "daily" in quota_id
    return info


def _gemini_quota_message(info: dict[str, Any]) -> str:
    """Clear, provider-agnostic message for exhausted quota (daily/project)."""
    model = info.get("model")
    limit = info.get("quota_value")
    detail = ""
    if model and limit:
        detail = f" for model '{model}' (request limit: {limit})"
    elif model:
        detail = f" for model '{model}'"
    reset = ""
    seconds = info.get("retry_delay_seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        reset = f" The provider indicates the quota resets in about {_format_duration(seconds)}."
    return (
        "The Gemini API daily quota has been exhausted"
        f"{detail}. The AI assistant is temporarily unavailable, but charts, indicators, "
        "market data, BUY/SELL analysis, and paper trading continue to work. Configure "
        "GEMINI_MODEL or GEMINI_FALLBACK_MODEL with a verified model that has available "
        f"quota, or wait for the quota to reset.{reset}"
    )


def _gemini_rate_message(info: dict[str, Any]) -> str:
    reset = ""
    seconds = info.get("retry_delay_seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        reset = f" Retry after about {_format_duration(seconds)}."
    return f"Gemini request rate limit reached. Retrying is temporarily paused.{reset}"


# Provider status labels used by the API and frontend. These are deliberately
# stable; the frontend and existing tests depend on them.
_ERROR_CODES = {
    "QUOTA_EXCEEDED": "GEMINI_QUOTA_EXHAUSTED",
    "RATE_LIMITED": "GEMINI_RATE_LIMITED",
    "INVALID_KEY": "GEMINI_INVALID_KEY",
    "MODEL_UNAVAILABLE": "GEMINI_MODEL_UNAVAILABLE",
    "INVALID_REQUEST": "AI_INVALID_REQUEST",
    "TIMEOUT": "AI_TIMEOUT",
    "PROVIDER_UNAVAILABLE": "AI_PROVIDER_UNAVAILABLE",
    "NETWORK_ERROR": "AI_NETWORK_ERROR",
    "EMPTY_RESPONSE": "GEMINI_EMPTY_RESPONSE",
    "ERROR": "GEMINI_ERROR",
}


class AIServiceError(Exception):
    """A sanitized, structured AI provider failure safe to return to the API layer.

    Carries no credentials, request payloads, or raw stack traces.
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        http_status: int = 502,
        provider: str = "gemini",
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.http_status = http_status
        self.provider = provider
        self.model = model

    def as_error_payload(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": {
                "code": _ERROR_CODES.get(self.status, "AI_ERROR"),
                "message": self.message,
                "retryable": self.retryable,
                "retry_after_seconds": int(self.retry_after_seconds) if self.retry_after_seconds else None,
            },
        }


_TIMEOUT_EXC_TOKENS = ("timeout", "timedout", "deadlineexceeded")
_NETWORK_EXC_TOKENS = (
    "connecterror",
    "connectionerror",
    "networkerror",
    "unreachable",
    "apiconnectionerror",
    "transporterror",
    "remoteprotocolerror",
    "readerror",
    "writeerror",
    "pooltimeout",
    "noderiveerror",
)


def classify_gemini_error(exc: Exception) -> tuple[str, str]:
    """Map Google GenAI SDK exceptions to standard status and safe message.

    Statuses:
    - INVALID_KEY
    - MODEL_UNAVAILABLE
    - RATE_LIMITED
    - QUOTA_EXCEEDED
    - INVALID_REQUEST
    - TIMEOUT
    - PROVIDER_UNAVAILABLE
    - NETWORK_ERROR
    - ERROR
    """
    exc_name = type(exc).__name__
    exc_name_l = exc_name.lower()
    exc_str = str(exc).lower()

    # Check for google.genai.errors.APIError attributes
    code = getattr(exc, "code", None)
    status_str = str(getattr(exc, "status", "") or "").upper()
    details = str(getattr(exc, "details", "") or "").lower()
    msg = str(getattr(exc, "message", "") or "")

    combined = f"{exc_name} {status_str} {msg} {details} {exc_str}".lower()

    # Timeouts are checked first: a gateway timeout (504) is itself a 5xx and a
    # connect/read timeout must not be reported as a generic server error.
    if (
        code in {408, 504}
        or status_str in {"DEADLINE_EXCEEDED", "TIMEOUT"}
        or any(token in exc_name_l for token in _TIMEOUT_EXC_TOKENS)
        or "timed out" in combined
    ):
        return "TIMEOUT", (
            "The Gemini API did not respond within the configured timeout. "
            "The model may be slow or the network unstable; retry shortly or raise "
            "GEMINI_REQUEST_TIMEOUT_MS."
        )

    if code in {401, 403} or status_str in {"UNAUTHENTICATED", "PERMISSION_DENIED"} or "api_key_invalid" in combined or "api key not valid" in combined or "invalid api key" in combined:
        return "INVALID_KEY", "Gemini rejected the configured API key. Verify GEMINI_API_KEY."

    if code == 404 or status_str == "NOT_FOUND" or ("model" in combined and ("not found" in combined or "unsupported" in combined or "is not found" in combined)):
        return "MODEL_UNAVAILABLE", "The configured Gemini model is unavailable to this account or not found. Check GEMINI_MODEL."

    if code == 429 or status_str == "RESOURCE_EXHAUSTED" or "resource_exhausted" in combined or "429" in combined:
        # Distinguish daily/project quota exhaustion from a transient
        # per-minute request rate limit. Prefer structured provider metadata
        # (quotaId/quotaMetric) over message keywords when available.
        info = parse_gemini_error_details(exc)
        if info["quota_id"] or info["quota_metric"]:
            if info["is_daily"]:
                return "QUOTA_EXCEEDED", _gemini_quota_message(info)
            return "RATE_LIMITED", _gemini_rate_message(info)
        if any(term in combined for term in ("quota", "exceeded your current quota", "limit: 0", "insufficient_quota", "bill", "credits")):
            return "QUOTA_EXCEEDED", _gemini_quota_message(info)
        return "RATE_LIMITED", _gemini_rate_message(info)

    if code == 400 or status_str == "INVALID_ARGUMENT":
        return "INVALID_REQUEST", f"Gemini rejected the request format or parameters: {msg or 'Invalid argument'}."

    if (code is not None and 500 <= code < 600) or "servererror" in exc_name_l:
        return "PROVIDER_UNAVAILABLE", "Google Gemini is temporarily unavailable (server error). Please retry shortly."

    if any(net_term in combined for net_term in _NETWORK_EXC_TOKENS) or "connection" in combined or "unreachable" in combined or "network" in combined:
        return "NETWORK_ERROR", "Could not reach the Gemini API. Check network connectivity and try again."

    return "ERROR", f"Gemini API request failed: {msg or str(exc)}"


def classify_openai_error(exc: Exception) -> tuple[str, str]:
    """Map OpenAI SDK exceptions to standard status and safe message."""
    name = type(exc).__name__
    text = str(exc).lower()

    if name == "AuthenticationError" or "unauthorized" in text:
        return "INVALID_KEY", "OpenAI rejected the configured API key. Replace it with a valid key."
    if name == "NotFoundError" or ("model" in text and "not found" in text):
        return "MODEL_UNAVAILABLE", "The configured OpenAI model is unavailable to this account. Check OPENAI_MODEL."
    if name in {"RateLimitError", "InsufficientQuotaError"} or "429" in text:
        if "quota" in text or "insufficient_quota" in text or "billing" in text:
            return "QUOTA_EXCEEDED", "The OpenAI account has exhausted its quota or has insufficient credits."
        return "RATE_LIMITED", "The OpenAI account request rate limit was reached."
    if name in {"APIConnectionError", "APITimeoutError", "ConnectTimeout", "ReadTimeout"}:
        return "ERROR", "Could not reach the OpenAI API. Check network access and try again."
    return "ERROR", "The OpenAI API request failed. Check the model and account configuration."


_RETRYABLE_STATUSES = {"RATE_LIMITED", "TIMEOUT", "PROVIDER_UNAVAILABLE", "NETWORK_ERROR"}
_NON_RETRYABLE_STATUSES = {"INVALID_KEY", "MODEL_UNAVAILABLE", "QUOTA_EXCEEDED", "INVALID_REQUEST", "EMPTY_RESPONSE"}


def is_retryable_error(status: str, exc: Exception) -> bool:
    """Only retry transient network, timeout, or 5xx server errors.

    Never retry auth, quota, invalid-request, or 404 failures: repeating them
    cannot succeed and would stack avoidable latency on an interactive request.
    """
    if status in _NON_RETRYABLE_STATUSES:
        return False
    if status in _RETRYABLE_STATUSES:
        return True
    code = getattr(exc, "code", None)
    if code is not None and code in {400, 401, 403, 404}:
        return False
    name = type(exc).__name__.lower()
    if "connection" in name or "timeout" in name or "servererror" in name:
        return True
    if code is not None and 500 <= code < 600:
        return True
    return False


_BLOCKED_FINISH_REASONS = {
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKED",
    "RECITATION",
    "LANGUAGE",
    "OTHER",
}


def _is_blocked_finish(finish_reason: str) -> bool:
    """True when a candidate was blocked rather than completed normally."""
    if not finish_reason:
        return False
    return finish_reason.upper() in _BLOCKED_FINISH_REASONS


def _response_finish_reasons(response: Any) -> list[str]:
    return [str(getattr(c, "finish_reason", "") or "") for c in (getattr(response, "candidates", None) or [])]


def _extract_response_text(response: Any) -> str:
    """Return the model text, tolerating thought-only or part-based responses."""
    try:
        text = getattr(response, "text", None)
    except Exception:
        text = None
    if isinstance(text, str) and text.strip():
        return text
    chunks: list[str] = []
    for candidate in (getattr(response, "candidates", None) or []):
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            piece = getattr(part, "text", None)
            if isinstance(piece, str) and piece:
                chunks.append(piece)
    return "\n".join(chunks).strip()


_FALLBACK_STATUSES = {
    "MODEL_UNAVAILABLE",
    "QUOTA_EXCEEDED",
    "RATE_LIMITED",
    "EMPTY_RESPONSE",
    "PROVIDER_UNAVAILABLE",
    "TIMEOUT",
    "ERROR",
}


def _resolve_client(genai: Any, key: str, timeout_ms: int) -> Any:
    """Build a Gemini client with a finite per-attempt timeout when supported.

    The timeout is set through ``HttpOptions`` (milliseconds). When the SDK does
    not expose ``HttpOptions`` (for example a test double), the client is built
    without it so behavior is unchanged.
    """
    http_options = None
    try:
        import google.genai.types as genai_types

        if timeout_ms and hasattr(genai_types, "HttpOptions"):
            http_options = genai_types.HttpOptions(timeout=int(timeout_ms))
    except Exception:
        http_options = None
    if http_options is not None:
        return genai.Client(api_key=key, http_options=http_options)
    return genai.Client(api_key=key)


def gemini_generate_content(
    contents: Any,
    config: Any,
    *,
    models: list[str] | None = None,
    max_attempts: int = 2,
    base_delay: float = 0.5,
    max_delay: float = 4.0,
    max_total_wait: float = 8.0,
    allow_empty: bool = False,
    sleep: Any = None,
    total_deadline: float | None = None,
    request_timeout_ms: int | None = None,
) -> tuple[str, str]:
    """Centralized Gemini generation with bounded retry and optional model fallback.

    Returns ``(text, model_used)``. Raises :class:`AIServiceError` with a
    sanitized message on failure. Every HTTP attempt carries a finite timeout and
    the whole call is bounded by a wall-clock deadline, so an interactive chat
    request can never wait indefinitely. Retries are bounded per model, capped by
    ``max_total_wait`` of sleeping, and daily quota is never retried. Models are
    tried in the configured order until one answers or the deadline is reached.
    """
    key, _ = gemini_config()
    if not key:
        raise AIServiceError(
            "INVALID_KEY",
            "Gemini is not configured. Set GEMINI_API_KEY in the backend environment.",
            http_status=503,
            provider="gemini",
        )

    import random

    candidate_models = models or gemini_models()
    do_sleep = sleep or time.sleep
    total_waited = 0.0
    last_error: AIServiceError | None = None
    deadline_exceeded = False

    budget_seconds = total_deadline if total_deadline is not None else gemini_total_deadline_seconds()
    per_request_timeout_ms = request_timeout_ms if request_timeout_ms is not None else gemini_request_timeout_ms()
    now = time.monotonic
    deadline = now() + budget_seconds

    for model in candidate_models:
        attempt = 0
        while True:
            remaining = deadline - now()
            if remaining <= 0:
                deadline_exceeded = True
                break
            try:
                import google.genai as genai

                # Never let one HTTP attempt outlive the remaining budget.
                # Google GenAI requires a minimum deadline of 10s (10000ms) for real
                # requests, but we still pass the capped timeout so the SDK can
                # enforce its own minimum; the test double accepts any value.
                attempt_timeout_ms = max(1, min(int(per_request_timeout_ms), int(remaining * 1000)))
                client = _resolve_client(genai, key, attempt_timeout_ms)
                response = client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as exc:
                status, message = classify_gemini_error(exc)
                info = parse_gemini_error_details(exc)
                retry_after = info.get("retry_delay_seconds")
                exc_code = getattr(exc, "code", None)
                retryable = is_retryable_error(status, exc)
                last_error = AIServiceError(
                    status,
                    message,
                    retryable=retryable,
                    retry_after_seconds=retry_after if isinstance(retry_after, (int, float)) else None,
                    provider="gemini",
                    model=model,
                )

                # Never fall back or retry on authentication or malformed-request
                # errors: a different model cannot fix either.
                if status == "INVALID_KEY" or exc_code in {400, 401, 403}:
                    raise last_error

                # Bounded exponential backoff with jitter for transient errors
                # (per-minute rate limit, timeout, network, 5xx). Daily quota is
                # not retryable here, so it moves straight to the fallback.
                if retryable and attempt < max_attempts - 1:
                    remaining = deadline - now()
                    if remaining <= 0:
                        deadline_exceeded = True
                        break
                    allowed_wait = min(max_total_wait - total_waited, remaining)
                    if isinstance(retry_after, (int, float)) and retry_after > 0:
                        # Respect the provider's Retry-After. If it exceeds our
                        # interactive budget, stop and report the rate limit
                        # instead of retrying too early and burning more quota.
                        if retry_after > allowed_wait:
                            break
                        wait = float(retry_after)
                    else:
                        wait = min(max_delay, base_delay * (2 ** attempt), remaining)
                    wait = min(wait, remaining) + random.uniform(0, 0.05)
                    if wait > 0 and total_waited + wait <= max_total_wait:
                        do_sleep(wait)
                        total_waited += wait
                        attempt += 1
                        continue
                break
            else:
                finish_reasons = _response_finish_reasons(response)
                text = _extract_response_text(response)
                if text:
                    return text, model
                blocked = bool(finish_reasons) and all(_is_blocked_finish(fr) for fr in finish_reasons)
                if blocked:
                    last_error = AIServiceError(
                        "ERROR",
                        f"Gemini returned a blocked response (finish_reason={finish_reasons}).",
                        provider="gemini",
                        model=model,
                    )
                elif allow_empty:
                    # A successful call with no text (e.g. a thinking model whose
                    # budget was consumed internally) is still a valid verification.
                    return "", model
                else:
                    last_error = AIServiceError(
                        "EMPTY_RESPONSE",
                        "Gemini returned an empty or malformed response. The configured model may need a larger output token budget.",
                        provider="gemini",
                        model=model,
                    )
                break

        if deadline_exceeded:
            break

        # Only fall back to another configured model for model-specific or
        # provider-side failures. Anything else stops immediately.
        if last_error is None or last_error.status not in _FALLBACK_STATUSES:
            break

    if deadline_exceeded and (last_error is None or last_error.status in _RETRYABLE_STATUSES):
        raise AIServiceError(
            "TIMEOUT",
            f"The Gemini request exceeded the configured time budget of about {int(budget_seconds)}s. "
            "Please retry shortly or raise GEMINI_TOTAL_DEADLINE_SECONDS.",
            retryable=True,
            http_status=504,
            provider="gemini",
        )
    if last_error is not None:
        raise last_error
    raise AIServiceError("ERROR", "The Gemini API request failed without a response.", provider="gemini")


def verify_gemini_configuration() -> dict[str, object]:
    """Perform a real authenticated request to Google Gemini and classify the response."""
    key, model = gemini_config()
    if not key or not model:
        record_verification(
            "NOT_CONFIGURED",
            "Set GEMINI_API_KEY and GEMINI_MODEL in backend/.env or the backend environment.",
            provider="gemini",
        )
        return get_ai_status()

    record_verification(
        "CONNECTING",
        "A real Google Gemini API verification request is in progress.",
        provider="gemini",
    )
    set_verification_in_flight(True, "gemini")

    try:
        import google.genai.types as types

        config = types.GenerateContentConfig(max_output_tokens=64, temperature=0.0)
        _, used_model = gemini_generate_content(
            contents="Reply with OK.",
            config=config,
            models=gemini_models(),
            allow_empty=True,
        )
        record_verification("VERIFIED", f"A real Google Gemini API request succeeded using {used_model}.", provider="gemini")
    except AIServiceError as exc:
        record_verification(
            exc.status,
            exc.message,
            error_code=exc.status,
            provider="gemini",
            retry_after_seconds=exc.retry_after_seconds,
        )
    finally:
        set_verification_in_flight(False, "gemini")

    return get_ai_status()


def verify_openai_configuration() -> dict[str, object]:
    """Perform a real authenticated request to OpenAI and classify the response."""
    key, model = openai_config()
    if not key or not model:
        record_verification(
            "NOT_CONFIGURED",
            "Set OPENAI_API_KEY and OPENAI_MODEL in backend/.env or the backend environment.",
            provider="openai",
        )
        return get_ai_status()

    record_verification(
        "CONNECTING",
        "A real OpenAI API verification request is in progress.",
        provider="openai",
    )
    set_verification_in_flight(True, "openai")

    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, timeout=20.0, max_retries=0)
        response = client.responses.create(
            model=model,
            input="Reply with OK.",
            max_output_tokens=16,
            store=False,
        )
        output = getattr(response, "output_text", "")
        if not isinstance(output, str) or not output.strip():
            raise ValueError("empty response")
        record_verification("VERIFIED", f"A real OpenAI Responses API request succeeded using {model}.", provider="openai")
    except Exception as exc:
        status_cls, msg = classify_openai_error(exc)
        record_verification(status_cls, msg, error_code=status_cls, provider="openai")
    finally:
        set_verification_in_flight(False, "openai")

    return get_ai_status()


def verify_ai_configuration() -> dict[str, object]:
    """Route verification to the currently active provider."""
    # Ensure fresh environment if backend/.env changed
    reload_env_if_modified()

    provider = get_active_provider()
    if provider == "gemini":
        return verify_gemini_configuration()
    return verify_openai_configuration()
