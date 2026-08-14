"""匿名 TikTok Explore 批量采集、下载与可选上传。

浏览器仅用于创建一次临时匿名状态并捕获自然 Explore 请求；后续 API 和
媒体流均通过 HTTPX 完成。任何状态均不会写入磁盘。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._batch_config import BatchConfigError, load_batch_config  # noqa: E402
from poc.explore._categories import (  # noqa: E402
    DEFAULT_CATEGORIES_PATH,
    get_category_name,
    list_categories,
    load_category_names,
)
from poc.explore._common import (  # noqa: E402
    DEFAULT_CHUNK_KB,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_RETRY,
    URL_MODES,
)
from poc.explore.diagnose_explore_fields import (  # noqa: E402
    extract_browser_request_params,
    extract_explore_templates,
)
from poc.explore.tiktok_explore_batch import parse_categories  # noqa: E402
from poc.explore.tiktok_explore_poc import (  # noqa: E402
    REQUIRED_BROWSER_FIELDS,
    collect_explore,
    download_media,
    nonnegative_float,
    persist_to_db,
    positive_int,
)
from poc.session.harvest_tiktok_session import check_egress_country  # noqa: E402


EXPLORE_URL = "https://www.tiktok.com/explore"
DEFAULT_OUTPUT_DIR = Path(".output/explore/anonymous")
DEFAULT_CATEGORY_DELAY = 5.0
DEFAULT_MAX_PAGES = 2
DEFAULT_COUNT = 8
DEFAULT_DELAY = 2.0
LOGIN_COOKIE_NAMES = frozenset({"sessionid", "sessionid_ss", "sid_tt"})
CONFIGURABLE_FIELDS = (
    "categories",
    "count",
    "max_pages",
    "delay",
    "category_delay",
    "concurrency",
    "max_retry",
    "chunk_kb",
    "url_mode",
    "download",
    "no_db",
    "output_dir",
    "proxy",
)


class AnonymousValidationError(ValueError):
    """匿名状态或实时证据不足以安全继续。"""


@dataclass
class AnonymousBootstrap:
    """只在当前进程生命周期中保存的匿名浏览器状态。"""

    cookie: dict[str, str]
    user_agent: str
    browser_request_params: dict[str, str]
    templates: tuple[list[tuple[str, str]], list[tuple[str, str]]]


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def reject_login_cookies(cookies: Sequence[Mapping[str, object]]) -> None:
    names = sorted(
        {
            str(cookie.get("name", "")).lower()
            for cookie in cookies
            if str(cookie.get("name", "")).lower() in LOGIN_COOKIE_NAMES
        }
    )
    if names:
        raise AnonymousValidationError(
            "匿名 context 包含登录 Cookie: " + ", ".join(names)
        )


def validate_bootstrap(state: AnonymousBootstrap) -> AnonymousBootstrap:
    missing = [
        field
        for field in REQUIRED_BROWSER_FIELDS
        if not state.browser_request_params.get(field)
    ]
    if missing:
        raise AnonymousValidationError("缺少必填浏览器字段: " + ", ".join(missing))
    if not state.user_agent or not all(state.templates):
        raise AnonymousValidationError("缺少首屏或翻页 Explore 请求模板")
    return state


def _capture_explore_query(request: Any, queries: list[dict[str, str]]) -> None:
    if "/api/explore/item_list/" not in request.url:
        return
    queries.append(dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True)))


async def bootstrap_anonymous(
    *, proxy: str | None, headed: bool, skip_geo: bool
) -> AnonymousBootstrap:
    """从新建 context 捕获自然 Explore 请求，不读取任何本地会话。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise AnonymousValidationError(
            "未安装 Playwright；请运行 uv sync 与 uv run playwright install chromium"
        ) from exc

    browser_kwargs: dict[str, Any] = {
        "headless": not headed,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if proxy:
        browser_kwargs["proxy"] = {"server": proxy}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**browser_kwargs)
        context = await browser.new_context(
            locale="es-ES",
            timezone_id="Europe/Madrid",
            viewport={"width": 1536, "height": 864},
            service_workers="block",
        )
        try:
            page = await context.new_page()
            if not skip_geo and not await check_egress_country(page, "ES"):
                raise AnonymousValidationError("validation_failure: 西班牙出口检查失败")

            queries: list[dict[str, str]] = []
            page.on("request", lambda request: _capture_explore_query(request, queries))
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            for _ in range(3):
                await page.evaluate(
                    "() => window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(2)

            cookies = await context.cookies()
            reject_login_cookies(cookies)
            browser_fields = extract_browser_request_params(queries)
            templates = extract_explore_templates(queries)
            if templates is None:
                raise AnonymousValidationError(
                    "validation_failure: 未捕获完整的首屏与翻页请求模板"
                )
            state = AnonymousBootstrap(
                cookie={
                    str(cookie.get("name")): str(cookie.get("value"))
                    for cookie in cookies
                    if isinstance(cookie.get("name"), str)
                    and isinstance(cookie.get("value"), str)
                },
                user_agent=await page.evaluate("() => navigator.userAgent"),
                browser_request_params=browser_fields,
                templates=templates,
            )
            return validate_bootstrap(state)
        finally:
            await context.close()
            await browser.close()


def _client_kwargs(state: AnonymousBootstrap, proxy: str | None) -> dict[str, Any]:
    return {
        "headers": {
            "User-Agent": state.user_agent,
            "Referer": EXPLORE_URL,
        },
        "cookies": state.cookie,
        "follow_redirects": True,
        "proxy": proxy or None,
        "timeout": httpx.Timeout(30.0),
        "limits": httpx.Limits(max_connections=50, max_keepalive_connections=10),
    }


def _passes_evidence_gate(
    metadata: Sequence[Mapping[str, object]], report: Sequence[Mapping[str, object]]
) -> bool:
    if not metadata or not report:
        return False
    if not any(
        isinstance(item.get("play_url"), str)
        or isinstance(item.get("download_url"), str)
        for item in metadata
    ):
        return False
    first = report[0]
    if first.get("http_status") != 200 or first.get("json") is not True:
        return False
    item_count = first.get("item_count")
    if not isinstance(item_count, int) or item_count < 1:
        return False
    if first.get("has_more"):
        if len(report) < 2:
            return False
        first_hashes = first.get("item_id_hashes", [])
        first_ids = (
            {item_id for item_id in first_hashes if isinstance(item_id, str)}
            if isinstance(first_hashes, list)
            else set()
        )
        later_pages = report[1:]
        later_ids = {
            item_id
            for page in later_pages
            for hashes in [page.get("item_id_hashes", [])]
            if isinstance(hashes, list)
            for item_id in hashes
            if isinstance(item_id, str)
        }
        if not later_ids - first_ids:
            return False
    return True


async def run_batch(
    *,
    categories: Sequence[str],
    category_names: Mapping[str, str],
    state: AnonymousBootstrap,
    count: int,
    max_pages: int,
    delay: float,
    category_delay: float,
    output_dir: Path,
    download: bool,
    url_mode: str,
    concurrency: int,
    max_retry: int,
    chunk_size: int,
    proxy: str | None,
    persist_and_upload: bool,
    gateway: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """串行处理分类；首分类直接 HTTP 重放通过后才允许任何副作用。"""
    initial_template = {"params": state.templates[0]}
    next_template = {"params": state.templates[1]}
    all_metadata: list[dict[str, Any]] = []
    all_manifest: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    seen_video_ids: set[str] = set()

    for index, category_type in enumerate(categories):
        started_at = time()
        category_name = get_category_name(category_type, category_names)
        category_dir = output_dir / category_type
        try:
            async with httpx.AsyncClient(**_client_kwargs(state, proxy)) as client:
                metadata, report = await collect_explore(
                    client,
                    initial_template=initial_template,
                    next_template=next_template,
                    category_type=category_type,
                    count=count,
                    max_pages=max_pages,
                    delay=delay,
                    cookie=state.cookie,
                    user_agent=state.user_agent,
                    initial_pull_type="1",
                )
                if index == 0 and not _passes_evidence_gate(metadata, report):
                    raise AnonymousValidationError(
                        "validation_failure: 首分类直接 HTTP 证据门失败"
                    )

                for item in metadata:
                    item["category_name"] = category_name
                canonical = [
                    item
                    for item in metadata
                    if isinstance(item.get("id"), str)
                    and item["id"] not in seen_video_ids
                ]
                duplicates = len(metadata) - len(canonical)
                seen_video_ids.update(str(item["id"]) for item in canonical)
                manifest: list[dict[str, Any]] = []
                if download and canonical:
                    manifest = await download_media(
                        client,
                        canonical,
                        output_dir=category_dir,
                        mode=url_mode,
                        concurrency=concurrency,
                        max_retry=max_retry,
                        chunk_size=chunk_size,
                        user_agent=state.user_agent,
                    )

            save_json(category_dir / "metadata.json", metadata)
            save_json(category_dir / "report.json", report)
            if download:
                save_json(category_dir / "download_manifest.json", manifest)

            upload_result: dict[str, int] | None = None
            if persist_and_upload and canonical:
                await persist_to_db(canonical, manifest)
                complete = bool(manifest) and all(record["ok"] for record in manifest)
                if download and complete:
                    from src.explore.upload import upload_pending_explore

                    upload_result = await upload_pending_explore(
                        media_root=output_dir,
                        layout="batch",
                        category_type=category_type,
                        gateway=gateway,
                    )

            all_metadata.extend(metadata)
            all_manifest.extend(manifest)
            summary[category_type] = {
                "category_type": category_type,
                "category_name": category_name,
                "status": "ok",
                "pages": len(report),
                "items": len(metadata),
                "duplicates": duplicates,
                "downloaded": sum(record["ok"] for record in manifest),
                "manifest_total": len(manifest),
                "upload": upload_result,
                "upload_skipped": bool(download and canonical and not upload_result),
                "duration_seconds": round(time() - started_at, 2),
            }
        except Exception as exc:  # noqa: BLE001
            if index == 0 and isinstance(exc, AnonymousValidationError):
                raise
            summary[category_type] = {
                "category_type": category_type,
                "category_name": category_name,
                "status": "error",
                "error_type": type(exc).__name__,
                "duration_seconds": round(time() - started_at, 2),
            }
        if index < len(categories) - 1 and category_delay:
            await asyncio.sleep(category_delay)
    return all_metadata, all_manifest, summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="匿名批量采集 TikTok Explore 公开内容。"
    )
    parser.add_argument("--live", action="store_true", help="显式允许联网。")
    parser.add_argument("--categories", default=None)
    parser.add_argument("--categories-file", type=Path, default=DEFAULT_CATEGORIES_PATH)
    parser.add_argument("--list-categories", action="store_true")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--count", type=positive_int, default=None)
    parser.add_argument("--max-pages", type=positive_int, default=None)
    parser.add_argument("--delay", type=nonnegative_float, default=None)
    parser.add_argument("--category-delay", type=nonnegative_float, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--url-mode", choices=URL_MODES, default=None)
    parser.add_argument("--concurrency", type=positive_int, default=None)
    parser.add_argument("--max-retry", type=positive_int, default=None)
    parser.add_argument("--chunk-kb", type=positive_int, default=None)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--skip-geo", action="store_true")
    parser.add_argument(
        "--download", action=argparse.BooleanOptionalAction, default=None
    )
    db_group = parser.add_mutually_exclusive_group()
    db_group.add_argument("--db", dest="no_db", action="store_false", default=None)
    db_group.add_argument("--no-db", dest="no_db", action="store_true", default=None)
    return parser.parse_args(argv)


def resolve_config(args: argparse.Namespace, cfg: Mapping[str, Any]) -> dict[str, Any]:
    builtin = {
        "categories": "",
        "count": DEFAULT_COUNT,
        "max_pages": DEFAULT_MAX_PAGES,
        "delay": DEFAULT_DELAY,
        "category_delay": DEFAULT_CATEGORY_DELAY,
        "concurrency": DEFAULT_CONCURRENCY,
        "max_retry": DEFAULT_MAX_RETRY,
        "chunk_kb": DEFAULT_CHUNK_KB,
        "url_mode": "play_url",
        "download": False,
        "no_db": True,
        "output_dir": str(DEFAULT_OUTPUT_DIR),
        "proxy": None,
    }
    resolved = {
        name: getattr(args, name)
        if getattr(args, name) is not None
        else cfg.get(name, builtin[name])
        for name in CONFIGURABLE_FIELDS
    }
    if resolved["url_mode"] == "all":
        raise AnonymousValidationError(
            "--url-mode all 不受支持：数据库每个 video_id 仅保存一条 media_path"
        )
    return resolved


def _print_categories(categories_file: Path) -> int:
    names = load_category_names(categories_file)
    if not names:
        print(f"未找到分类映射: {categories_file}")
        return 2
    for category_type, name in list_categories(names):
        print(f"{category_type:>6}  {name}")
    return 0


def _validate_categories(categories: Sequence[str], names: Mapping[str, str]) -> None:
    if not categories:
        raise AnonymousValidationError(
            "分类不能为空；请先使用 --list-categories 查看本地映射"
        )
    unknown = [category for category in categories if category not in names]
    if unknown:
        raise AnonymousValidationError("未知分类 ID: " + ", ".join(unknown))


def _preflight_storage() -> Any:
    if not os.getenv("TIKTOK_DATABASE_URL"):
        raise AnonymousValidationError("缺少 TIKTOK_DATABASE_URL，拒绝静默降级")
    from src.explore.s3 import S3Uploader

    return S3Uploader()


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    args = parse_args()
    if args.list_categories:
        return _print_categories(args.categories_file)
    if not args.live:
        print("未指定 --live，拒绝联网。")
        return 2
    try:
        cfg = load_batch_config(args.config, args.profile)
        resolved = resolve_config(args, cfg)
        category_names = load_category_names(args.categories_file)
        categories = parse_categories(resolved["categories"])
        _validate_categories(categories, category_names)
    except (AnonymousValidationError, BatchConfigError) as exc:
        print(f"配置错误: {exc}")
        return 2

    gateway = None
    try:
        if not resolved["no_db"]:
            gateway = _preflight_storage()
        state = asyncio.run(
            bootstrap_anonymous(
                proxy=resolved["proxy"], headed=args.headed, skip_geo=args.skip_geo
            )
        )
        metadata, manifest, summary = asyncio.run(
            run_batch(
                categories=categories,
                category_names=category_names,
                state=state,
                count=resolved["count"],
                max_pages=resolved["max_pages"],
                delay=resolved["delay"],
                category_delay=resolved["category_delay"],
                output_dir=Path(resolved["output_dir"]),
                download=resolved["download"],
                url_mode=resolved["url_mode"],
                concurrency=resolved["concurrency"],
                max_retry=resolved["max_retry"],
                chunk_size=resolved["chunk_kb"] * 1024,
                proxy=resolved["proxy"],
                persist_and_upload=not resolved["no_db"],
                gateway=gateway,
            )
        )
        output_dir = Path(resolved["output_dir"])
        save_json(output_dir / "all_metadata.json", metadata)
        save_json(output_dir / "all_manifest.json", manifest)
        save_json(output_dir / "summary.json", summary)
        return 0 if all(item["status"] == "ok" for item in summary.values()) else 1
    except AnonymousValidationError as exc:
        print(str(exc))
        return 1
    finally:
        if gateway is not None:
            gateway.close()
        try:
            from src.explore.db import dispose_tiktok_db

            asyncio.run(dispose_tiktok_db())
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
