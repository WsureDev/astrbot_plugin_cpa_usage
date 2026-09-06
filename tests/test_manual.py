import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import run_manual_test
from cpa_usage.quota_models import QuotaSnapshot


class FakeClient:
    base_url = "http://cpa"
    management_prefix = "/v0/management"

    def __init__(self):
        self.calls = []

    def fetch_quota_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return QuotaSnapshot([], datetime.now(timezone.utc))


class FakeRenderer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def write(self, snapshot, path, **kwargs):
        Path(path).write_text("svg", encoding="utf-8")
        return Path(path)

    def write_png(self, snapshot, path, **kwargs):
        Path(path).write_bytes(b"png")
        return Path(path)


class ManualEntryTests(unittest.TestCase):
    def test_env_to_api_to_renderer_trigger_path(self):
        fake_client = FakeClient()
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "CPA_PROVIDERS_JSON='" + json.dumps([
                    {"provider_id": "N5-CPA", "base_url": "http://cpa", "token": "secret"}
                ]) + "'\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                env=str(env_path), provider_id="N5-CPA", provider_type="codex",
                include_disabled=False, output_dir=directory, font_path="", title="CPA Usages",
            )
            with patch.object(run_manual_test, "client_from_config", return_value=fake_client), patch.object(
                run_manual_test, "QuotaCardRenderer", FakeRenderer
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(run_manual_test.run(args), 0)
            self.assertEqual(fake_client.calls, [{"provider_type": "codex", "include_disabled": False}])
            self.assertEqual((Path(directory) / "cpa-quota-live.png").read_bytes(), b"png")
