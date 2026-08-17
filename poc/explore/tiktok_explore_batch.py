"""批量遍历多个 TikTok Explore 分类并采集 + 下载（单 client 共享 session）。"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
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
from src.explore.disk_check import check_disk_usage  # noqa: E402


DEFAULT_CATEGORIES = ""
DEFAULT_OUTPUT_DIR = Path(".output/explore/batch")
DEFAULT_CATEGORY_DELAY = 5.0
DEFAULT_MAX_PAGES = 2
DEFAULT_COUNT = 8
DEFAULT_DELAY = 2.0
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


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
                )
                for item in metadata:
                    item["category_name"] = category_name

                manifest: list[dict[str, Any]] = []
                if download and metadata:
                    if _disk_low_for_download(
                        category_summary,
                        category_dir,
                        disk_used_percent=disk_used_percent,
                        min_free_gb=min_free_gb,
                    ):
                        category_summary["download_skipped"] = "disk_low"
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

            save_json(category_dir / "metadata.json", metadata)
            save_json(category_dir / "report.json", report)
            if download:
                save_json(category_dir / "download_manifest.json", manifest)

            if persist_and_upload and metadata:
                try:
                    await persist_to_db(metadata, manifest)
                    if download and manifest:
                        from src.explore.upload import upload_pending_explore

                        await upload_pending_explore(
                            media_root=output_dir,
                            layout="batch",
                            category_type=category_type,
                            gateway=gateway,
                        )
                except Exception as exc:  # noqa: BLE001
                    category_summary["storage_warning"] = f"{type(exc).__name__}: {exc}"

            all_metadata.extend(metadata)
            all_manifest.extend(manifest)

            category_summary |= {
                "status": "ok",
                "items": len(metadata),
                "pages": len(report),
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
            print(
                "[提示] --list-categories 使用本地映射文件；如需联网刷新请单独运行 harvest_tiktok_session 后重试。"
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
    print(
        f"批量 Explore 完成: {len(categories)} 个分类, {total_items} 个条目, "
        f"已下载 {downloaded}/{total_manifest} 个媒体文件。"
    )
    for category_type, cat_summary in summary.items():
        status = cat_summary.get("status")
        category_name = cat_summary.get("category_name") or "-"
        if status == "ok":
            print(
                f"  [{category_type}] {category_name}: "
                f"{cat_summary['items']} 条目 / "
                f"{cat_summary['pages']} 页 / "
                f"已下载 {cat_summary['downloaded']} / "
                f"{cat_summary['duration_seconds']} 秒"
            )
            if cat_summary.get("download_skipped"):
                disk = cat_summary.get("disk") or {}
                print(
                    f"    磁盘空间不足，已跳过下载: "
                    f"used={disk.get('used_percent')}%, "
                    f"free={disk.get('free_gb')} GB"
                )
        else:
            print(
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
