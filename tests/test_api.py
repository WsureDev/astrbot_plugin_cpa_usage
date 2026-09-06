import json
import unittest

from cpa_usage.api import CPAUsageClient, CPAUsageError, QuotaRequestConfig, parse_quota_account, parse_xai_quota
from cpa_usage.quota_models import QuotaAccount


class APIClientTests(unittest.TestCase):
    def test_quota_snapshot_uses_read_only_api_call(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, headers, body, timeout))
            path = url.split("?", 1)[0]
            if path.endswith("/auth-files"):
                payload = {"files": [{"auth_index": "auth-1", "email": "user@example.com", "type": "codex", "provider": "OpenAI", "id_token": {"plan_type": "plus"}}]}
                return 200, {}, json.dumps(payload).encode()
            if path.endswith("/api-call"):
                request = json.loads(body)
                self.assertEqual(request["authIndex"], "auth-1")
                self.assertEqual(request["method"], "GET")
                if request["url"].endswith("/usage"):
                    self.assertEqual(request["header"]["Authorization"], "Bearer $TOKEN$")
                    upstream = {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 0, "reset_at": 1780000000}, "secondary_window": {"used_percent": 58, "reset_at": 1780100000}}, "rate_limit_reset_credits": {"available_count": 2, "credits": [{"expires_at": 1780200000}, {"expires_at": 1780300000}]}}
                else:
                    upstream = {"credits": [], "available_count": 2}
                return 200, {}, json.dumps({"statusCode": 200, "body": json.dumps(upstream)}).encode()
            return 200, {}, b"[]"

        client = CPAUsageClient("https://cpa.example", "management-key", transport=transport)
        snapshot = client.fetch_quota_snapshot(provider_type="codex")

        self.assertEqual(len(snapshot.accounts), 1)
        account = snapshot.accounts[0]
        self.assertEqual(account.account_display, "use*********.com")
        self.assertEqual(account.plan, "plus")
        self.assertEqual(account.reset_credit_count, 2)
        self.assertEqual([window.remaining_percent for window in account.windows], [100, 42])
        self.assertEqual(sum(path.endswith("/usage-queue") for _, path, *_ in calls), 0)
        self.assertGreaterEqual(len([method for method, path, *_ in calls if path.endswith("/api-call")]), 1)

    def test_custom_quota_request_is_substituted_and_upstream_error_is_reported(self):
        seen = {}

        def transport(method, url, headers, body, timeout):
            path = url.split("?", 1)[0]
            if path.endswith("/auth-files"):
                return 200, {}, json.dumps({"files": [{"auth_index": "a", "name": "a.json", "type": "claude"}]}).encode()
            if path.endswith("/api-call"):
                seen["body"] = json.loads(body)
                return 200, {}, json.dumps({"statusCode": 429, "body": "rate limited"}).encode()
            return 200, {}, b"[]"

        client = CPAUsageClient("https://cpa.example", "key", transport=transport)
        snapshot = client.fetch_quota_snapshot(request_configs={"claude": QuotaRequestConfig("GET", "https://example.test/$AUTH_INDEX")})
        self.assertEqual(seen["body"]["url"], "https://example.test/a")
        self.assertEqual(snapshot.accounts[0].error, "upstream quota request returned HTTP 429")

    def test_management_http_error_does_not_leak_token(self):
        def transport(method, url, headers, body, timeout):
            return 401, {}, b'{"error":"unauthorized"}'

        client = CPAUsageClient("https://cpa.example", "secret-token", transport=transport)
        with self.assertRaises(CPAUsageError) as raised:
            client.fetch_auth_files()
        self.assertEqual(raised.exception.status, 401)
        self.assertNotIn("secret-token", str(raised.exception))

    def test_cookie_auth_can_be_configured_without_bearer_spacing(self):
        seen = {}

        def transport(method, url, headers, body, timeout):
            seen.update(headers)
            return 200, {}, b'{"files":[]}'

        client = CPAUsageClient("https://cpa.example", "session-token", token_header="Cookie", token_prefix="cpa_usage_keeper_session=", token_separator="", transport=transport)
        client.fetch_auth_files()
        self.assertEqual(seen["Cookie"], "cpa_usage_keeper_session=session-token")

    def test_all_auth_file_provider_types_are_queried_without_usage_queue(self):
        paths = []

        def transport(method, url, headers, body, timeout):
            path = url.split("?", 1)[0]
            paths.append(path)
            if path.endswith("/auth-files"):
                return 200, {}, b'{"files":[{"name":"codex.json","type":"codex","auth_index":"codex-auth"},{"name":"claude.json","type":"claude","auth_index":"claude-auth"}]}'
            if path.endswith("/api-call"):
                request = json.loads(body)
                if request["authIndex"] == "codex-auth" and request["url"].endswith("/usage"):
                    payload = {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 1}}}
                elif request["authIndex"] == "codex-auth":
                    payload = {"credits": [], "available_count": 0}
                elif request["url"].endswith("/profile"):
                    payload = {"account": {"has_claude_pro": True}}
                else:
                    payload = {"five_hour": {"utilization": 10}}
                return 200, {}, json.dumps({"status_code": 200, "body": json.dumps(payload)}).encode()
            return 404, {}, b'{"error":"unexpected endpoint"}'

        client = CPAUsageClient("https://cpa.example", "key", transport=transport)
        snapshot = client.fetch_quota_snapshot()
        self.assertEqual({account.auth_index for account in snapshot.accounts}, {"codex-auth", "claude-auth"})
        self.assertNotIn("https://cpa.example/v0/management/codex-api-key", paths)
        self.assertNotIn("https://cpa.example/v0/management/claude-api-key", paths)
        self.assertFalse(any(path.endswith("/usage-queue") for path in paths))

    def test_all_cpa_quota_provider_payloads_normalize_to_windows(self):
        base = lambda provider: QuotaAccount("auth", provider, provider, "acc")
        cases = [
            ("codex", {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 10}, "secondary_window": {"used_percent": 20}}}),
            ("claude", {"five_hour": {"utilization": 10, "resets_at": "2026-09-06T00:00:00Z"}, "seven_day": {"utilization": 20}}),
            ("gemini-cli", {"buckets": [{"model_id": "gemini-pro", "remaining_fraction": 0.7, "remaining_amount": 7}]}),
            ("antigravity", {"groups": [{"displayName": "Gemini Models", "buckets": [{"bucketId": "gemini-5h", "remainingFraction": 0.7}]}]}),
            ("kimi", {"usage": {"limit": 100, "remaining": 60, "resetTime": "2026-09-07T00:00:00Z"}}),
        ]
        for provider, payload in cases:
            with self.subTest(provider=provider):
                self.assertTrue(parse_quota_account(base(provider), payload).windows)
        xai = parse_xai_quota(base("xai"), {"config": {"currentPeriod": {"end": "2026-09-07T00:00:00Z"}, "creditUsagePercent": 25}}, {"config": {"monthlyLimit": {"val": 100}, "used": {"val": 10}, "billingPeriodEnd": "2026-10-01T00:00:00Z"}})
        self.assertEqual(len(xai.windows), 2)
        self.assertEqual(xai.windows[0].remaining_percent, 75)
        self.assertEqual(xai.windows[1].remaining_value, 90)
