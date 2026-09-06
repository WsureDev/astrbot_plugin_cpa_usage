"""Safe, reusable asset loading for quota card rendering."""

from __future__ import annotations

import base64
import hashlib
import random
from datetime import date
from pathlib import Path
from typing import Iterable


MAX_ASSET_SIZE = 5 * 1024 * 1024


class AssetError(ValueError):
    """Raised when a configured render asset is unsafe or unusable."""


_MIME_TYPES = {
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".otf": "font/otf",
    ".ttc": "font/collection",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

_IMAGE_SUFFIXES = frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})
_FONT_SUFFIXES = frozenset({".otf", ".ttc", ".ttf", ".woff", ".woff2"})


class AssetResolver:
    """Resolve user and bundled assets without allowing arbitrary file reads."""

    def __init__(self, base_dir: str | Path, plugin_data_dir: str | Path | None = None):
        self.base_dir = Path(base_dir).resolve()
        self.plugin_data_dir = Path(plugin_data_dir or base_dir).resolve()
        self._data_uri_cache: dict[tuple[Path, int, int], str] = {}

    def resolve(self, value: str | Path, *, user_path: bool = False) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = (self.plugin_data_dir if user_path else self.base_dir) / raw
        try:
            path = raw.resolve()
        except (OSError, RuntimeError) as exc:
            raise AssetError(f"无法解析资源路径：{value}") from exc
        if not any(self._inside(path, root) for root in (self.base_dir, self.plugin_data_dir)):
            raise AssetError(f"资源路径不在插件或插件数据目录内：{value}")
        if not path.is_file():
            raise AssetError(f"资源文件不存在：{value}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AssetError(f"无法读取资源文件：{value}") from exc
        if size > MAX_ASSET_SIZE:
            raise AssetError(f"资源文件超过 5 MB：{value}")
        return path

    def data_uri(self, value: str | Path, *, user_path: bool = False, kind: str | None = None) -> str:
        path = self.resolve(value, user_path=user_path)
        suffix = path.suffix.lower()
        allowed = _IMAGE_SUFFIXES if kind == "image" else _FONT_SUFFIXES if kind == "font" else _MIME_TYPES.keys()
        if suffix not in allowed or suffix not in _MIME_TYPES:
            raise AssetError(f"不支持的{kind or '资源'}格式：{path.name}")
        stat = path.stat()
        key = (path, stat.st_mtime_ns, stat.st_size)
        cached = self._data_uri_cache.get(key)
        if cached is not None:
            return cached
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise AssetError(f"无法读取资源文件：{path.name}") from exc
        result = f"data:{_MIME_TYPES[suffix]};base64,{encoded}"
        self._data_uri_cache = {
            cached_key: value
            for cached_key, value in self._data_uri_cache.items()
            if cached_key[0] != path
        }
        if len(self._data_uri_cache) >= 16:
            self._data_uri_cache.pop(next(iter(self._data_uri_cache)))
        self._data_uri_cache[key] = result
        return result

    def choose_background(
        self,
        *,
        user_paths: Iterable[str | Path] = (),
        bundled_dir: str | Path = "templates/res/backgrounds",
        strategy: str = "random",
        day: date | None = None,
        selection_key: str = "",
    ) -> str:
        """Return a background Data URI, preferring valid configured files."""
        user_candidates = self._valid_candidates(user_paths, user_path=True, kind="image")
        candidates = user_candidates or self._bundled_candidates(bundled_dir)
        if not candidates:
            return ""
        normalized = strategy.strip().lower()
        if normalized == "fixed":
            chosen = candidates[0]
        elif normalized == "daily":
            marker = (day or date.today()).isoformat()
            digest = hashlib.sha256(f"{marker}:{selection_key}".encode("utf-8")).digest()
            chosen = candidates[int.from_bytes(digest[:8], "big") % len(candidates)]
        elif normalized == "random":
            chosen = random.choice(candidates)
        else:
            raise AssetError(f"不支持的背景选择策略：{strategy}")
        return self.data_uri(chosen, kind="image")

    def font_data_uri(self, font_path: str | Path | None = None) -> str:
        if font_path:
            try:
                return self.data_uri(font_path, user_path=True, kind="font")
            except AssetError:
                pass
        bundled = self.base_dir / "templates/res/fonts/CPAQuotaSans-Regular.woff2"
        return self.data_uri(bundled, kind="font")

    def _valid_candidates(self, values: Iterable[str | Path], *, user_path: bool, kind: str) -> list[Path]:
        result: list[Path] = []
        for value in values:
            if not str(value).strip():
                continue
            try:
                path = self.resolve(value, user_path=user_path)
                suffix_set = _IMAGE_SUFFIXES if kind == "image" else _FONT_SUFFIXES
                if path.suffix.lower() in suffix_set:
                    result.append(path)
            except AssetError:
                continue
        return result

    def _bundled_candidates(self, directory: str | Path) -> list[Path]:
        path = (self.base_dir / directory).resolve()
        if not self._inside(path, self.base_dir) or not path.is_dir():
            return []
        return sorted(
            item
            for item in path.iterdir()
            if item.is_file()
            and item.suffix.lower() in _IMAGE_SUFFIXES
            and item.stat().st_size <= MAX_ASSET_SIZE
        )

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
