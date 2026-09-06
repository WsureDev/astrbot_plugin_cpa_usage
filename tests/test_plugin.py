import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def load_plugin_module():
    package = types.ModuleType("astrbot_plugin_cpa_usage")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)

    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.filter = types.SimpleNamespace(
        command=lambda *args, **kwargs: (lambda function: function)
    )

    class Star:
        def __init__(self, context):
            self.context = context

        async def html_render(self, *_args, **_kwargs):
            return "rendered://quota.png"

    class StarTools:
        @staticmethod
        def get_data_dir(_name):
            return Path("/tmp/astrbot_plugin_cpa_usage_tests")

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = object
    star_module.Star = Star
    star_module.StarTools = StarTools
    star_module.register = lambda *args, **kwargs: (lambda cls: cls)

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module

    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_cpa_usage.main", ROOT / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = load_plugin_module()


class FakeProvider:
    def __init__(self, provider_id, source_id=""):
        self.provider_config = {
            "id": provider_id,
            "provider_source_id": source_id,
        }


class FakeProviderManager:
    def __init__(self, configs=None):
        self.configs = configs or {}

    def get_provider_config_by_id(self, provider_id):
        return self.configs.get(provider_id)


class FakeContext:
    def __init__(self, provider_id, providers=None, saved_configs=None):
        self.provider_id = provider_id
        self.providers = providers or {}
        self.provider_manager = FakeProviderManager(saved_configs)

    async def get_current_chat_provider_id(self, *, umo):
        return self.provider_id

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)


class FakeEvent:
    unified_msg_origin = "platform:group:1"

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, path):
        return ("image", path)


async def collect(generator):
    return [item async for item in generator]


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


async def rendered_url(*args, **kwargs):
    return "rendered://quota.png"


class PluginTriggerTests(unittest.TestCase):
    def test_matching_current_provider_sends_png(self):
        plugin = PLUGIN.CPAUsagePlugin(
            FakeContext("N5-CPA"),
            {"cpa_providers": [{"provider_id": "N5-CPA", "base_url": "http://cpa", "token": "secret"}]},
        )
        plugin._fetch_snapshot = lambda provider: object()
        plugin._renderer_for = lambda provider: types.SimpleNamespace(render_url=rendered_url)
        with patch.object(PLUGIN.asyncio, "to_thread", immediate_to_thread):
            results = asyncio.run(collect(plugin.show_quota(FakeEvent())))
        self.assertEqual(results, [("image", "rendered://quota.png")])

    def test_matching_provider_passes_current_id_to_renderer(self):
        plugin = PLUGIN.CPAUsagePlugin(
            FakeContext("N5-CPA"),
            {
                "title": "CPA Usages",
                "cpa_providers": [
                    {"provider_id": "N5-CPA", "base_url": "http://cpa", "token": "secret"}
                ],
            },
        )
        plugin._fetch_snapshot = lambda provider: object()
        calls = []

        async def capture_url(*args, **kwargs):
            calls.append((args, kwargs))
            return "rendered://quota.png"

        plugin._renderer_for = lambda provider: types.SimpleNamespace(render_url=capture_url)
        with patch.object(PLUGIN.asyncio, "to_thread", immediate_to_thread):
            results = asyncio.run(collect(plugin.show_quota(FakeEvent())))
        self.assertEqual(results, [("image", "rendered://quota.png")])
        self.assertEqual(calls[0][1]["title"], "CPA Usages")
        self.assertEqual(calls[0][1]["selection_key"], "N5-CPA")

    def test_query_failure_returns_plain_result_without_token(self):
        plugin = PLUGIN.CPAUsagePlugin(
            FakeContext("N5-CPA"),
            {
                "cpa_providers": [
                    {"provider_id": "N5-CPA", "base_url": "http://cpa", "token": "secret-token"}
                ],
            },
        )
        plugin._fetch_snapshot = lambda provider: (_ for _ in ()).throw(RuntimeError("CPA request failed"))
        with patch.object(PLUGIN.asyncio, "to_thread", immediate_to_thread):
            results = asyncio.run(collect(plugin.show_quota(FakeEvent())))
        self.assertEqual(results[0][0], "plain")
        self.assertIn("查询失败", results[0][1])
        self.assertNotIn("secret-token", results[0][1])

    def test_unmatched_provider_does_not_query(self):
        plugin = PLUGIN.CPAUsagePlugin(
            FakeContext("other"),
            {"cpa_providers": [{"provider_id": "N5-CPA", "base_url": "http://cpa", "token": "secret"}]},
        )
        plugin._fetch_snapshot = lambda provider: self.fail("CPA must not be queried")
        results = asyncio.run(collect(plugin.show_quota(FakeEvent())))
        self.assertEqual(results[0][0], "plain")
        self.assertIn("未配置", results[0][1])

    def test_models_from_same_provider_source_share_one_binding(self):
        anchor_id = "N5-CPA/gpt-5"
        current_id = "N5-CPA/gpt-5-mini"
        context = FakeContext(
            current_id,
            providers={
                anchor_id: FakeProvider(anchor_id, "N5-CPA"),
                current_id: FakeProvider(current_id, "N5-CPA"),
            },
        )
        plugin = PLUGIN.CPAUsagePlugin(
            context,
            {
                "cpa_providers": [
                    {"provider_id": anchor_id, "base_url": "http://cpa", "token": "secret"}
                ]
            },
        )
        plugin._fetch_snapshot = lambda provider: object()
        plugin._renderer_for = lambda provider: types.SimpleNamespace(render_url=rendered_url)
        with patch.object(PLUGIN.asyncio, "to_thread", immediate_to_thread):
            results = asyncio.run(collect(plugin.show_quota(FakeEvent())))
        self.assertEqual(results, [("image", "rendered://quota.png")])

    def test_models_from_different_provider_sources_do_not_match(self):
        anchor_id = "CPA-A/gpt-5"
        current_id = "CPA-B/gpt-5"
        context = FakeContext(
            current_id,
            providers={
                anchor_id: FakeProvider(anchor_id, "CPA-A"),
                current_id: FakeProvider(current_id, "CPA-B"),
            },
        )
        plugin = PLUGIN.CPAUsagePlugin(
            context,
            {
                "cpa_providers": [
                    {"provider_id": anchor_id, "base_url": "http://cpa", "token": "secret"}
                ]
            },
        )
        plugin._fetch_snapshot = lambda provider: self.fail("CPA must not be queried")
        results = asyncio.run(collect(plugin.show_quota(FakeEvent())))
        self.assertEqual(results[0][0], "plain")

    def test_disabled_anchor_resolves_source_from_saved_provider_config(self):
        anchor_id = "N5-CPA/disabled-model"
        current_id = "N5-CPA/gpt-5"
        context = FakeContext(
            current_id,
            providers={current_id: FakeProvider(current_id, "N5-CPA")},
            saved_configs={
                anchor_id: {"id": anchor_id, "provider_source_id": "N5-CPA"}
            },
        )
        plugin = PLUGIN.CPAUsagePlugin(
            context,
            {
                "cpa_providers": [
                    {"provider_id": anchor_id, "base_url": "http://cpa", "token": "secret"}
                ]
            },
        )
        self.assertIsNotNone(plugin._match_provider(current_id))

    def test_schema_uses_public_model_selector_as_source_anchor(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        field = schema["cpa_providers"]["templates"]["provider"]["items"]["provider_id"]
        self.assertEqual(field["type"], "string")
        self.assertEqual(field["_special"], "select_provider")
