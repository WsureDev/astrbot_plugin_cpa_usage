import asyncio
import os
import struct
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from cpa_usage.assets import AssetError, AssetResolver, MAX_ASSET_SIZE
from cpa_usage.quota_models import QuotaAccount, QuotaSnapshot, QuotaWindow, ResetCredit, mask_account
from cpa_usage.quota_render import QuotaCardRenderer, RENDER_OPTIONS, find_chromium


ROOT = Path(__file__).resolve().parent.parent


class RenderTests(unittest.TestCase):
    def snapshot(self):
        account = QuotaAccount(
            auth_index="auth-secret-index",
            provider_type="codex",
            provider_name="OpenAI",
            account_display=mask_account("user@example.com"),
            plan="Plus",
            renew_at=datetime(2026, 9, 21, 2, 50, tzinfo=timezone.utc),
            reset_credit_count=2,
            reset_credit_expiries=[
                ResetCredit(datetime(2026, 10, 4, 1, 3, tzinfo=timezone.utc)),
                ResetCredit(datetime(2026, 10, 5, 22, 40, tzinfo=timezone.utc)),
            ],
            windows=[
                QuotaWindow("5h", "5 小时限额", remaining_percent=100, reset_at=datetime(2026, 9, 6, 1, 54, tzinfo=timezone.utc)),
                QuotaWindow("7d", "周限额", remaining_percent=42, reset_at=datetime(2026, 9, 7, 9, 2, tzinfo=timezone.utc)),
            ],
            account_id="upstream-secret-account-id",
        )
        return QuotaSnapshot([account], datetime(2026, 9, 6, 1, 5, tzinfo=timezone.utc))

    def test_account_mask_keeps_only_edges(self):
        self.assertEqual(mask_account("user@example.com"), "use*********.com")
        self.assertNotIn("user@exam", mask_account("user@example.com"))

    def test_html_uses_external_template_css_and_inlined_assets(self):
        rendered = QuotaCardRenderer(background_strategy="fixed").render(self.snapshot(), title="CPA Usages")
        self.assertIn("<!DOCTYPE html>", rendered)
        self.assertIn("@font-face", rendered)
        self.assertIn("data:font/woff2;base64,", rendered)
        self.assertIn("data:font/ttf;base64,", rendered)
        self.assertIn("data:image/", rendered)
        self.assertIn("data:image/svg+xml;base64,", rendered)
        self.assertIn("CPA Usages", rendered)
        self.assertIn("Queried at:", rendered)
        self.assertIn("AUTH FILE", rendered)
        self.assertIn("5 小时限额", rendered)
        self.assertIn("42%", rendered)
        self.assertIn("use*********.com", rendered)
        self.assertNotIn("user@example.com", rendered)
        self.assertNotIn("auth-secret-index", rendered)
        self.assertNotIn("upstream-secret-account-id", rendered)

    def test_provider_icons_and_multiple_auth_files_are_in_payload(self):
        first = self.snapshot().accounts[0]
        second = QuotaAccount(
            auth_index="auth-2",
            provider_type="claude",
            provider_name="Anthropic",
            account_display=mask_account("claude.user@example.com"),
            plan="Pro",
            windows=[QuotaWindow("5h", "5 小时限额", remaining_percent=76)],
        )
        snapshot = QuotaSnapshot([first, second], self.snapshot().fetched_at)
        payload = QuotaCardRenderer(background_strategy="fixed").payload(snapshot, title="CPA Usages")
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertTrue(payload["accounts"][0]["provider_icon"].startswith("data:image/svg+xml;base64,"))
        self.assertTrue(payload["accounts"][1]["provider_icon"].startswith("data:image/svg+xml;base64,"))
        self.assertEqual(payload["accounts"][1]["renew_at"], "")
        self.assertNotIn("claude.user@example.com", repr(payload))

    def test_render_url_uses_astrbot_full_page_png_contract(self):
        calls = []

        async def fake_html_render(*args, **kwargs):
            calls.append((args, kwargs))
            return "rendered://quota.png"

        result = asyncio.run(QuotaCardRenderer().render_url(self.snapshot(), fake_html_render))
        self.assertEqual(result, "rendered://quota.png")
        self.assertEqual(calls[0][0][1], {})
        self.assertEqual(calls[0][1], {"return_url": True, "options": RENDER_OPTIONS})

    @unittest.skipUnless(os.environ.get("CPA_RUN_CHROMIUM_TESTS") == "1" and find_chromium(), "set CPA_RUN_CHROMIUM_TESTS=1 for Chromium integration")
    def test_write_png_creates_png_file(self):
        with TemporaryDirectory() as directory:
            html_path = Path(directory) / "quota.html"
            path = QuotaCardRenderer().write_png(self.snapshot(), Path(directory) / "quota.png", html_path=html_path)
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            width, height = struct.unpack(">II", path.read_bytes()[16:24])
            self.assertEqual(width, 500)
            self.assertGreater(height, 700)
            self.assertLess(height, 1000)
            self.assertIn("<!DOCTYPE html>", html_path.read_text(encoding="utf-8"))


class AssetTests(unittest.TestCase):
    def test_path_traversal_is_rejected(self):
        with TemporaryDirectory() as directory:
            resolver = AssetResolver(ROOT, directory)
            with self.assertRaises(AssetError):
                resolver.data_uri("/etc/passwd")

    def test_oversized_asset_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "large.png"
            with path.open("wb") as handle:
                handle.truncate(MAX_ASSET_SIZE + 1)
            resolver = AssetResolver(ROOT, directory)
            with self.assertRaises(AssetError):
                resolver.data_uri(path, user_path=True, kind="image")

    def test_user_background_precedes_fallback_and_daily_is_stable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "custom.svg"
            path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
            resolver = AssetResolver(ROOT, directory)
            first = resolver.choose_background(user_paths=[path], strategy="daily", day=date(2026, 9, 6), selection_key="provider")
            second = resolver.choose_background(user_paths=[path], strategy="daily", day=date(2026, 9, 6), selection_key="provider")
            self.assertEqual(first, second)
            self.assertIn("PHN2Zy", first)
            self.assertEqual(len(resolver._data_uri_cache), 1)

    def test_invalid_user_background_falls_back_to_bundled(self):
        resolver = AssetResolver(ROOT, ROOT)
        result = resolver.choose_background(user_paths=["../../etc/passwd"], strategy="fixed")
        self.assertTrue(result.startswith("data:image/"))


if __name__ == "__main__":
    unittest.main()
