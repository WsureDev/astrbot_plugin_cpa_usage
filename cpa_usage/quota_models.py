"""Typed models for CPA provider capacity responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def first(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = text(mapping.get(key))
        if value:
            return value
    return ""


def boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def mask_account(value: str) -> str:
    """Keep a short prefix/suffix and mask the middle of an account identifier."""
    value = text(value)
    if not value:
        return "unknown"
    if len(value) <= 8:
        return value[:2] + "*" * max(len(value) - 4, 1) + value[-2:]
    return value[:3] + "*********" + value[-4:]


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    key: str
    label: str
    remaining_percent: float | None = None
    remaining_value: float | None = None
    limit_value: float | None = None
    reset_at: datetime | None = None
    reset_after_seconds: int | None = None
    window_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ResetCredit:
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class QuotaAccount:
    auth_index: str
    provider_type: str
    provider_name: str
    account_display: str
    plan: str | None = None
    renew_at: datetime | None = None
    reset_credit_count: int | None = None
    reset_credit_expiries: list[ResetCredit] = field(default_factory=list)
    windows: list[QuotaWindow] = field(default_factory=list)
    error: str | None = None
    disabled: bool = False
    account_id: str | None = None
    project_id: str | None = None
    xai_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    accounts: list[QuotaAccount]
    fetched_at: datetime


def normalize_provider_type(value: str) -> str:
    normalized = text(value).lower().replace("_", "-")
    aliases = {
        "gemini": "gemini-cli",
        "gemini-cli-code-assist": "gemini-cli",
        "google-gemini": "gemini-cli",
    }
    return aliases.get(normalized, normalized)


def quota_account_from_raw(raw: Mapping[str, Any]) -> QuotaAccount | None:
    auth_index = first(raw, "auth_index", "auth-index", "authIndex")
    if not auth_index:
        return None
    provider_type = normalize_provider_type(first(raw, "type", "provider") or "unknown")
    provider_name = first(raw, "provider", "type") or provider_type
    name = first(raw, "email", "label", "name", "auth_index", "authIndex")
    id_token = raw.get("id_token") if isinstance(raw.get("id_token"), Mapping) else {}
    account_id = first(id_token, "chatgpt_account_id", "chatgptAccountId") or first(raw, "account", "account_id")
    project_id = first(raw, "project_id", "projectId") or None
    plan = first(id_token, "plan_type", "planType") or None
    renew_at = parse_time(id_token.get("chatgpt_subscription_active_until", id_token.get("chatgptSubscriptionActiveUntil")))
    xai_user_id = resolve_xai_user_id(raw)
    return QuotaAccount(
        auth_index=auth_index,
        provider_type=provider_type,
        provider_name=provider_name,
        account_display=mask_account(name),
        plan=plan,
        renew_at=renew_at,
        disabled=boolean(raw.get("disabled")),
        account_id=account_id or None,
        project_id=project_id,
        xai_user_id=xai_user_id,
    )


def resolve_xai_user_id(raw: Mapping[str, Any]) -> str | None:
    """Match Keeper's preferred xAI subject candidates without exposing them."""
    candidates: list[Any] = []
    for key in ("sub", "subject", "user_id", "userId"):
        candidates.append(raw.get(key))
    for container_key in ("metadata", "attributes", "oauth", "user"):
        container = raw.get(container_key)
        if isinstance(container, Mapping):
            for key in ("sub", "subject", "user_id", "userId", "id"):
                candidates.append(container.get(key))
            for nested_key in ("oauth", "user"):
                nested = container.get(nested_key)
                if isinstance(nested, Mapping):
                    candidates.extend(nested.get(key) for key in ("sub", "subject", "user_id", "userId", "id"))
    for candidate in candidates:
        value = text(candidate)
        if value:
            return value
    return None
