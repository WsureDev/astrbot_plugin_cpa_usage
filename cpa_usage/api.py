"""Direct CPA management API client and read-only quota normalization.

This module deliberately does not call CPA Usage Keeper. It reads CPA auth-file
metadata and uses CPA's management api-call proxy for provider quota checks.
"""

from __future__ import annotations

import json
import math
import ssl
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .quota_models import (
    QuotaAccount,
    QuotaSnapshot,
    QuotaWindow,
    ResetCredit,
    normalize_provider_type,
    quota_account_from_raw,
)


class CPAUsageError(RuntimeError):
    """Raised for transport errors, HTTP errors, or malformed CPA payloads."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class QuotaRequestConfig:
    """Read-only upstream request sent through CPA's management api-call."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    data: Any = None


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, Mapping[str, str], bytes]]


class CPAUsageClient:
    """Direct CPA client: metadata + read-only upstream quota checks."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        token_header: str = "Authorization",
        token_prefix: str = "Bearer",
        token_separator: str = " ",
        management_prefix: str = "/v0/management",
        timeout: float = 20.0,
        tls_verify: bool = True,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.strip().rstrip("/")
        self.token = token.strip()
        self.token_header = token_header.strip() or "Authorization"
        self.token_prefix = token_prefix.strip()
        self.token_separator = token_separator
        prefix = management_prefix.strip() or "/v0/management"
        self.management_prefix = "/" + prefix.strip("/")
        self.timeout = timeout
        self.tls_verify = tls_verify
        self._transport = transport or self._urlopen_transport
        if not self.base_url:
            raise ValueError("CPA base_url is required")
        if not self.token:
            raise ValueError("CPA token is required")

    def _urlopen_transport(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> tuple[int, Mapping[str, str], bytes]:
        context = ssl.create_default_context() if self.tls_verify else ssl._create_unverified_context()
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()
        except urllib.error.URLError as exc:
            raise CPAUsageError(f"CPA request failed: {exc.reason}") from exc

    def _request_json(self, path: str, *, method: str = "GET", body: Any = None) -> Any:
        normalized_path = path if path.startswith("/") else "/" + path
        url = f"{self.base_url}{normalized_path}"
        auth_value = f"{self.token_prefix}{self.token_separator}{self.token}".strip()
        encoded_body = None
        headers = {"Accept": "application/json", self.token_header: auth_value}
        if body is not None:
            encoded_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, _, raw = self._transport(
            method.upper(),
            url,
            headers,
            encoded_body,
            self.timeout,
        )
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CPAUsageError("CPA response is not valid JSON", status=status, body=raw) from exc
        if status < 200 or status >= 300:
            message = payload.get("error") if isinstance(payload, Mapping) else None
            raise CPAUsageError(message or f"CPA request returned HTTP {status}", status=status, body=payload)
        return payload

    def fetch_auth_files(self) -> list[Mapping[str, Any]]:
        payload = self._request_json(f"{self.management_prefix}/auth-files")
        return self._list_payload(payload, "files")

    def call_upstream(self, auth_index: str, request: QuotaRequestConfig) -> Any:
        """Call one provider's read-only quota endpoint through CPA.

        CPA authenticates the selected auth file and returns an api-call wrapper
        with ``statusCode`` and a JSON-encoded ``body``. This method unwraps it
        and never calls the usage queue or a reset/consume endpoint.
        """
        if not auth_index.strip():
            raise ValueError("auth_index is required")
        body: dict[str, Any] = {
            "authIndex": auth_index.strip(),
            "method": request.method.upper(),
            "url": request.url,
        }
        if request.headers:
            body["header"] = dict(request.headers)
        if request.data is not None:
            body["data"] = request.data if isinstance(request.data, str) else json.dumps(request.data, separators=(",", ":"))
        wrapper = self._request_json(f"{self.management_prefix}/api-call", method="POST", body=body)
        if not isinstance(wrapper, Mapping):
            raise CPAUsageError("CPA api-call response must be a JSON object", body=wrapper)
        upstream_status = _number(wrapper.get("statusCode", wrapper.get("status_code")))
        raw_body = wrapper.get("body", wrapper.get("bodyText", wrapper.get("body_text")))
        decoded = raw_body
        if isinstance(raw_body, str):
            try:
                decoded = json.loads(raw_body)
            except json.JSONDecodeError:
                decoded = raw_body
        if upstream_status and (upstream_status < 200 or upstream_status >= 300):
            raise CPAUsageError(f"upstream quota request returned HTTP {int(upstream_status)}", status=int(upstream_status), body=decoded)
        return decoded

    def fetch_quota_snapshot(
        self,
        *,
        provider_type: str | None = None,
        include_disabled: bool = False,
        request_configs: Mapping[str, QuotaRequestConfig] | None = None,
    ) -> QuotaSnapshot:
        """Read current remaining quota without consuming usage events."""
        fetched_at = datetime.now(timezone.utc)
        accounts: list[QuotaAccount] = []
        configs = dict(DEFAULT_QUOTA_REQUESTS)
        if request_configs:
            configs.update(request_configs)
        provider_filter = normalize_provider_type(provider_type or "")
        pending: list[QuotaAccount] = []
        seen: set[tuple[str, str]] = set()
        for raw in self.fetch_auth_files():
            account = quota_account_from_raw(raw)
            if account is None:
                continue
            if provider_filter and account.provider_type.lower() != provider_filter:
                continue
            key = (account.provider_type.lower(), account.auth_index)
            if key in seen:
                continue
            seen.add(key)
            if account.disabled and not include_disabled:
                continue
            pending.append(account)
        if pending:
            worker_count = min(10, len(pending))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                accounts = list(pool.map(lambda account: self._fetch_account_quota_safe(account, configs), pending))
        return QuotaSnapshot(accounts, fetched_at)

    def _fetch_account_quota_safe(self, account: QuotaAccount, configs: Mapping[str, QuotaRequestConfig]) -> QuotaAccount:
        try:
            return self._fetch_account_quota(account, configs)
        except (CPAUsageError, ValueError) as exc:
            return account_with_error(account, str(exc))

    def _fetch_account_quota(self, account: QuotaAccount, configs: Mapping[str, QuotaRequestConfig]) -> QuotaAccount:
        provider = account.provider_type.lower()
        request = configs.get(provider)
        if request is None:
            return account_with_error(account, f"unsupported quota provider: {account.provider_type}")
        if provider == "xai":
            monthly_config = configs.get("xai-monthly", request)
            with ThreadPoolExecutor(max_workers=2) as pool:
                weekly_future = pool.submit(self.call_upstream, account.auth_index, request_for_account(request, account))
                monthly_future = pool.submit(self.call_upstream, account.auth_index, request_for_account(monthly_config, account))
                weekly = _future_or_none(weekly_future)
                monthly = _future_or_none(monthly_future)
            if weekly is None and monthly is None:
                return account_with_error(account, "xAI billing requests failed")
            return parse_xai_quota(account, weekly, monthly)
        if provider == "antigravity":
            if not account.project_id:
                return account_with_error(account, "missing project_id parameter")
            last_error: Exception | None = None
            parsed_quota: QuotaAccount | None = None
            for candidate in (request, configs.get("antigravity-sandbox"), configs.get("antigravity-fallback")):
                if candidate is None:
                    continue
                try:
                    payload = self.call_upstream(account.auth_index, request_for_account(candidate, account))
                    parsed = parse_antigravity_quota(account, payload)
                    if parsed.windows:
                        parsed_quota = parsed
                        break
                    last_error = ValueError("empty antigravity quota response")
                except Exception as exc:  # try the next CPA-supported endpoint
                    last_error = exc
            result = parsed_quota or account_with_error(account, str(last_error or "antigravity quota request failed"))
            subscription_request = configs.get("antigravity-subscription")
            if subscription_request and account.project_id:
                try:
                    subscription = self.call_upstream(account.auth_index, request_for_account(subscription_request, account))
                    result = parse_antigravity_subscription(result, subscription)
                except (CPAUsageError, ValueError):
                    pass
            return result
        if provider == "gemini-cli" and not account.project_id:
            return account_with_error(account, "missing project_id parameter")
        parsed = parse_quota_account(account, self.call_upstream(account.auth_index, request_for_account(request, account)))
        if provider == "codex":
            reset_request = configs.get("codex-reset-credits")
            if reset_request:
                try:
                    reset_payload = self.call_upstream(account.auth_index, request_for_account(reset_request, account))
                    parsed = parse_codex_reset_credits(parsed, reset_payload)
                except (CPAUsageError, ValueError):
                    pass
        if provider == "claude":
            profile_request = configs.get("claude-profile")
            if profile_request:
                try:
                    profile = self.call_upstream(account.auth_index, request_for_account(profile_request, account))
                    parsed = parse_claude_profile_metadata(parsed, profile)
                except (CPAUsageError, ValueError):
                    pass
        if provider == "gemini-cli":
            code_assist = configs.get("gemini-code-assist")
            if code_assist and account.project_id:
                try:
                    tier = self.call_upstream(account.auth_index, request_for_account(code_assist, account))
                    parsed = parse_gemini_tier_metadata(parsed, tier)
                except (CPAUsageError, ValueError):
                    pass
        return parsed

    @staticmethod
    def _list_payload(payload: Any, wrapper: str) -> list[Mapping[str, Any]]:
        values = payload if isinstance(payload, list) else payload.get(wrapper, []) if isinstance(payload, Mapping) else []
        return [item for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []

DEFAULT_QUOTA_REQUESTS: dict[str, QuotaRequestConfig] = {
    "codex": QuotaRequestConfig("GET", "https://chatgpt.com/backend-api/wham/usage", {"Authorization": "Bearer $TOKEN$", "Chatgpt-Account-Id": "$ACCOUNT_ID$", "Content-Type": "application/json", "User-Agent": "codex_cli_rs/0.76.0", "Originator": "Codex Desktop"}),
    "codex-reset-credits": QuotaRequestConfig("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {"Authorization": "Bearer $TOKEN$", "Accept": "application/json", "OpenAI-Beta": "codex-1", "Originator": "Codex Desktop"}),
    "claude": QuotaRequestConfig("GET", "https://api.anthropic.com/api/oauth/usage", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20"}),
    "kimi": QuotaRequestConfig("GET", "https://api.kimi.com/coding/v1/usages", {"Authorization": "Bearer $TOKEN$"}),
    "xai": QuotaRequestConfig("GET", "https://cli-chat-proxy.grok.com/v1/billing?format=credits", {"Authorization": "Bearer $TOKEN$", "x-xai-token-auth": "xai-grok-cli", "x-grok-client-version": "0.2.93", "x-userid": "$XAI_USER_ID$", "Accept": "*/*"}),
    "xai-monthly": QuotaRequestConfig("GET", "https://cli-chat-proxy.grok.com/v1/billing", {"Authorization": "Bearer $TOKEN$", "x-xai-token-auth": "xai-grok-cli", "x-grok-client-version": "0.2.93", "x-userid": "$XAI_USER_ID$", "Accept": "*/*"}),
    "claude-profile": QuotaRequestConfig("GET", "https://api.anthropic.com/api/oauth/profile", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20"}),
    "gemini": QuotaRequestConfig("POST", "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json"}, {"project": "$PROJECT_ID$"}),
    "gemini-cli": QuotaRequestConfig("POST", "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json"}, {"project": "$PROJECT_ID$"}),
    "gemini-code-assist": QuotaRequestConfig("POST", "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json"}, {"cloudaicompanionProject": "$PROJECT_ID$", "metadata": {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI", "duetProject": "$PROJECT_ID$"}}),
    "antigravity": QuotaRequestConfig("POST", "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json", "User-Agent": "antigravity/cli/1.0.13 (aidev_client; os_type=linux; arch=x86_64)"}, {"project": "$PROJECT_ID$"}),
    "antigravity-sandbox": QuotaRequestConfig("POST", "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json", "User-Agent": "antigravity/cli/1.0.13 (aidev_client; os_type=linux; arch=x86_64)"}, {"project": "$PROJECT_ID$"}),
    "antigravity-fallback": QuotaRequestConfig("POST", "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json", "User-Agent": "antigravity/cli/1.0.13 (aidev_client; os_type=linux; arch=x86_64)"}, {"project": "$PROJECT_ID$"}),
    "antigravity-subscription": QuotaRequestConfig("POST", "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist", {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json", "User-Agent": "antigravity/cli/1.0.13 (aidev_client; os_type=linux; arch=x86_64)"}, {"metadata": {"ideType": "ANTIGRAVITY"}}),
}


def request_for_account(request: QuotaRequestConfig, account: QuotaAccount) -> QuotaRequestConfig:
    def substitute(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("$AUTH_INDEX$", account.auth_index).replace("$AUTH_INDEX", account.auth_index).replace("$PROJECT_ID$", account.project_id or "").replace("$PROJECT_ID", account.project_id or "").replace("$ACCOUNT_ID$", account.account_id or "").replace("$ACCOUNT_ID", account.account_id or "").replace("$XAI_USER_ID$", account.xai_user_id or "").replace("$XAI_USER_ID", account.xai_user_id or "")
        if isinstance(value, Mapping):
            return {key: substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value
    headers = substitute(request.headers)
    if isinstance(headers, Mapping) and not account.account_id:
        headers = {key: value for key, value in headers.items() if not (key.lower() == "chatgpt-account-id" and not value)}
    if isinstance(headers, Mapping) and not account.xai_user_id:
        headers = {key: value for key, value in headers.items() if not (key.lower() == "x-userid" and not value)}
    return QuotaRequestConfig(request.method, substitute(request.url), headers, substitute(request.data))


def account_with_error(account: QuotaAccount, error: str) -> QuotaAccount:
    return replace(account, error=error)


def _future_or_none(future: Any) -> Any:
    try:
        return future.result()
    except Exception:
        return None


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_quota_account(account: QuotaAccount, payload: Any) -> QuotaAccount:
    if not isinstance(payload, Mapping):
        return account_with_error(account, "upstream quota body is not an object")
    provider = account.provider_type.lower()
    if provider == "codex":
        return parse_codex_quota(account, payload)
    if provider == "claude":
        return parse_claude_quota(account, payload)
    if provider == "kimi":
        return parse_kimi_quota(account, payload)
    if provider == "gemini" or provider == "gemini-cli":
        return parse_gemini_quota(account, payload)
    if provider == "antigravity":
        return parse_antigravity_quota(account, payload)
    if provider == "xai":
        return parse_generic_quota(account, payload)
    return parse_generic_quota(account, payload)


def _window(key: str, label: str, raw: Mapping[str, Any], *, used_percent_key: str = "used_percent") -> QuotaWindow:
    used_value = raw.get(used_percent_key, raw.get("utilization", raw.get("usedPercent")))
    remaining_percent: float | None = None
    if used_value is not None:
        used = _number(used_value)
        # Codex and Claude report used_percent/utilization in 0..100 units.
        remaining_percent = max(0.0, min(100.0, 100.0 - used))
    if raw.get("remaining_fraction") is not None or raw.get("remainingFraction") is not None:
        remaining_percent = max(0.0, min(100.0, 100.0 * _number(raw.get("remaining_fraction", raw.get("remainingFraction")))))
    remaining = raw.get("remaining", raw.get("remaining_amount", raw.get("remainingAmount")))
    limit = raw.get("limit")
    reset_at = _parse_time(raw.get("reset_at", raw.get("resetAt", raw.get("reset_time", raw.get("resetTime")))))
    reset_after = raw.get("reset_after_seconds", raw.get("resetAfterSeconds", raw.get("resetIn")))
    window_seconds = raw.get("limit_window_seconds", raw.get("limitWindowSeconds", raw.get("window_seconds", raw.get("windowSeconds"))))
    return QuotaWindow(key, label, remaining_percent, _number(remaining) if remaining is not None else None, _number(limit) if limit is not None else None, reset_at, int(_number(reset_after)) if reset_after is not None else None, int(_number(window_seconds)) if window_seconds is not None else None)


def parse_codex_quota(account: QuotaAccount, payload: Mapping[str, Any]) -> QuotaAccount:
    windows: list[QuotaWindow] = []
    _append_codex_rate_windows(windows, "rate_limit", "", payload.get("rate_limit", payload.get("rateLimit")))
    _append_codex_rate_windows(windows, "code_review_rate_limit", "Code Review · ", payload.get("code_review_rate_limit", payload.get("codeReviewRateLimit")))
    additional = payload.get("additional_rate_limits", payload.get("additionalRateLimits", []))
    if isinstance(additional, Mapping):
        additional = [dict(value, limit_name=key) for key, value in additional.items() if isinstance(value, Mapping)]
    for index, item in enumerate(additional if isinstance(additional, list) else []):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("limit_name") or item.get("limitName") or item.get("metered_feature") or item.get("meteredFeature") or f"Additional {index + 1}").strip()
        _append_codex_rate_windows(windows, f"additional.{name}", name + " · ", item.get("rate_limit", item.get("rateLimit", item)))
    reset_raw = payload.get("rate_limit_reset_credits", payload.get("rateLimitResetCredits"))
    reset = reset_raw if isinstance(reset_raw, Mapping) else {}
    credits = reset.get("credits", [])
    expiries = [ResetCredit(_parse_time(item.get("expires_at", item.get("expiresAt")))) for item in credits if isinstance(item, Mapping)]
    count = reset.get("available_count", reset.get("availableCount"))
    return replace(account, plan=str(payload.get("plan_type") or payload.get("planType") or account.plan or "").strip() or None, reset_credit_count=int(_number(count)) if count is not None else None, reset_credit_expiries=expiries, windows=windows)


def _append_codex_rate_windows(windows: list[QuotaWindow], key_prefix: str, label_prefix: str, rate: Any) -> None:
    if not isinstance(rate, Mapping):
        return
    for role, camel, fallback in (("primary_window", "primaryWindow", "主限额"), ("secondary_window", "secondaryWindow", "次限额")):
        raw = rate.get(role, rate.get(camel))
        if not isinstance(raw, Mapping):
            continue
        seconds = int(_number(raw.get("limit_window_seconds", raw.get("limitWindowSeconds"))))
        label = "5 小时限额" if seconds == 18000 else "周限额" if seconds == 604800 else "月限额" if seconds in {2592000, 2628000, 2678400} else fallback
        windows.append(_window(f"{key_prefix}.{role}", label_prefix + label, raw))


def parse_codex_reset_credits(account: QuotaAccount, payload: Any) -> QuotaAccount:
    if not isinstance(payload, Mapping):
        return account
    credits = payload.get("credits", [])
    expiries = [ResetCredit(_parse_time(item.get("expires_at", item.get("expiresAt")))) for item in credits if isinstance(item, Mapping) and str(item.get("status", "available")).lower() == "available"]
    count = payload.get("available_count", payload.get("availableCount"))
    return replace(account, reset_credit_count=int(_number(count)) if count is not None else (len(expiries) if expiries else account.reset_credit_count), reset_credit_expiries=expiries or account.reset_credit_expiries)


def parse_claude_quota(account: QuotaAccount, payload: Mapping[str, Any]) -> QuotaAccount:
    windows: list[QuotaWindow] = []
    labels = (("five_hour", "fiveHour", "5 小时限额"), ("seven_day", "sevenDay", "周限额"), ("seven_day_opus", "sevenDayOpus", "周限额 · Opus"), ("seven_day_sonnet", "sevenDaySonnet", "周限额 · Sonnet"), ("seven_day_oauth_apps", "sevenDayOauthApps", "周限额 · OAuth Apps"), ("seven_day_cowork", "sevenDayCowork", "周限额 · Cowork"), ("iguana_necktie", "iguanaNecktie", "Iguana Necktie"))
    for key, camel, label in labels:
        raw = payload.get(key, payload.get(camel))
        if isinstance(raw, Mapping):
            windows.append(_window(key, label, raw, used_percent_key="utilization"))
    extra = payload.get("extra_usage", payload.get("extraUsage"))
    if isinstance(extra, Mapping):
        normalized = dict(extra)
        limit = extra.get("monthly_limit", extra.get("monthlyLimit"))
        used = extra.get("used_credits", extra.get("usedCredits"))
        normalized["limit"] = limit
        if limit is not None and used is not None:
            normalized["remaining"] = max(0.0, _number(limit) - _number(used))
        windows.append(_window("extra_usage", "额外用量", normalized, used_percent_key="utilization"))
    return replace(account, windows=windows)


def parse_kimi_quota(account: QuotaAccount, payload: Mapping[str, Any]) -> QuotaAccount:
    windows: list[QuotaWindow] = []
    for index, raw in enumerate(payload.get("limits", []) if isinstance(payload.get("limits"), list) else []):
        if not isinstance(raw, Mapping):
            continue
        detail = raw.get("detail") if isinstance(raw.get("detail"), Mapping) else raw
        if isinstance(raw.get("remaining"), (int, float, str)):
            detail = raw
        windows.append(_window(str(raw.get("name") or index), str(raw.get("title") or raw.get("name") or "限额"), detail))
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else None
    if usage:
        windows.append(_window("usage", str(usage.get("title") or usage.get("name") or "总额度"), usage))
    return replace(account, windows=windows)


def parse_gemini_quota(account: QuotaAccount, payload: Mapping[str, Any]) -> QuotaAccount:
    windows: list[QuotaWindow] = []
    for index, raw in enumerate(payload.get("buckets", []) if isinstance(payload.get("buckets"), list) else []):
        if isinstance(raw, Mapping):
            windows.append(_window(str(raw.get("bucket_id") or raw.get("bucketId") or index), str(raw.get("display_name") or raw.get("displayName") or raw.get("model_id") or raw.get("modelId") or "限额"), raw))
    return replace(account, windows=windows)


def parse_antigravity_quota(account: QuotaAccount, payload: Mapping[str, Any]) -> QuotaAccount:
    windows: list[QuotaWindow] = []
    if isinstance(payload.get("body"), Mapping) and not payload.get("groups"):
        payload = payload["body"]
    for group in payload.get("groups", []) if isinstance(payload.get("groups"), list) else []:
        if not isinstance(group, Mapping):
            continue
        for index, raw in enumerate(group.get("buckets", []) if isinstance(group.get("buckets"), list) else []):
            if isinstance(raw, Mapping):
                group_name = str(group.get("displayName") or group.get("display_name") or "").strip()
                bucket_name = str(raw.get("displayName") or raw.get("display_name") or raw.get("bucketId") or "限额")
                label = f"{group_name} · {bucket_name}" if group_name else bucket_name
                windows.append(_window(str(raw.get("bucketId") or index), label, raw))
    return replace(account, windows=windows)


def parse_antigravity_subscription(account: QuotaAccount, payload: Any) -> QuotaAccount:
    if not isinstance(payload, Mapping):
        return account
    if isinstance(payload.get("body"), Mapping):
        payload = payload["body"]
    current = payload.get("currentTier", payload.get("current_tier"))
    paid = payload.get("paidTier", payload.get("paid_tier"))
    tier = paid if isinstance(paid, Mapping) and paid.get("name") else current
    if not isinstance(tier, Mapping):
        return account
    plan = str(tier.get("name") or tier.get("id") or "").strip() or account.plan
    return replace(account, plan=plan)


def parse_gemini_tier_metadata(account: QuotaAccount, payload: Any) -> QuotaAccount:
    if not isinstance(payload, Mapping):
        return account
    paid = payload.get("paidTier", payload.get("paid_tier"))
    current = payload.get("currentTier", payload.get("current_tier"))
    tier = paid if isinstance(paid, Mapping) and (paid.get("name") or paid.get("id")) else current
    if not isinstance(tier, Mapping):
        return account
    plan = str(tier.get("name") or tier.get("id") or "").strip() or account.plan
    windows = list(account.windows)
    for index, credit in enumerate(tier.get("availableCredits", tier.get("available_credits", [])) if isinstance(tier.get("availableCredits", tier.get("available_credits", [])), list) else []):
        if not isinstance(credit, Mapping):
            continue
        amount = credit.get("creditAmount", credit.get("credit_amount"))
        credit_type = str(credit.get("creditType") or credit.get("credit_type") or f"Credit {index + 1}")
        if amount is not None:
            windows.append(QuotaWindow(f"credit.{credit_type}", credit_type, remaining_value=_number(amount)))
    return replace(account, plan=plan, windows=windows)


def parse_claude_profile_metadata(account: QuotaAccount, payload: Any) -> QuotaAccount:
    if not isinstance(payload, Mapping):
        return account
    profile = payload.get("account") if isinstance(payload.get("account"), Mapping) else payload
    name = str(profile.get("email") or profile.get("display_name") or profile.get("displayName") or "").strip()
    return replace(account, account_display=mask_profile_account(name, account.account_display), plan=account.plan or ("Max" if profile.get("has_claude_max", profile.get("hasClaudeMax")) else "Pro" if profile.get("has_claude_pro", profile.get("hasClaudePro")) else None))


def mask_profile_account(value: str, fallback: str) -> str:
    if not value:
        return fallback
    from .quota_models import mask_account
    return mask_account(value)


def parse_xai_quota(account: QuotaAccount, weekly: Any, monthly: Any) -> QuotaAccount:
    windows: list[QuotaWindow] = []
    for label, payload in (("周限额", weekly), ("月度额度", monthly)):
        config = payload.get("config") if isinstance(payload, Mapping) and isinstance(payload.get("config"), Mapping) else payload
        if not isinstance(config, Mapping):
            continue
        period = config.get("currentPeriod", config.get("current_period")) if isinstance(config.get("currentPeriod", config.get("current_period")), Mapping) else {}
        reset_at = _parse_time(config.get("billingPeriodEnd", config.get("billing_period_end"))) or _parse_time(period.get("end", period.get("reset_at")))
        percent = config.get("creditUsagePercent", config.get("credit_usage_percent"))
        if percent is not None:
            windows.append(QuotaWindow("billing." + label, label, max(0.0, min(100.0, 100.0 - _number(percent))), reset_at=reset_at, window_seconds=604800 if label == "周限额" else 2592000))
        if label == "月度额度":
            limit_obj = config.get("monthlyLimit", config.get("monthly_limit"))
            used_obj = config.get("used")
            limit = _number(limit_obj.get("val") if isinstance(limit_obj, Mapping) else limit_obj)
            used = _number(used_obj.get("val") if isinstance(used_obj, Mapping) else used_obj)
            if limit is not None and used is not None and limit > 0:
                windows.append(QuotaWindow("billing.monthly", "月度额度", max(0.0, min(100.0, 100.0 * (limit - used) / limit)), remaining_value=max(0.0, limit - used), limit_value=limit, reset_at=reset_at, window_seconds=2592000))
        for product in config.get("productUsage", config.get("product_usage", [])) if isinstance(config.get("productUsage", config.get("product_usage", [])), list) else []:
            usage_percent = product.get("usagePercent", product.get("usage_percent")) if isinstance(product, Mapping) else None
            if isinstance(product, Mapping) and usage_percent is not None:
                used = _number(usage_percent)
                windows.append(QuotaWindow("product." + str(product.get("product", "")), str(product.get("product", "产品")), max(0.0, 100.0 - used), reset_at=reset_at, window_seconds=604800))
    return replace(account, windows=windows)


def parse_generic_quota(account: QuotaAccount, payload: Mapping[str, Any]) -> QuotaAccount:
    windows: list[QuotaWindow] = []
    def visit(value: Any, path: str = "") -> None:
        if isinstance(value, Mapping):
            if any(key in value for key in ("remaining", "remaining_fraction", "remainingFraction", "used_percent", "utilization")):
                label = path.replace("_", " ").strip().title() or "限额"
                windows.append(_window(path or str(len(windows)), label, value))
                return
            for key, child in value.items():
                visit(child, f"{path}.{key}".strip("."))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}.{index}".strip("."))
    visit(payload)
    return replace(account, windows=windows)
