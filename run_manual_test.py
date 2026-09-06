#!/usr/bin/env python3
"""Manual realtime API + PNG rendering entrypoint.

This is deliberately a standalone diagnostic command, not an AstrBot plugin
entrypoint. It reads the repository ``.env`` without python-dotenv, selects one
configured provider, fetches a fresh snapshot, and writes a card under /tmp.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from cpa_usage import CPAUsageError, QuotaCardRenderer, client_from_config, find_provider_config


ROOT = Path(__file__).resolve().parent
DEFAULT_ENV = ROOT / ".env"
DEFAULT_OUTPUT_DIR = Path("/tmp")


def load_env(path: str | Path = DEFAULT_ENV) -> dict[str, str]:
    """Load simple KEY=VALUE dotenv entries, preserving quoted JSON values."""
    values: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        return values
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"invalid .env line {line_number}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _env_value(values: Mapping[str, str], key: str, default: str = "") -> str:
    return values.get(key, os.environ.get(key, default)).strip()


def _provider_list(values: Mapping[str, str]) -> list[Mapping[str, Any]]:
    raw = _env_value(values, "CPA_PROVIDERS_JSON")
    if not raw:
        raise ValueError("CPA_PROVIDERS_JSON is empty")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CPA_PROVIDERS_JSON is invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("CPA_PROVIDERS_JSON must be a non-empty JSON array")
    providers = [item for item in parsed if isinstance(item, Mapping)]
    if len(providers) != len(parsed):
        raise ValueError("every CPA provider entry must be a JSON object")
    return providers


def _select_provider(providers: list[Mapping[str, Any]], provider_id: str) -> Mapping[str, Any]:
    if provider_id:
        provider = find_provider_config(providers, provider_id)
        if provider is not None:
            return provider
        configured = ", ".join(str(item.get("provider_id", "")).strip() or "<unnamed>" for item in providers)
        raise ValueError(f"provider_id {provider_id!r} was not found; configured: {configured}")
    return providers[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch read-only CPA quota and render a PNG card")
    parser.add_argument("--env", default=str(DEFAULT_ENV), help="dotenv file (default: repository .env)")
    parser.add_argument("--provider-id", default="", help="configured provider_id; defaults to the first entry")
    parser.add_argument("--provider-type", default="", help="only query one auth-file provider type, e.g. codex or claude")
    parser.add_argument("--include-disabled", action="store_true", help="include disabled auth files")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="output directory (default: /tmp)")
    parser.add_argument("--asset-dir", default="", help="allowed directory for custom font/background files")
    parser.add_argument("--font-path", default="", help="custom font inside the repository or --asset-dir")
    parser.add_argument("--background-image", action="append", default=[], help="custom background; repeat for multiple files")
    parser.add_argument("--background-strategy", choices=("random", "daily", "fixed"), default="", help="background selection strategy")
    parser.add_argument("--background-overlay", type=float, default=None, help="hero dark overlay, 0.0 to 1.0")
    parser.add_argument("--background-position", default="", help="CSS background position (default: center top)")
    parser.add_argument("--chromium-path", default="", help="Chromium executable; defaults to CPA_CHROMIUM_PATH/autodetection")
    parser.add_argument("--node-path", default="", help="Node.js executable; defaults to CPA_NODE_PATH/autodetection")
    parser.add_argument("--title", default="CPA Usages", help="card title")
    return parser


def run(args: argparse.Namespace) -> int:
    values = load_env(args.env)
    providers = _provider_list(values)
    requested_id = args.provider_id.strip() or _env_value(values, "CPA_TEST_PROVIDER_ID")
    provider = _select_provider(providers, requested_id)
    provider_id = str(provider.get("provider_id", "")).strip() or "<unnamed>"
    timeout = float(provider.get("timeout", _env_value(values, "CPA_USAGE_TIMEOUT", "20")) or 20)
    tls_raw = provider.get("tls_verify", _env_value(values, "CPA_USAGE_TLS_VERIFY", "true"))
    client = client_from_config(
        provider,
        default_timeout=timeout,
        default_tls_verify=str(tls_raw).strip().lower() not in {"0", "false", "no", "off"},
    )
    print(f"provider: {provider_id}")
    print(f"management: {client.base_url}{client.management_prefix}")
    print("request: read-only quota, usage queue untouched")
    snapshot = client.fetch_quota_snapshot(provider_type=args.provider_type or None, include_disabled=args.include_disabled)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    font_path = getattr(args, "font_path", "").strip() or _env_value(values, "CPA_FONT_PATH")
    asset_dir = getattr(args, "asset_dir", "").strip() or _env_value(values, "CPA_ASSET_DIR") or str(ROOT)
    background_images = list(getattr(args, "background_image", []) or [])
    if not background_images:
        raw_backgrounds = _env_value(values, "CPA_BACKGROUND_IMAGES_JSON", "[]")
        try:
            parsed_backgrounds = json.loads(raw_backgrounds)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CPA_BACKGROUND_IMAGES_JSON is invalid JSON: {exc.msg}") from exc
        if not isinstance(parsed_backgrounds, list) or not all(isinstance(item, str) for item in parsed_backgrounds):
            raise ValueError("CPA_BACKGROUND_IMAGES_JSON must be a JSON string array")
        background_images = parsed_backgrounds
    overlay_arg = getattr(args, "background_overlay", None)
    overlay = overlay_arg if overlay_arg is not None else float(_env_value(values, "CPA_BACKGROUND_OVERLAY", "0.66"))
    renderer = QuotaCardRenderer(
        base_dir=ROOT,
        plugin_data_dir=asset_dir,
        font_path=font_path or None,
        background_images=background_images,
        background_strategy=getattr(args, "background_strategy", "") or _env_value(values, "CPA_BACKGROUND_STRATEGY", "random"),
        background_overlay=overlay,
        background_position=getattr(args, "background_position", "") or _env_value(values, "CPA_BACKGROUND_POSITION", "center top"),
        chromium_path=getattr(args, "chromium_path", "") or _env_value(values, "CPA_CHROMIUM_PATH") or None,
        node_path=getattr(args, "node_path", "") or _env_value(values, "CPA_NODE_PATH") or None,
    )
    print(f"font: {font_path or 'bundled CPAQuotaSans'}")
    html_path = output_dir / "cpa-quota-live.html"
    png_path = renderer.write_png(
        snapshot,
        output_dir / "cpa-quota-live.png",
        title=args.title,
        selection_key=provider_id,
        html_path=html_path,
    )
    print(f"accounts: {len(snapshot.accounts)}")
    for account in snapshot.accounts:
        print(f"- {account.provider_type}: {account.account_display}; windows={len(account.windows)}; error={account.error or 'none'}")
    print(f"html: {html_path}")
    print(f"png: {png_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (CPAUsageError, RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, CPAUsageError) and exc.status is not None:
            print(f"http_status: {exc.status}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
