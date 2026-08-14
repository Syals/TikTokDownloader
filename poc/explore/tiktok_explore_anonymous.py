"""匿名 TikTok Explore 批量采集、下载与可选上传。

默认使用临时浏览器状态重放 HTTP Explore 请求；--browser-pages 直接消费
未登录浏览器的 Explore 响应。任何状态均不会写入磁盘。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
    save_category_names,
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
    extract_explore_pagination,
    flatten_explore_item,
    nonnegative_float,
    persist_to_db,
    positive_int,
)
from poc.session.harvest_tiktok_session import check_egress_country, harvest_categories  # noqa: E402


EXPLORE_URL = "https://www.tiktok.com/explore"
DEFAULT_OUTPUT_DIR = Path(".output/explore/anonymous")
DEFAULT_CATEGORY_DELAY = 5.0
DEFAULT_MAX_PAGES = 2
DEFAULT_COUNT = 8
DEFAULT_DELAY = 2.0
LOGIN_COOKIE_NAMES = frozenset({"sessionid", "sessionid_ss", "sid_tt"})
BROWSER_CATEGORY_SELECTOR = 'button[class*="tux-chip__element"]'
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
    "browser_pages",
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


@dataclass
class BrowserCollection:
    """一次临时浏览器采集的媒体下载状态与脱敏 Explore 结果。"""

    cookie: dict[str, str]
    user_agent: str
    metadata: list[dict[str, Any]]
    report: list[dict[str, Any]]


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


def _client_kwargs(
    state: AnonymousBootstrap | BrowserCollection, proxy: str | None
) -> dict[str, Any]:
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


async def collect_explore_in_browser(
    *,
    category_type: str,
    category_names: Mapping[str, str],
    max_pages: int,
    delay: float,
    proxy: str | None,
    headed: bool,
    skip_geo: bool,
) -> BrowserCollection:
    """在临时未登录浏览器中选择分类并收集自然 Explore 响应。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise AnonymousValidationError(
            "未安装 Playwright；请运行 uv sync 与 uv run playwright install chromium"
        ) from exc

    try:
        category_index = list(category_names).index(category_type)
    except ValueError as exc:
        raise AnonymousValidationError(f"未知分类 ID: {category_type}") from exc

    browser_kwargs: dict[str, Any] = {
        "headless": not headed,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if proxy:
        browser_kwargs["proxy"] = {"server": proxy}

    captured_pages: list[tuple[dict[str, Any], dict[str, str], int, bool]] = []
    response_tasks: list[asyncio.Task[None]] = []
    browser_delay = max(delay, 0.5)

    async def capture_response(response: Any) -> None:
        if "/api/explore/item_list/" not in response.url:
            return
        params = dict(
            parse_qsl(urlsplit(response.request.url).query, keep_blank_values=True)
        )
        if params.get("categoryType") != category_type:
            return
        try:
            payload = await response.json()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(payload, dict) or len(captured_pages) >= max_pages:
            return
        captured_pages.append(
            (
                payload,
                params,
                response.status,
                bool(response.headers.get("x-ms-token")),
            )
        )

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

            page.on(
                "response",
                lambda response: response_tasks.append(
                    asyncio.create_task(capture_response(response))
                ),
            )
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(browser_delay)

            consent = page.get_by_role("button", name="Entendido")
            if await consent.count():
                await consent.click(force=True)
                await asyncio.sleep(0.5)

            if category_index:
                category_buttons = page.locator(BROWSER_CATEGORY_SELECTOR)
                if category_index >= await category_buttons.count():
                    raise AnonymousValidationError(
                        f"分类 {category_type} 未出现在当前 Explore 分类栏中"
                    )
                category_button = category_buttons.nth(category_index)
                interaction = category_button.locator(
                    '[data-testid="tux-web-interaction-container"]'
                )
                if await interaction.count():
                    await interaction.click(force=True)
                else:
                    await category_button.click(force=True)
                await asyncio.sleep(browser_delay)

            for _ in range(max_pages * 3):
                if len(captured_pages) >= max_pages:
                    break
                # Explore advances its infinite list from wheel events, not repeated bottom jumps.
                await page.mouse.wheel(0, 900)
                await asyncio.sleep(browser_delay)
            await asyncio.sleep(browser_delay)
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)

            cookies = await context.cookies()
            reject_login_cookies(cookies)
            cookie = {
                str(item.get("name")): str(item.get("value"))
                for item in cookies
                if isinstance(item.get("name"), str)
                and isinstance(item.get("value"), str)
            }
            user_agent = await page.evaluate("() => navigator.userAgent")
        finally:
            await context.close()
            await browser.close()

    if not captured_pages:
        raise AnonymousValidationError(
            f"validation_failure: 未捕获分类 {category_type} 的 Explore 浏览器响应"
        )

    seen_video_ids: set[str] = set()
    metadata: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    for page_number, (payload, params, status, received_ms_token) in enumerate(
        captured_pages, start=1
    ):
        item_list = payload.get("itemList")
        item_list_missing = not isinstance(item_list, list)
        items = (
            [item for item in item_list if isinstance(item, dict)]
            if not item_list_missing
            else []
        )
        flattened_items = [flatten_explore_item(item, category_type) for item in items]
        item_ids = [item["id"] for item in items if isinstance(item.get("id"), str)]
        next_cursor, has_more = extract_explore_pagination(payload, "")
        report.append(
            {
                "source": "browser",
                "pull_type": params.get("pullType", "?"),
                "http_status": status,
                "json": True,
                "request_field_names": list(params),
                "request_cursor_present": "cursor" in params,
                "request_ms_token_present": "msToken" in params,
                "item_count": len(items),
                "item_id_hashes": [
                    hashlib.sha256(item_id.encode()).hexdigest()[:12]
                    for item_id in item_ids
                ],
                "media_url_count": sum(
                    bool(item.get("play_url") or item.get("download_url"))
                    for item in flattened_items
                ),
                "has_more": has_more,
                "cursor_sha256": hashlib.sha256(next_cursor.encode()).hexdigest()[:12],
                "received_ms_token": received_ms_token,
                "page": page_number,
                "item_list_missing": item_list_missing,
            }
        )
        for item in flattened_items:
            if isinstance(item.get("id"), str) and item["id"] not in seen_video_ids:
                seen_video_ids.add(item["id"])
                metadata.append(item)

    return BrowserCollection(
        cookie=cookie,
        user_agent=user_agent,
        metadata=metadata,
        report=report,
    )


def _evidence_gate_diagnostics(
    metadata: Sequence[Mapping[str, object]], report: Sequence[Mapping[str, object]]
) -> dict[str, Any]:
    media_url_count = sum(
        isinstance(item.get("play_url"), str)
        or isinstance(item.get("download_url"), str)
        for item in metadata
    )
    reasons: list[str] = []

    if not metadata:
        reasons.append("未提取到任何视频元数据")
    elif not media_url_count:
        reasons.append("视频元数据中没有 play_url 或 download_url")
    if not report:
        reasons.append("未收到 Explore 响应报告")
    else:
        first = report[0]
        http_status = first.get("http_status")
        if http_status != 200:
            reasons.append(f"首响应 HTTP 状态={http_status!r}（期望 200）")
        if first.get("json") is not True:
            reasons.append("首响应不是可解析的 JSON")
        item_count = first.get("item_count")
        if not isinstance(item_count, int) or item_count < 1:
            reasons.append(f"首响应 item_count={item_count!r}（期望至少 1）")
        if first.get("has_more"):
            if len(report) < 2:
                reasons.append("首响应 has_more=true，但未获得第 2 页响应报告")
            else:
                first_hashes = first.get("item_id_hashes", [])
                first_ids = (
                    {item_id for item_id in first_hashes if isinstance(item_id, str)}
                    if isinstance(first_hashes, list)
                    else set()
                )
                later_ids = {
                    item_id
                    for page in report[1:]
                    for hashes in [page.get("item_id_hashes", [])]
                    if isinstance(hashes, list)
                    for item_id in hashes
                    if isinstance(item_id, str)
                }
                if not later_ids - first_ids:
                    reasons.append("翻页响应没有包含相对首屏的新视频 ID")

    return {
        "metadata_count": len(metadata),
        "media_url_count": media_url_count,
        "report": list(report),
        "reasons": reasons,
    }


def _passes_evidence_gate(
    metadata: Sequence[Mapping[str, object]], report: Sequence[Mapping[str, object]]
) -> bool:
    return not _evidence_gate_diagnostics(metadata, report)["reasons"]


def _save_evidence_failure(
    *,
    category_type: str,
    category_name: str | None,
    category_dir: Path,
    diagnostics: Mapping[str, object],
    request_error_type: str | None = None,
) -> str:
    diagnostic = {
        "category_type": category_type,
        "category_name": category_name,
        **diagnostics,
    }
    if request_error_type:
        diagnostic["request_error_type"] = request_error_type
    diagnostic_path = category_dir / "evidence_failure.json"
    try:
        save_json(diagnostic_path, diagnostic)
    except Exception as exc:  # noqa: BLE001
        return f"诊断报告写入失败: {type(exc).__name__}: {exc}"
    return f"诊断报告: {diagnostic_path}"


async def run_batch(
    *,
    categories: Sequence[str],
    category_names: Mapping[str, str],
    state: AnonymousBootstrap | None,
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
    browser_pages: bool = False,
    headed: bool = False,
    skip_geo: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """串行处理分类；首分类通过后才允许采集、下载和数据库副作用。"""
    initial_template = {"params": state.templates[0]} if state else {}
    next_template = {"params": state.templates[1]} if state else {}
    all_metadata: list[dict[str, Any]] = []
    all_manifest: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    seen_video_ids: set[str] = set()

    for index, category_type in enumerate(categories):
        started_at = time()
        category_name = get_category_name(category_type, category_names)
        category_dir = output_dir / category_type
        try:
            metadata: list[dict[str, Any]] = []
            report: list[dict[str, Any]] = []
            if browser_pages:
                collection = await collect_explore_in_browser(
                    category_type=category_type,
                    category_names=category_names,
                    max_pages=max_pages,
                    delay=delay,
                    proxy=proxy,
                    headed=headed,
                    skip_geo=skip_geo,
                )
                active_state: AnonymousBootstrap | BrowserCollection = collection
                metadata, report = collection.metadata, collection.report
            else:
                if state is None:
                    raise AnonymousValidationError("缺少 HTTP 重放所需的匿名浏览器状态")
                active_state = state

            async with httpx.AsyncClient(
                **_client_kwargs(active_state, proxy)
            ) as client:
                if not browser_pages:
                    try:
                        metadata, report = await collect_explore(
                            client,
                            initial_template=initial_template,
                            next_template=next_template,
                            category_type=category_type,
                            count=count,
                            max_pages=max_pages,
                            delay=delay,
                            cookie=active_state.cookie,
                            user_agent=active_state.user_agent,
                            initial_pull_type="1",
                        )
                    except httpx.HTTPError as exc:
                        if index != 0:
                            raise
                        report = []
                        if isinstance(exc, httpx.HTTPStatusError):
                            response = exc.response
                            report.append(
                                {
                                    "http_status": response.status_code,
                                    "json": "application/json"
                                    in response.headers.get("content-type", ""),
                                }
                            )
                        diagnostics = _evidence_gate_diagnostics([], report)
                        diagnostic_location = _save_evidence_failure(
                            category_type=category_type,
                            category_name=category_name,
                            category_dir=category_dir,
                            diagnostics=diagnostics,
                            request_error_type=type(exc).__name__,
                        )
                        raise AnonymousValidationError(
                            "validation_failure: 首分类直接 HTTP 请求失败: "
                            + type(exc).__name__
                            + "；"
                            + "；".join(diagnostics["reasons"])
                            + f"；{diagnostic_location}"
                        ) from exc
                diagnostics = _evidence_gate_diagnostics(metadata, report)
                if index == 0 and diagnostics["reasons"]:
                    diagnostic_location = _save_evidence_failure(
                        category_type=category_type,
                        category_name=category_name,
                        category_dir=category_dir,
                        diagnostics=diagnostics,
                    )
                    raise AnonymousValidationError(
                        "validation_failure: 首分类 Explore 证据门失败: "
                        + "；".join(diagnostics["reasons"])
                        + f"；{diagnostic_location}"
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
                        user_agent=active_state.user_agent,
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
    parser.add_argument(
        "--harvest-categories",
        action="store_true",
        help="从 TikTok Explore SSR 抓取分类映射并保存到 --categories-file。",
    )
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
        "--browser-pages",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="在未登录浏览器中直接采集 Explore 响应，而非 HTTP 重放。",
    )
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
        "browser_pages": False,
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


async def harvest_anonymous_categories(
    *,
    proxy: str | None,
    headed: bool,
    skip_geo: bool,
    categories_file: Path,
) -> dict[str, str]:
    """启动匿名浏览器访问 Explore，从 SSR 提取分类映射并落盘。"""
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

            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(3)

            categories = await harvest_categories(page)
            if not categories:
                raise AnonymousValidationError("未从 Explore SSR 提取到任何分类")
            path = save_category_names(categories, path=categories_file)
            print(f"已保存 {len(categories)} 个分类到 {path}")
            return categories
        finally:
            await context.close()
            await browser.close()


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


def main(argv: Sequence[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    args = parse_args(argv)
    if args.list_categories:
        return _print_categories(args.categories_file)
    if args.harvest_categories:
        if not args.live:
            print("未指定 --live，拒绝联网。")
            return 2
        try:
            asyncio.run(
                harvest_anonymous_categories(
                    proxy=args.proxy,
                    headed=args.headed,
                    skip_geo=args.skip_geo,
                    categories_file=args.categories_file,
                )
            )
            return 0
        except AnonymousValidationError as exc:
            print(str(exc))
            return 1
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
        state = (
            None
            if resolved["browser_pages"]
            else asyncio.run(
                bootstrap_anonymous(
                    proxy=resolved["proxy"],
                    headed=args.headed,
                    skip_geo=args.skip_geo,
                )
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
                browser_pages=resolved["browser_pages"],
                headed=args.headed,
                skip_geo=args.skip_geo,
            )
        )
        output_dir = Path(resolved["output_dir"])
        save_json(output_dir / "all_metadata.json", metadata)
        save_json(output_dir / "all_manifest.json", manifest)
        save_json(output_dir / "summary.json", summary)
        return 0 if all(item["status"] == "ok" for item in summary.values()) else 1
    except AnonymousValidationError as exc:
        print(f"[失败] {exc}")
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
