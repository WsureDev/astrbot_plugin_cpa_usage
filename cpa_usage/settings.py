"""Shared CPA endpoint configuration for CLI and AstrBot adapters."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .api import CPAUsageClient


def as_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def find_provider_config(
    providers: Iterable[Mapping[str, Any]], provider_id: str
) -> Mapping[str, Any] | None:
    provider_id = provider_id.strip()
    for provider in providers:
        try:
            if not as_bool(provider.get("enabled"), True):
                continue
        except ValueError:
            # Invalid enable flags should not accidentally activate a mapping.
            continue
        configured_id = str(provider.get("provider_id") or provider.get("id") or "").strip()
        if configured_id and configured_id == provider_id:
            return provider
    return None


def client_from_config(
    provider: Mapping[str, Any],
    *,
    default_timeout: float = 20.0,
    default_tls_verify: bool = True,
) -> CPAUsageClient:
    base_url = str(provider.get("base_url") or provider.get("url") or "").strip()
    token = str(provider.get("token") or provider.get("api_token") or "").strip()
    if not base_url:
        raise ValueError("CPA base_url is empty")
    if not token:
        raise ValueError("CPA token is empty")
    return CPAUsageClient(
        base_url,
        token,
        token_header=str(provider.get("token_header") or "Authorization"),
        token_prefix=str(
            provider.get("token_prefix")
            if provider.get("token_prefix") is not None
            else "Bearer"
        ),
        token_separator=str(
            provider.get("token_separator")
            if provider.get("token_separator") is not None
            else " "
        ),
        management_prefix=str(provider.get("management_prefix") or "/v0/management"),
        timeout=float(provider.get("timeout") or default_timeout),
        tls_verify=as_bool(provider.get("tls_verify"), default_tls_verify),
    )
