"""HTML/CSS quota card renderer backed by independent template files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .assets import AssetError, AssetResolver
from .quota_models import QuotaAccount, QuotaSnapshot, QuotaWindow


GMT8 = timezone(timedelta(hours=8))
RENDER_OPTIONS = {"full_page": True, "type": "png", "scale": "device"}
HtmlRenderCallable = Callable[..., Awaitable[str]]


def _fmt_time(value: datetime | None) -> str:
    return "--" if value is None else value.astimezone(GMT8).strftime("%m/%d %H:%M")


def _relative(value: datetime | None, now: datetime) -> str:
    if value is None:
        return ""
    seconds = int((value - now).total_seconds())
    if seconds < 0:
        return "已过期"
    if seconds >= 86400:
        return f"{seconds // 86400}天后"
    if seconds >= 3600:
        return f"{seconds // 3600}小时后"
    return f"{max(seconds // 60, 1)}分钟后"


def _percent(window: QuotaWindow) -> float | None:
    if window.remaining_percent is not None:
        return max(0.0, min(100.0, window.remaining_percent))
    if window.remaining_value is not None and window.limit_value and window.limit_value > 0:
        return max(0.0, min(100.0, 100.0 * window.remaining_value / window.limit_value))
    return None


def _format_number(value: float) -> str:
    return f"{value:g}"


def _safe_json(value: Mapping[str, Any]) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def find_chromium(explicit: str | Path | None = None) -> Path | None:
    """Locate a Chromium executable for the standalone render command."""
    candidates: list[str | Path] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("CPA_CHROMIUM_PATH"):
        candidates.append(os.environ["CPA_CHROMIUM_PATH"])
    candidates.extend(
        (
            "/mnt/storage/.cache/ms-playwright/chromium-1237/chrome-linux64/chrome",
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        )
    )
    for candidate in candidates:
        raw = str(candidate)
        located_text = raw if os.path.sep in raw else (shutil.which(raw) or "")
        if not located_text:
            continue
        located = Path(located_text).expanduser()
        if located.is_file() and os.access(located, os.X_OK):
            return located.resolve()
    return None


def find_node(explicit: str | Path | None = None) -> Path | None:
    candidates = [explicit, os.environ.get("CPA_NODE_PATH"), shutil.which("node")]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    return None


class QuotaCardRenderer:
    """Transform quota models into one self-contained HTML card."""

    def __init__(
        self,
        *,
        base_dir: str | Path | None = None,
        plugin_data_dir: str | Path | None = None,
        font_path: str | Path | None = None,
        background_images: list[str] | tuple[str, ...] | None = None,
        background_strategy: str = "random",
        background_overlay: float = 0.66,
        background_position: str = "center",
        chromium_path: str | Path | None = None,
        node_path: str | Path | None = None,
    ) -> None:
        self.base_dir = Path(base_dir or Path(__file__).parent.parent).resolve()
        self.template_dir = self.base_dir / "templates"
        self.plugin_data_dir = Path(plugin_data_dir or self.base_dir).resolve()
        self.font_path = font_path
        self.background_images = list(background_images or [])
        self.background_strategy = background_strategy
        self.background_overlay = max(0.0, min(1.0, float(background_overlay)))
        self.background_position = self._safe_background_position(background_position)
        self.chromium_path = chromium_path
        self.node_path = node_path
        self.assets = AssetResolver(self.base_dir, self.plugin_data_dir)

    def payload(
        self,
        snapshot: QuotaSnapshot,
        *,
        title: str = "CPA Usages",
        background_uri: str = "",
    ) -> dict[str, Any]:
        now = snapshot.fetched_at.astimezone(timezone.utc)
        return {
            "title": str(title),
            "fetched_at": _fmt_time(now),
            "background_uri": background_uri,
            "background_overlay": self.background_overlay,
            "background_position": self.background_position,
            "accounts": [self._account_payload(account, now) for account in snapshot.accounts],
        }

    def render(
        self,
        snapshot: QuotaSnapshot,
        *,
        title: str = "CPA Usages",
        selection_key: str = "",
    ) -> str:
        """Build self-contained HTML; all visual markup remains in templates/."""
        template = (self.template_dir / "quota_card.html").read_text(encoding="utf-8")
        css = (self.template_dir / "res/css/quota_card.css").read_text(encoding="utf-8")
        font_uris = {
            "__FONT_TITLE_URI__": self._bundled_font_uri("baotu.woff2"),
            "__FONT_DISPLAY_URI__": self._bundled_font_uri("ADLaM-Display-Regular.ttf"),
            "__FONT_DATA_URI__": self._bundled_font_uri("Spicy-Rice-Regular.ttf"),
            "__FONT_FOOTER_URI__": self._bundled_font_uri("DingTalk-JinBuTi.ttf"),
            "__FONT_BODY_URI__": self.assets.font_data_uri(self.font_path),
        }
        for marker, uri in font_uris.items():
            css = css.replace(marker, uri)
        background_uri = self.assets.choose_background(
            user_paths=self.background_images,
            strategy=self.background_strategy,
            day=snapshot.fetched_at.astimezone(GMT8).date(),
            selection_key=selection_key,
        )
        payload = self.payload(snapshot, title=title, background_uri=background_uri)
        return template.replace("__CSS_STYLE__", css).replace("__PAYLOAD_JSON__", _safe_json(payload))

    def write_html(
        self,
        snapshot: QuotaSnapshot,
        path: str | Path,
        *,
        title: str = "CPA Usages",
        selection_key: str = "",
    ) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            self.render(snapshot, title=title, selection_key=selection_key),
            encoding="utf-8",
        )
        return destination

    async def render_url(
        self,
        snapshot: QuotaSnapshot,
        html_render: HtmlRenderCallable,
        *,
        title: str = "CPA Usages",
        selection_key: str = "",
    ) -> str:
        html_content = self.render(snapshot, title=title, selection_key=selection_key)
        return await html_render(
            html_content,
            {},
            return_url=True,
            options=dict(RENDER_OPTIONS),
        )

    def write_png(
        self,
        snapshot: QuotaSnapshot,
        path: str | Path,
        *,
        title: str = "CPA Usages",
        selection_key: str = "",
        html_path: str | Path | None = None,
        chromium_path: str | Path | None = None,
    ) -> Path:
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        executable = find_chromium(chromium_path or self.chromium_path)
        if executable is None:
            raise RuntimeError("未找到 Chromium；请通过 --chromium-path 或 CPA_CHROMIUM_PATH 指定")
        node = find_node(self.node_path)
        if node is None:
            raise RuntimeError("未找到 Node.js；手动 PNG 渲染需要 Node.js 20+")

        temporary_html: Path | None = None
        if html_path is None:
            handle = tempfile.NamedTemporaryFile(prefix="cpa-quota-", suffix=".html", delete=False)
            handle.close()
            page = Path(handle.name)
            temporary_html = page
        else:
            page = Path(html_path).resolve()
        self.write_html(snapshot, page, title=title, selection_key=selection_key)

        try:
            helper = self.base_dir / "scripts/render_html.mjs"
            self._run_browser_helper(node, helper, executable, page, destination)
        finally:
            if temporary_html is not None:
                temporary_html.unlink(missing_ok=True)
        if not destination.is_file() or destination.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError("Chromium 未生成有效 PNG")
        return destination

    @staticmethod
    def _run_browser_helper(
        node: Path,
        helper: Path,
        chromium: Path,
        page: Path,
        destination: Path,
    ) -> None:
        command = [
            str(node),
            str(helper),
            "--html",
            str(page),
            "--output",
            str(destination),
            "--chromium",
            str(chromium),
            "--width",
            "500",
            "--timeout",
            "30000",
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"手动渲染器不存在：{command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Playwright 生成 PNG 超时") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "unknown error").strip().splitlines()[-1]
            raise RuntimeError(f"Playwright 生成 PNG 失败：{detail}") from exc

    def _account_payload(self, account: QuotaAccount, now: datetime) -> dict[str, Any]:
        plan = account.plan or ""
        plan = {
            "plus": "Plus",
            "pro": "Pro",
            "free": "Free",
            "team": "Team",
            "enterprise": "Enterprise",
        }.get(plan.lower(), plan)
        resets = [
            self._time_with_relative(credit.expires_at, now)
            for credit in account.reset_credit_expiries
        ]
        return {
            "icon": (account.provider_type or account.provider_name or "?")[:1].upper(),
            "provider_icon": self._provider_icon_uri(account.provider_type),
            "provider_icon_theme": account.provider_type.strip().lower().replace("_", "-"),
            "provider_type": account.provider_type.upper(),
            "provider_name": account.provider_name,
            "account_display": account.account_display,
            "plan": plan,
            "renew_at": _fmt_time(account.renew_at) if account.renew_at else "",
            "renew_relative": _relative(account.renew_at, now) if account.renew_at else "",
            "reset_count_text": str(account.reset_credit_count) if account.reset_credit_count is not None else "",
            "reset_credits": resets,
            "windows": [self._window_payload(window, now) for window in account.windows],
            "error": account.error or "",
        }

    def _window_payload(self, window: QuotaWindow, now: datetime) -> dict[str, Any]:
        percent = _percent(window)
        value = f"{percent:.0f}%" if percent is not None else "--"
        remaining = ""
        if window.remaining_value is not None:
            remaining = _format_number(window.remaining_value)
            if window.limit_value is not None:
                remaining += f" / {_format_number(window.limit_value)}"
        reset_at = window.reset_at
        if reset_at is None and window.reset_after_seconds is not None and window.reset_after_seconds >= 0:
            reset_at = now + timedelta(seconds=window.reset_after_seconds)
        reset_text = self._time_with_relative(reset_at, now) if reset_at else ""
        tone = "good" if percent is None or percent >= 50 else "warn" if percent >= 20 else "bad"
        return {
            "label": window.label,
            "value": value,
            "percent": percent,
            "tone": tone,
            "meta": " · ".join(item for item in (remaining, reset_text) if item),
        }

    @staticmethod
    def _time_with_relative(value: datetime | None, now: datetime) -> str:
        if value is None:
            return "--"
        relative = _relative(value, now)
        return f"{_fmt_time(value)} · {relative}" if relative else _fmt_time(value)

    def _bundled_font_uri(self, filename: str) -> str:
        path = self.template_dir / "res/fonts" / filename
        return self.assets.data_uri(path, kind="font")

    def _provider_icon_uri(self, provider_type: str) -> str:
        normalized = provider_type.strip().lower().replace("_", "-")
        filename = {
            "gemini": "gemini-cli.svg",
            "gemini-cli": "gemini-cli.svg",
            "grok": "xai.svg",
            "xai": "xai.svg",
        }.get(normalized, f"{normalized}.svg")
        path = self.template_dir / "res/icons" / filename
        if not path.is_file():
            path = self.template_dir / "res/icons/default.svg"
        return self.assets.data_uri(path, kind="image")

    @staticmethod
    def _safe_background_position(value: str) -> str:
        normalized = str(value or "center").strip().lower()
        allowed = {
            "center",
            "top",
            "bottom",
            "left",
            "right",
            "left top",
            "left center",
            "left bottom",
            "center top",
            "center center",
            "center bottom",
            "right top",
            "right center",
            "right bottom",
        }
        return normalized if normalized in allowed else "center"


__all__ = ["AssetError", "QuotaCardRenderer", "RENDER_OPTIONS", "find_chromium", "find_node"]
