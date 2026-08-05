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
    DEFAULT_PROXY,
    DEFAULT_SETTINGS_PATH,
    load_device_id,
    load_session,
)


DEFAULT_CATEGORIES = "119,120,123"
DEFAULT_OUTPUT_DIR = Path(".output/explore/batch")
DEFAULT_CATEGORY_DELAY = 5.0
DEFAULT_MAX_PAGES = 2
DEFAULT_COUNT = 8
DEFAULT_DELAY = 2.0


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


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
    device_id: str,
    browser_request_params: Mapping[str, str],
    browser_templates: tuple[list[tuple[str, str]], list[tuple[str, str]]] | None,
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
    cookie: dict[str, str],
    user_agent: str,
    proxy: str | None,
    persist_and_upload: bool = False,
    gateway: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """串行遍历分类，每个分类使用独立 AsyncClient；单分类失败不中断后续。"""
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
        category_summary: dict[str, Any] = {
            "category_type": category_type,
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

                manifest: list[dict[str, Any]] = []
                if download and metadata:
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
    parser.add_argument("--download", action="store_true", help="下载媒体文件。")
    parser.add_argument("--categories", default=DEFAULT_CATEGORIES)
    parser.add_argument("--count", type=positive_int, default=DEFAULT_COUNT)
    parser.add_argument("--max-pages", type=positive_int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--delay", type=nonnegative_float, default=DEFAULT_DELAY)
    parser.add_argument(
        "--category-delay", type=nonnegative_float, default=DEFAULT_CATEGORY_DELAY
    )
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--url-mode",
        choices=URL_MODES,
        default="play_url",
    )
    parser.add_argument("--concurrency", type=positive_int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-retry", type=positive_int, default=DEFAULT_MAX_RETRY)
    parser.add_argument("--chunk-kb", type=positive_int, default=DEFAULT_CHUNK_KB)
    parser.add_argument("--proxy", default=os.getenv("TIKTOK_POC_PROXY", DEFAULT_PROXY))
    parser.add_argument(
        "--no-db", action="store_true", help="跳过 media-pipeline 数据库存储。"
    )
    return parser.parse_args(argv)


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    args = parse_args()

    if not args.live:
        print("未指定 --live，拒绝联网。")
        return 2

    categories = parse_categories(args.categories)
    if not categories:
        print("错误: --categories 为空。")
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
    if not args.no_db:
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
                device_id=device_id,
                browser_request_params=browser_request_params,
                browser_templates=browser_templates,
                count=args.count,
                max_pages=args.max_pages,
                delay=args.delay,
                category_delay=args.category_delay,
                output_dir=args.output_dir,
                download=args.download,
                url_mode=args.url_mode,
                concurrency=args.concurrency,
                max_retry=args.max_retry,
                chunk_size=args.chunk_kb * 1024,
                cookie=cookie,
                user_agent=user_agent,
                proxy=args.proxy or None,
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

    args.output_dir.mkdir(parents=True, exist_ok=True)
    save_json(args.output_dir / "all_metadata.json", metadata)
    save_json(args.output_dir / "all_manifest.json", manifest)
    save_json(args.output_dir / "summary.json", summary)

    total_items = len(metadata)
    total_manifest = len(manifest)
    downloaded = sum(record["ok"] for record in manifest)
    print(
        f"批量 Explore 完成: {len(categories)} 个分类, {total_items} 个条目, "
        f"已下载 {downloaded}/{total_manifest} 个媒体文件。"
    )
    for category_type, cat_summary in summary.items():
        status = cat_summary.get("status")
        if status == "ok":
            print(
                f"  [{category_type}] {cat_summary['items']} 条目 / "
                f"{cat_summary['pages']} 页 / "
                f"已下载 {cat_summary['downloaded']} / "
                f"{cat_summary['duration_seconds']} 秒"
            )
        else:
            print(f"  [{category_type}] {status}: {cat_summary.get('error', '')}")

    return (
        0
        if all(cat_summary.get("status") == "ok" for cat_summary in summary.values())
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
