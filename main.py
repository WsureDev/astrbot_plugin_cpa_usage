"""AstrBot adapter for the read-only CPA capacity card."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .cpa_usage import QuotaCardRenderer, as_bool, client_from_config


PLUGIN_ID = "astrbot_plugin_cpa_usage"


@register(PLUGIN_ID, "WsureDev", "只读查询 CPA Provider 配额并生成容量卡片", "0.3.0")
class CPAUsagePlugin(Star):
    """Match the current AstrBot provider and send its CPA quota card."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        self.base_dir = Path(__file__).parent.resolve()
        self.plugin_data_dir = Path(StarTools.get_data_dir(getattr(self, "name", PLUGIN_ID))).resolve()
        self._provider_locks: dict[str, asyncio.Lock] = {}

    @filter.command("cpausage")
    async def show_quota(self, event: AstrMessageEvent):
        """查询当前 provider 对应的 CPA 配额容量卡片。"""
        try:
            current_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
        except Exception as exc:
            logger.warning("CPA quota: unable to resolve current provider: %s", exc)
            yield event.plain_result("无法获取当前会话的模型 Provider。")
            return
        provider = self._match_provider(str(current_id or "").strip())
        if provider is None:
            yield event.plain_result("当前模型 Provider 未配置 CPA 配额查询。")
            return
        provider_key = str(provider.get("provider_id") or current_id)
        lock = self._provider_locks.setdefault(provider_key, asyncio.Lock())
        try:
            async with lock:
                snapshot = await asyncio.to_thread(self._fetch_snapshot, provider)
                renderer = self._renderer_for(provider)
                title = str(provider.get("title") or self.config.get("title") or "CPA Usages")
                image_url = await renderer.render_url(
                    snapshot,
                    self.html_render,
                    title=title,
                    selection_key=str(current_id),
                )
        except Exception as exc:
            logger.warning("CPA quota card failed for provider %s: %s", current_id, exc)
            yield event.plain_result(f"CPA 配额查询失败：{exc}")
            return
        yield event.image_result(str(image_url))

    def _match_provider(self, current_id: str) -> Mapping[str, Any] | None:
        providers = [
            raw
            for raw in self.config.get("cpa_providers", []) or []
            if isinstance(raw, Mapping)
        ]
        current_source_id = self._provider_source_id(current_id)
        for provider in providers:
            try:
                if not as_bool(provider.get("enabled"), True):
                    continue
            except ValueError:
                continue
            anchor_id = str(
                provider.get("provider_id") or provider.get("id") or ""
            ).strip()
            if not anchor_id:
                continue
            if self._provider_source_id(anchor_id) == current_source_id:
                return provider
        return None

    def _provider_source_id(self, provider_id: str) -> str:
        """Resolve a model Provider ID to its shared Provider Source ID."""
        provider_id = provider_id.strip()
        if not provider_id:
            return ""

        instance = None
        get_provider = getattr(self.context, "get_provider_by_id", None)
        if callable(get_provider):
            instance = get_provider(provider_id)
        config = getattr(instance, "provider_config", None)
        source_id = self._source_id_from_config(config)
        if source_id:
            return source_id

        # Disabled model providers are not instantiated, so resolve their
        # saved source through the manager configuration when available.
        manager = getattr(self.context, "provider_manager", None)
        get_config = getattr(manager, "get_provider_config_by_id", None)
        if callable(get_config):
            try:
                config = get_config(provider_id)
            except (KeyError, TypeError, ValueError):
                config = None
            source_id = self._source_id_from_config(config)
            if source_id:
                return source_id
        # Legacy AstrBot providers had no Provider Source layer.
        return provider_id

    @staticmethod
    def _source_id_from_config(config: Any) -> str:
        if not isinstance(config, Mapping):
            return ""
        return str(config.get("provider_source_id") or "").strip()

    def _fetch_snapshot(self, provider: Mapping[str, Any]):
        client = client_from_config(
            provider,
            default_timeout=float(self.config.get("timeout", 20)),
            default_tls_verify=as_bool(self.config.get("tls_verify"), True),
        )
        return client.fetch_quota_snapshot(
            provider_type=str(provider.get("provider_type") or "").strip() or None,
            include_disabled=as_bool(provider.get("include_disabled", self.config.get("include_disabled")), False),
        )

    def _renderer_for(self, provider: Mapping[str, Any]) -> QuotaCardRenderer:
        background_images = provider.get("background_images", self.config.get("background_images", []))
        if isinstance(background_images, str):
            background_images = [background_images]
        elif not isinstance(background_images, (list, tuple)):
            background_images = []
        font_file = provider.get("font_file") or self.config.get("font_file") or ""
        if isinstance(font_file, (list, tuple)):
            font_file = next((item for item in font_file if str(item).strip()), "")
        overlay_value = provider.get(
            "background_overlay",
            self.config.get("background_overlay", 0.66),
        )
        if overlay_value is None or overlay_value == "":
            overlay_value = 0.66
        return QuotaCardRenderer(
            base_dir=self.base_dir,
            plugin_data_dir=self.plugin_data_dir,
            font_path=str(font_file).strip() or None,
            background_images=[str(item) for item in background_images if str(item).strip()],
            background_strategy=str(
                provider.get("background_strategy")
                or self.config.get("background_strategy")
                or "random"
            ),
            background_overlay=float(overlay_value),
            background_position=str(
                provider.get("background_position")
                or self.config.get("background_position")
                or "center top"
            ),
        )
