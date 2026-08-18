"""批量遍历多个 TikTok Explore 分类并采集 + 下载（单 client 共享 session）。"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._batch_config import (  # noqa: E402
    BatchConfigError,
    load_batch_config,
)
from poc.explore._categories import (  # noqa: E402
    DEFAULT_CATEGORIES_PATH,
    find_explore_categories,
    get_category_name,
    list_categories,
    load_category_meta,
    load_category_names,
    merge_explore_categories,
    save_category_names,
)
from poc.explore._common import (  # noqa: E402
    DEFAULT_CHUNK_KB,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_RETRY,
    URL_MODES,
)
from poc.explore.tiktok_explore_poc import (  # noqa: E402
    build_base_params,
    collect_explore,
    download_media,
    load_browser_request_params,
    load_explore_templates,
    nonnegative_float,
    persist_to_db,
    positive_int,
)
from poc.explore.tiktok_explore_replay import (  # noqa: E402
    DEFAULT_SETTINGS_PATH,
    load_device_id,
    load_session,
)
from poc.session.harvest_tiktok_session import (  # noqa: E402
    EXPLORE_URL,
    extract_ssr_json,
)
from src.explore.disk_check import check_disk_usage  # noqa: E402


DEFAULT_CATEGORIES = ""
DEFAULT_OUTPUT_DIR = Path(".output/explore/batch")
DEFAULT_CATEGORY_DELAY = 5.0
DEFAULT_MAX_PAGES = 2
DEFAULT_COUNT = 8
DEFAULT_DELAY = 2.0
# 分类映射自动刷新 TTL：文档请求不带任何风控拦截（实测 HTTP/1.1 匿名可得
# 完整 SSR），但分类表变化低频，定时任务场景无需每次运行都多一次请求。
DEFAULT_CATEGORIES_TTL_HOURS = 168.0
# settings 不可用时的刷新请求 UA（Linux Chromium，与 VPS 采集环境一致）。
REFRESH_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


class CategoryRefreshError(RuntimeError):
    """分类映射联网刷新失败。"""


# 下载前磁盘检测阈值；取值 <= 0 表示关闭对应检测。
DEFAULT_DISK_USED_PERCENT = 70.0
DEFAULT_MIN_FREE_GB = 10.0

# 这些字段可由配置（defaults + profile）覆盖；会话相关字段不在其中。
CONFIGURABLE_FIELDS = (
    "categories",
    "count",
    "max_pages",
    "delay",
    "category_delay",
    "disk_used_percent",
    "min_free_gb",
    "concurrency",
    "max_retry",
    "chunk_kb",
    "url_mode",
    "download",
    "no_db",
    "output_dir",
    "proxy",
)


def parse_categories(text: str) -> list[str]:
    """解析逗号分隔的 category_type 列表，去空去重。"""
    categories = [part.strip() for part in text.split(",") if part.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for category_type in categories:
        if category_type not in seen:
            seen.add(category_type)
            unique.append(category_type)
    return unique


def expand_categories(
    parsed: Sequence[str],
    names: Mapping[str, str],
) -> list[str] | None:
    """``all`` 通配符展开为本地分类映射的全部 ID（升序）。

    返回 ``None`` 表示未使用通配符，调用方走原有逐项校验路径；
    使用了通配符但映射为空时返回 ``[]``，由调用方报错。
    """
    if parsed != ["all"]:
        return None
    return [category_id for category_id, _ in list_categories(names)]


def parse_categories_from_html(html: str) -> dict[str, str]:
    """从 explore 页 HTML 解析 SSR 分类映射（方案 A 实测路径）。

    与 harvest 的 HTML 回退解析共用 ``extract_ssr_json`` / ``find_explore_categories``，
    拿不到 ``exploreCategoryList`` 时抛 :class:`CategoryRefreshError`。
    """
    payload = extract_ssr_json(html)
    if payload is None:
        raise CategoryRefreshError(
            "explore 页缺少 __UNIVERSAL_DATA_FOR_REHYDRATION__ SSR 数据"
        )
    category_list = find_explore_categories(payload)
    if not isinstance(category_list, dict):
        raise CategoryRefreshError("SSR 中未找到 exploreCategoryList")
    merged = merge_explore_categories(category_list)
    if not merged:
        raise CategoryRefreshError("exploreCategoryList 未包含有效分类")
    return merged


def settings_user_agent(settings_path: Path) -> str | None:
    """宽松读取 settings 中的 User-Agent；不可用时返回 None（不要求会话完整）。"""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    info = data.get("browser_info_tiktok")
    ua = info.get("User-Agent") if isinstance(info, dict) else None
    return ua if isinstance(ua, str) and ua else None


async def refresh_categories_via_http(
    *,
    proxy: str | None,
    user_agent: str,
    timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    """匿名 httpx 请求 explore 文档并解析分类映射（不依赖登录会话）。"""
    client_kwargs: dict[str, Any] = {
        "headers": {
            "User-Agent": user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
        "follow_redirects": True,
        "timeout": timeout,
        "verify": False,
    }
    # transport 与 proxy 互斥；transport 仅供测试注入 MockTransport。
    if transport is not None:
        client_kwargs["transport"] = transport
    else:
        client_kwargs["proxy"] = proxy or None
    async with httpx.AsyncClient(**client_kwargs) as client:
        resp = await client.get(EXPLORE_URL)
        if resp.status_code != 200:
            raise CategoryRefreshError(f"explore 页返回 HTTP {resp.status_code}")
        return parse_categories_from_html(resp.text)


async def refresh_categories_via_browser(
    *,
    categories_file: Path,
    proxy: str | None,
) -> dict[str, str]:
    """浏览器兜底：复用匿名脚本现有实现（含地理校验与落盘）。"""
    from poc.explore.tiktok_explore_anonymous import harvest_anonymous_categories

    return await harvest_anonymous_categories(
        proxy=proxy,
        headed=False,
        skip_geo=False,
        categories_file=categories_file,
    )


def categories_need_refresh(
    path: Path,
    *,
    ttl_hours: float,
    force: bool,
    disabled: bool,
) -> bool:
    """判定是否需要联网刷新：禁用优先，其次强制，最后缺失/超过 TTL。

    ``_meta.harvested_at`` 缺失或损坏同样视为过期，触发一次幂等刷新。
    """
    if disabled:
        return False
    if force:
        return True
    if not load_category_names(path):
        return True
    meta = load_category_meta(path)
    try:
        harvested_at = datetime.fromisoformat(str(meta.get("harvested_at")))
    except (TypeError, ValueError):
        return True
    if harvested_at.tzinfo is None:
        harvested_at = harvested_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - harvested_at > timedelta(hours=ttl_hours)


async def maybe_refresh_categories(
    *,
    categories_file: Path,
    proxy: str | None,
    force: bool,
    disabled: bool,
    settings_path: Path,
    ttl_hours: float = DEFAULT_CATEGORIES_TTL_HOURS,
) -> str:
    """采集前按需联网刷新分类映射：HTTP 主路径，浏览器兜底，本地降级。

    返回执行状态：

    - ``disabled`` / ``fresh``：未触发刷新；
    - ``refreshed_http`` / ``refreshed_browser``：刷新成功并已落盘；
    - ``kept_local_after_failure``：两条路径均失败但本地有映射，降级继续；
    - ``failed_no_local``：均失败且本地无映射，调用方应退出。
    """
    if not categories_need_refresh(
        categories_file, ttl_hours=ttl_hours, force=force, disabled=disabled
    ):
        _log(
            "提示: --no-refresh-categories，仅使用本地分类映射。"
            if disabled
            else "提示: 分类映射新鲜，跳过联网刷新。"
        )
        return "disabled" if disabled else "fresh"

    user_agent = settings_user_agent(settings_path) or REFRESH_FALLBACK_USER_AGENT
    try:
        categories = await refresh_categories_via_http(
            proxy=proxy, user_agent=user_agent
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"警告: HTTP 刷新分类映射失败: {type(exc).__name__}: {exc}")
        try:
            # 兜底路径内部已完成落盘。
            categories = await refresh_categories_via_browser(
                categories_file=categories_file, proxy=proxy
            )
        except Exception as exc:  # noqa: BLE001
            if load_category_names(categories_file):
                _log(
                    f"警告: 浏览器兜底亦失败，继续使用本地分类映射: "
                    f"{type(exc).__name__}: {exc}"
                )
                return "kept_local_after_failure"
            _log(f"错误: 分类映射刷新失败且本地无映射: {type(exc).__name__}: {exc}")
            return "failed_no_local"
        _log(f"[完成] 已通过浏览器兜底刷新分类映射 ({len(categories)} 个分类)")
        return "refreshed_browser"

    save_category_names(categories, path=categories_file)
    _log(f"[完成] 已通过 HTTP 刷新分类映射 ({len(categories)} 个分类)")
    return "refreshed_http"


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _log(message: str) -> None:
    """进度统一走 stdout 并立即 flush，保证重定向到文件时实时可见。"""
    print(message, flush=True)


def _collect_stop_reason(report: Sequence[Mapping[str, Any]]) -> str:
    """从分页报告推导采集停止原因，用于 0 条目分类的日志提示。"""
    if not report:
        return "无分页响应（未收到任何页面报告）"
    last = report[-1]
    if last.get("json") is False:
        return "响应非 JSON（疑似风控/验证页，需检查 cookie 或代理）"
    if "item_count" not in last:
        return "响应体不是有效 JSON 对象（疑似被拦截）"
    if last.get("item_list_missing"):
        reason = "响应缺少 itemList 字段（服务端未返回内容）"
        if probe := last.get("payload_status_probe"):
            reason += f"；状态字段: {json.dumps(probe, ensure_ascii=False)}"
            status_code = probe.get("statusCode", probe.get("status_code", ""))
            if status_code in ("", "0"):
                if last.get("has_more"):
                    reason += (
                        "；statusCode=0 且 hasMore=true：服务端有内容但未下发，"
                        "疑似会话/指纹被降权（非分类池为空）"
                    )
                else:
                    reason += (
                        "；statusCode=0：请求本身成功，分类内容池为空"
                        "（非风控/会话问题）"
                    )
            else:
                reason += "；statusCode 非 0：疑似风控/限流"
        if "payload_top_keys" in last:
            top_keys = last["payload_top_keys"]
            reason += f"；响应顶层字段: {', '.join(top_keys) or '(空对象)'}"
        return reason
    if not last.get("has_more"):
        return (
            "hasMore=false：服务端判定无更多内容，多见于该分类不向当前地区/账号会话投放"
        )
    if last.get("new_item_count", 1) == 0:
        return "翻页无新增条目（全部重复），提前停止"
    return "达到 max_pages 上限"


def _page_progress(
    category_type: str, category_name: str
) -> Callable[[int, int, int], None]:
    def progress(page: int, new_items: int, total_items: int) -> None:
        _log(
            f"    [{category_type}] {category_name} 第 {page} 页: "
            f"新增 {new_items} 条，累计 {total_items} 条"
        )

    return progress


def _disk_low_for_download(
    category_summary: dict[str, Any],
    path: Path,
    *,
    disk_used_percent: float,
    min_free_gb: float,
) -> bool:
    """下载前检测磁盘空间，快照写入 category_summary。

    阈值触发返回 True（调用方应跳过下载）；检测本身失败降级为
    ``disk_warning`` 警告并返回 False，不阻断正常下载流程。
    """
    try:
        disk = check_disk_usage(
            path,
            used_percent_threshold=disk_used_percent,
            min_free_gb=min_free_gb,
        )
    except OSError as exc:
        category_summary["disk_warning"] = f"{type(exc).__name__}: {exc}"
        return False
    category_summary["disk"] = {
        "used_percent": round(disk.used_percent, 2),
        "free_gb": round(disk.free_gb, 2),
    }
    return disk.need_cleanup


def _client_kwargs(
    user_agent: str,
    cookie: dict[str, str],
    proxy: str | None,
    timeout: float = 30,
) -> dict[str, Any]:
    return {
        "headers": {"User-Agent": user_agent},
        "cookies": cookie,
        "follow_redirects": True,
        "proxy": proxy or None,
        "timeout": timeout,
        "verify": False,
        "limits": httpx.Limits(max_connections=50, max_keepalive_connections=10),
    }


async def run_batch(
    *,
    categories: Sequence[str],
    category_names: Mapping[str, str],
    device_id: str,
    browser_request_params: Mapping[str, str],
    browser_templates: tuple[list[tuple[str, str]], list[tuple[str, str]]] | None,
    count: int,
    max_pages: int,
    delay: float,
    category_delay: float,
    disk_used_percent: float = DEFAULT_DISK_USED_PERCENT,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    output_dir: Path,
    download: bool,
    url_mode: str,
    concurrency: int,
    max_retry: int,
    chunk_size: int,
    cookie: dict[str, str],
    user_agent: str,
    proxy: str | None,
    persist_and_upload: bool = False,
    gateway: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """串行遍历分类，每个分类使用独立 AsyncClient；单分类失败不中断后续。

    下载前按磁盘阈值检测，空间不足时跳过该分类下载、仅保留采集。
    """
    if browser_templates:
        initial_template: dict[str, Any] = {"params": browser_templates[0]}
        next_template: dict[str, Any] = {"params": browser_templates[1]}
    else:
        initial_template = {
            "params": build_base_params(device_id, browser_request_params)
        }
        next_template = {
            "params": build_base_params(
                device_id,
                browser_request_params,
                from_page="",
            )
        }
    all_metadata: list[dict[str, Any]] = []
    all_manifest: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}

    if not persist_and_upload:
        _log(
            "提示: 未启用数据库持久化与 S3 上传（--no-db 或 S3 初始化失败），"
            "本次仅采集 + 下载。"
        )

    for index, category_type in enumerate(categories):
        started_at = time()
        category_dir = output_dir / category_type
        category_name = get_category_name(category_type, category_names)
        category_summary: dict[str, Any] = {
            "category_type": category_type,
            "category_name": category_name,
            "status": "running",
        }

        try:
            _log(
                f"[{category_type}] {category_name}: 开始采集 "
                f"(count={count}, max_pages={max_pages})"
            )
            async with httpx.AsyncClient(
                **_client_kwargs(user_agent, cookie, proxy)
            ) as client:
                metadata, report = await collect_explore(
                    client,
                    initial_template=initial_template,
                    next_template=next_template,
                    category_type=category_type,
                    count=count,
                    max_pages=max_pages,
                    delay=delay,
                    cookie=cookie,
                    user_agent=user_agent,
                    initial_pull_type="1",
                    progress=_page_progress(category_type, category_name),
                )
                for item in metadata:
                    item["category_name"] = category_name

                stop_reason = _collect_stop_reason(report)
                if metadata:
                    _log(
                        f"[{category_type}] {category_name}: 采集完成 "
                        f"{len(report)} 页 / {len(metadata)} 条目 ({stop_reason})"
                    )
                else:
                    _log(
                        f"[{category_type}] {category_name}: 采集完成 0 条目 / "
                        f"{len(report)} 页 — {stop_reason}"
                    )

                manifest: list[dict[str, Any]] = []
                if download and metadata:
                    if _disk_low_for_download(
                        category_summary,
                        category_dir,
                        disk_used_percent=disk_used_percent,
                        min_free_gb=min_free_gb,
                    ):
                        category_summary["download_skipped"] = "disk_low"
                        _log(
                            f"[{category_type}] {category_name}: 磁盘空间不足，跳过下载"
                        )
                    else:
                        manifest = await download_media(
                            client,
                            metadata,
                            output_dir=category_dir,
                            mode=url_mode,
                            concurrency=concurrency,
                            max_retry=max_retry,
                            chunk_size=chunk_size,
                            user_agent=user_agent,
                        )
                        downloaded_ok = sum(record["ok"] for record in manifest)
                        _log(
                            f"[{category_type}] {category_name}: 下载完成 "
                            f"{downloaded_ok}/{len(manifest)} 个媒体文件"
                        )
                        if downloaded_ok < len(manifest):
                            _log(
                                f"[{category_type}] {category_name}: "
                                f"{len(manifest) - downloaded_ok} 个下载失败，详见 "
                                f"{category_dir / 'download_manifest.json'}"
                            )
                elif download:
                    _log(f"[{category_type}] {category_name}: 采集 0 条目，跳过下载")

            save_json(category_dir / "metadata.json", metadata)
            save_json(category_dir / "report.json", report)
            if download:
                save_json(category_dir / "download_manifest.json", manifest)

            if persist_and_upload and metadata:
                try:
                    await persist_to_db(metadata, manifest)
                except Exception as exc:  # noqa: BLE001
                    reason = f"{type(exc).__name__}: {exc}"
                    category_summary["storage_warning"] = reason
                    _log(
                        f"[{category_type}] {category_name}: 持久化数据库失败，"
                        f"跳过上传: {reason}"
                    )
                else:
                    _log(
                        f"[{category_type}] {category_name}: "
                        f"已持久化 {len(metadata)} 条到数据库"
                    )
                    if not (download and manifest):
                        category_summary["upload_skipped"] = True
                        _log(
                            f"[{category_type}] {category_name}: "
                            "无已下载清单（未开启下载或无媒体 URL），跳过上传"
                        )
                    else:
                        try:
                            from src.explore.upload import upload_pending_explore

                            upload_result = await upload_pending_explore(
                                media_root=output_dir,
                                layout="batch",
                                category_type=category_type,
                                gateway=gateway,
                                limit=max(len(manifest), 100),
                            )
                        except Exception as exc:  # noqa: BLE001
                            reason = f"{type(exc).__name__}: {exc}"
                            category_summary["storage_warning"] = reason
                            _log(
                                f"[{category_type}] {category_name}: 上传异常: {reason}"
                            )
                        else:
                            category_summary["upload"] = upload_result
                            _log(
                                f"[{category_type}] {category_name}: 上传完成 "
                                f"成功 {upload_result.get('success', 0)} / "
                                f"失败 {upload_result.get('failed', 0)} / "
                                f"跳过 {upload_result.get('skipped', 0)}"
                            )

            all_metadata.extend(metadata)
            all_manifest.extend(manifest)

            category_summary |= {
                "status": "ok",
                "items": len(metadata),
                "pages": len(report),
                "collect_stop_reason": stop_reason,
                "downloaded": sum(record["ok"] for record in manifest),
                "manifest_total": len(manifest),
                "duration_seconds": round(time() - started_at, 2),
            }
        except Exception as exc:  # noqa: BLE001
            category_summary |= {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "duration_seconds": round(time() - started_at, 2),
            }

        summary[category_type] = category_summary

        if index < len(categories) - 1 and category_delay > 0:
            await asyncio.sleep(category_delay)

    return all_metadata, all_manifest, summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="批量遍历多个 TikTok Explore 分类并采集 + 下载。",
    )
    parser.add_argument("--live", action="store_true", help="发送联网请求。")
    parser.add_argument(
        "--download",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="下载媒体文件（--no-download 关闭）。",
    )
    parser.add_argument(
        "--categories", default=None, help="分类 ID（逗号分隔，如 104,112）。"
    )
    parser.add_argument(
        "--categories-file",
        type=Path,
        default=DEFAULT_CATEGORIES_PATH,
        help="分类 ID→名称映射 JSON（由 harvest_tiktok_session 生成）。",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="列出已知分类 ID→名称后退出（默认读取本地映射文件）。",
    )
    refresh_group = parser.add_mutually_exclusive_group()
    refresh_group.add_argument(
        "--refresh-categories",
        action="store_true",
        help=(
            "采集前强制联网刷新分类映射"
            f"（默认仅在映射缺失或超过 {DEFAULT_CATEGORIES_TTL_HOURS:.0f}h 时自动刷新）。"
        ),
    )
    refresh_group.add_argument(
        "--no-refresh-categories",
        action="store_true",
        help="禁用采集前的自动联网刷新，仅使用本地映射文件。",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="读取 JSON 配置（Volume/batch_config.json）。",
    )
    parser.add_argument(
        "--profile", default=None, help="档位名，从配置的 profiles 中选取。"
    )
    parser.add_argument("--count", type=positive_int, default=None)
    parser.add_argument("--max-pages", type=positive_int, default=None)
    parser.add_argument("--delay", type=nonnegative_float, default=None)
    parser.add_argument("--category-delay", type=nonnegative_float, default=None)
    parser.add_argument(
        "--disk-used-percent",
        type=nonnegative_float,
        default=None,
        help="下载前检测：使用率 >= 该值时跳过该分类下载（0 关闭，默认 90）。",
    )
    parser.add_argument(
        "--min-free-gb",
        type=nonnegative_float,
        default=None,
        help="下载前检测：剩余空间 <= 该值(GB)时跳过下载（0 关闭，默认 5）。",
    )
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--url-mode",
        choices=URL_MODES,
        default=None,
    )
    parser.add_argument("--concurrency", type=positive_int, default=None)
    parser.add_argument("--max-retry", type=positive_int, default=None)
    parser.add_argument("--chunk-kb", type=positive_int, default=None)
    parser.add_argument("--proxy", default=None)
    db_group = parser.add_mutually_exclusive_group()
    db_group.add_argument(
        "--db",
        dest="no_db",
        action="store_false",
        default=None,
        help="启用 media-pipeline 数据库存储。",
    )
    db_group.add_argument(
        "--no-db",
        dest="no_db",
        action="store_true",
        default=None,
        help="跳过 media-pipeline 数据库存储。",
    )
    return parser.parse_args(argv)


def resolve_config(
    args: argparse.Namespace,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """按 CLI > 配置 > 内置默认 的优先级合并可配置字段。"""
    builtin: dict[str, Any] = {
        "categories": DEFAULT_CATEGORIES,
        "count": DEFAULT_COUNT,
        "max_pages": DEFAULT_MAX_PAGES,
        "delay": DEFAULT_DELAY,
        "category_delay": DEFAULT_CATEGORY_DELAY,
        "disk_used_percent": DEFAULT_DISK_USED_PERCENT,
        "min_free_gb": DEFAULT_MIN_FREE_GB,
        "concurrency": DEFAULT_CONCURRENCY,
        "max_retry": DEFAULT_MAX_RETRY,
        "chunk_kb": DEFAULT_CHUNK_KB,
        "url_mode": URL_MODES[0],
        "download": False,
        "no_db": False,
        "output_dir": str(DEFAULT_OUTPUT_DIR),
        "proxy": None,
    }
    resolved: dict[str, Any] = {}
    for name in CONFIGURABLE_FIELDS:
        cli_value = getattr(args, name)
        resolved[name] = (
            cli_value if cli_value is not None else cfg.get(name, builtin[name])
        )
    # proxy 单独处理：仅当 CLI 与配置都未提供（None）时才回退环境变量；
    # 显式空字符串表示"不使用代理"，不应命中 TIKTOK_POC_PROXY。
    # 注意与其他 poc/explore 脚本的内联 env 解析不同：此处配置优先于环境变量。
    proxy = resolved["proxy"]
    if proxy is None:
        proxy = os.getenv("TIKTOK_POC_PROXY") or None
    resolved["proxy"] = proxy
    return resolved


def _print_category_help(names: Mapping[str, str]) -> None:
    if not names:
        print(
            "提示: 当前没有本地分类映射（Volume/explore_categories.json）。\n"
            "请先运行 harvest_tiktok_session --proxy <西班牙代理> "
            "生成映射后再重试。"
        )
        return
    print("已知分类:")
    for id_, name in list_categories(names):
        print(f"  {id_:>6}  {name}")


def _print_categories(categories_file: Path) -> int:
    names = load_category_names(categories_file)
    if not names:
        print(
            f"未找到分类映射: {categories_file}\n"
            "请先运行 harvest_tiktok_session（--proxy 西班牙出口）生成映射。"
        )
        return 2
    for id_, name in list_categories(names):
        print(f"{id_:>6}  {name}")
    return 0


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    args = parse_args()

    if args.list_categories:
        if args.live:
            # --live 时先按 TTL 刷新再打印，闭环“查看即最新”。
            asyncio.run(
                maybe_refresh_categories(
                    categories_file=args.categories_file,
                    proxy=args.proxy or os.getenv("TIKTOK_POC_PROXY") or None,
                    force=args.refresh_categories,
                    disabled=args.no_refresh_categories,
                    settings_path=args.settings,
                )
            )
        return _print_categories(args.categories_file)

    if not args.live:
        print("未指定 --live，拒绝联网。")
        return 2

    try:
        cfg = load_batch_config(args.config, args.profile)
    except BatchConfigError as exc:
        print(f"配置错误: {exc}")
        return 2
    resolved = resolve_config(args, cfg)
    output_dir = Path(resolved["output_dir"])

    refresh_state = asyncio.run(
        maybe_refresh_categories(
            categories_file=args.categories_file,
            proxy=resolved["proxy"] or None,
            force=args.refresh_categories,
            disabled=args.no_refresh_categories,
            settings_path=args.settings,
        )
    )
    if refresh_state == "failed_no_local":
        return 2

    category_names = load_category_names(args.categories_file)
    categories = parse_categories(resolved["categories"])
    expanded = expand_categories(categories, category_names)
    if expanded is not None:
        categories = expanded
    if not categories:
        print("错误: --categories 为空。")
        _print_category_help(category_names)
        return 2

    if expanded is None:
        unknown = [
            category for category in categories if category not in category_names
        ]
        if unknown:
            print("错误: 以下分类 ID 不在已知映射列表中: " + ", ".join(unknown))
            _print_category_help(category_names)
            return 2

    try:
        cookie, user_agent = load_session(args.settings)
        device_id = load_device_id(args.settings)
        browser_request_params = load_browser_request_params(args.settings)
        browser_templates = load_explore_templates(args.settings)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误: 无法加载会话: {type(exc).__name__}: {exc}")
        return 2

    gateway = None
    persist_and_upload = False
    if not resolved["no_db"]:
        try:
            from src.explore.s3 import S3Uploader

            gateway = S3Uploader()
            persist_and_upload = True
        except Exception as exc:  # noqa: BLE001
            print(
                f"警告: 初始化 S3 上传器失败，将跳过数据库持久化与上传: "
                f"{type(exc).__name__}: {exc}"
            )

    async def run(
        gateway: Any = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        try:
            return await run_batch(
                categories=categories,
                category_names=category_names,
                device_id=device_id,
                browser_request_params=browser_request_params,
                browser_templates=browser_templates,
                count=resolved["count"],
                max_pages=resolved["max_pages"],
                delay=resolved["delay"],
                category_delay=resolved["category_delay"],
                disk_used_percent=resolved["disk_used_percent"],
                min_free_gb=resolved["min_free_gb"],
                output_dir=output_dir,
                download=resolved["download"],
                url_mode=resolved["url_mode"],
                concurrency=resolved["concurrency"],
                max_retry=resolved["max_retry"],
                chunk_size=resolved["chunk_kb"] * 1024,
                cookie=cookie,
                user_agent=user_agent,
                proxy=resolved["proxy"] or None,
                persist_and_upload=persist_and_upload,
                gateway=gateway,
            )
        finally:
            from src.explore.db import dispose_tiktok_db

            await dispose_tiktok_db()

    try:
        metadata, manifest, summary = asyncio.run(run(gateway=gateway))
    except httpx.HTTPError as error:
        print(f"网络错误: {type(error).__name__}")
        return 2
    finally:
        if gateway is not None:
            gateway.close()

    output_dir.mkdir(parents=True, exist_ok=True)
    save_json(output_dir / "all_metadata.json", metadata)
    save_json(output_dir / "all_manifest.json", manifest)
    save_json(output_dir / "summary.json", summary)

    total_items = len(metadata)
    total_manifest = len(manifest)
    downloaded = sum(record["ok"] for record in manifest)
    upload_totals = {
        field: sum(
            (cat_summary.get("upload") or {}).get(field, 0)
            for cat_summary in summary.values()
        )
        for field in ("success", "failed", "skipped")
    }
    _log(
        f"批量 Explore 完成: {len(categories)} 个分类, {total_items} 个条目, "
        f"已下载 {downloaded}/{total_manifest} 个媒体文件, "
        f"已上传 {upload_totals['success']} 个"
        f"（失败 {upload_totals['failed']}, 跳过 {upload_totals['skipped']}）。"
    )
    for category_type, cat_summary in summary.items():
        status = cat_summary.get("status")
        category_name = cat_summary.get("category_name") or "-"
        if status == "ok":
            _log(
                f"  [{category_type}] {category_name}: "
                f"{cat_summary['items']} 条目 / "
                f"{cat_summary['pages']} 页 / "
                f"已下载 {cat_summary['downloaded']} / "
                f"{cat_summary['duration_seconds']} 秒"
            )
            upload = cat_summary.get("upload")
            if upload is not None:
                _log(
                    f"    上传: 成功 {upload.get('success', 0)} / "
                    f"失败 {upload.get('failed', 0)} / "
                    f"跳过 {upload.get('skipped', 0)}"
                )
            elif cat_summary.get("upload_skipped"):
                _log("    跳过上传: 无已下载清单（未开启下载或无媒体 URL）")
            if not cat_summary.get("items"):
                reason = cat_summary.get("collect_stop_reason", "未知原因")
                _log(
                    f"    0 条目提示: {reason}；逐页明细见 "
                    f"{(output_dir / category_type / 'report.json').as_posix()}"
                )
            if warning := cat_summary.get("storage_warning"):
                _log(f"    存储/上传警告: {warning}")
            if cat_summary.get("download_skipped"):
                disk = cat_summary.get("disk") or {}
                _log(
                    f"    磁盘空间不足，已跳过下载: "
                    f"used={disk.get('used_percent')}%, "
                    f"free={disk.get('free_gb')} GB"
                )
        else:
            _log(
                f"  [{category_type}] {category_name}: "
                f"{status}: {cat_summary.get('error', '')}"
            )

    return (
        0
        if all(cat_summary.get("status") == "ok" for cat_summary in summary.values())
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
