import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 跨项目复用 media-pipeline 的 SQL 存储与上传模块（硬编码路径，见方案约定）。
MEDIA_PIPELINE_ROOT = Path(r"C:\Users\admin\Documents\GitHub\scrapy_server异步")
sys.path.insert(0, str(MEDIA_PIPELINE_ROOT))

from poc.explore._common import (  # noqa: E402
    DEFAULT_CHUNK_KB,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_RETRY,
    download_headers,
    download_one,
    flatten_item,
    headers,
    item_directory_name,
    select_urls,
)
from poc.explore.tiktok_explore_replay import (  # noqa: E402
    DEFAULT_HAR_PATH,
    DEFAULT_PROXY,
    DEFAULT_SETTINGS_PATH,
    EXPLORE_ITEM_LIST_ENDPOINT,
    captured_explore_requests,
    load_device_id,
    load_session,
)
from src.encrypt import XBogus, XGnarly  # noqa: E402
from src.interface.template import APITikTok  # noqa: E402


DEFAULT_CATEGORY_TYPE = "120"
DEFAULT_COUNT = 8
DEFAULT_MAX_PAGES = 2
DEFAULT_DELAY = 1.5
DEFAULT_OUTPUT_DIR = Path(".output/explore/signed")
SIGNATURE_FIELDS = {"x-bogus", "x-gnarly", "x-dynosaur"}
BROWSER_REQUEST_FIELDS = (
    "clientABVersions",
    "odinId",
    "verifyFp",
    "is_new_user",
    "video_encoding",
)
EXPLORE_TEMPLATE_KEYS = ("explore_initial_template", "explore_next_template")


def load_browser_request_params(settings_path: Path) -> dict[str, str]:
    """读取浏览器实际 Explore 请求中可复用的身份字段。"""
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, Mapping):
        raise ValueError("settings 根节点必须是对象")
    browser_info = settings.get("browser_info_tiktok")
    if not isinstance(browser_info, Mapping):
        return {}
    return {
        field: value
        for field in BROWSER_REQUEST_FIELDS
        if isinstance((value := browser_info.get(field)), str) and value
    }


def load_explore_templates(
    settings_path: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]] | None:
    """读取诊断阶段捕获的首屏与翻页 query 模板。"""
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, Mapping):
        raise ValueError("settings 根节点必须是对象")
    browser_info = settings.get("browser_info_tiktok")
    if not isinstance(browser_info, Mapping):
        return None

    templates: list[list[tuple[str, str]]] = []
    for key in EXPLORE_TEMPLATE_KEYS:
        raw_template = browser_info.get(key)
        if not isinstance(raw_template, list):
            return None
        template = [
            (name, value)
            for item in raw_template
            if isinstance(item, list)
            and len(item) == 2
            and isinstance((name := item[0]), str)
            and isinstance((value := item[1]), str)
        ]
        if not template:
            return None
        templates.append(template)
    return templates[0], templates[1]


def build_base_params(
    device_id: str,
    browser_request_params: Mapping[str, str] | None = None,
    from_page: str | None = None,
) -> list[tuple[str, str]]:
    """构造不依赖 HAR 的 Explore 参数模板。"""
    params = APITikTok.params | {
        "device_id": device_id,
    }
    params.pop("from_page", None)
    params.pop("msToken", None)
    if from_page is not None:
        params["from_page"] = from_page
    if browser_request_params:
        for field in BROWSER_REQUEST_FIELDS:
            if value := browser_request_params.get(field):
                params[field] = value
    return [(str(name), str(value)) for name, value in params.items()]


def build_explore_params(
    template: Sequence[tuple[str, str]],
    *,
    category_type: str,
    pull_type: str,
    cursor: str,
    count: int,
    ms_token: str,
) -> list[tuple[str, str]]:
    """复用抓取到的参数结构，仅替换其中可变字段。"""
    replacements = {
        "categorytype": category_type,
        "pulltype": pull_type,
        "cursor": cursor,
        "count": str(count),
        "mstoken": ms_token,
        "webidlasttime": str(int(time())),
    }
    params: list[tuple[str, str]] = []
    present: set[str] = set()
    for name, value in template:
        normalized = name.lower()
        if normalized in SIGNATURE_FIELDS:
            continue
        if normalized == "cursor" and not cursor:
            continue
        if normalized == "mstoken" and not ms_token:
            continue
        params.append((name, replacements.get(normalized, value)))
        present.add(normalized)
    for name in ("categoryType", "pullType", "count"):
        if name.lower() not in present:
            params.append((name, replacements[name.lower()]))
    if cursor and "cursor" not in present:
        params.append(("cursor", cursor))
    if ms_token and "mstoken" not in present:
        params.append(("msToken", ms_token))
    return params


def signed_url(params: Sequence[tuple[str, str]], user_agent: str) -> str:
    query = urlencode(params, safe="=", quote_via=quote)
    x_bogus = XBogus().get_x_bogus(query, None, "GET", user_agent=user_agent)
    x_gnarly = XGnarly().generate(query, "", "GET", user_agent=user_agent)
    return f"{EXPLORE_ITEM_LIST_ENDPOINT}?{query}&X-Bogus={x_bogus}&X-Gnarly={x_gnarly}"


def extract_explore_pagination(
    payload: Mapping[str, Any], current_cursor: str
) -> tuple[str, bool]:
    cursor = payload.get("cursor", current_cursor)
    has_more = payload.get("hasMore", payload.get("has_more", False))
    return str(cursor), has_more.lower() == "true" if isinstance(
        has_more, str
    ) else bool(has_more)


def flatten_explore_item(item: dict[str, Any], category_type: str) -> dict[str, Any]:
    return flatten_item(item) | {"category_type": category_type}


def select_template(
    requests: Sequence[Mapping[str, Any]], category_type: str, pull_type: str
) -> Mapping[str, Any]:
    for request in requests:
        if (
            request.get("category_type") == category_type
            and request.get("pull_type") == pull_type
        ):
            return request
    raise ValueError(
        f"没有 categoryType={category_type} 对应的 pullType={pull_type} 抓包请求"
    )


def select_initial_template(
    requests: Sequence[Mapping[str, Any]], category_type: str
) -> tuple[Mapping[str, Any], str]:
    try:
        return select_template(requests, category_type, "1"), "1"
    except ValueError:
        return select_template(requests, category_type, "2"), "2"


def initial_cursor(template: Mapping[str, Any]) -> str:
    params = template.get("params", [])
    if not isinstance(params, list):
        return ""
    return next((value for name, value in params if name == "cursor"), "")


def refresh_session(cookie: dict[str, str], response: httpx.Response) -> str:
    """刷新 cookie，并返回本响应新下发的 query msToken。"""
    response_cookies = dict(response.cookies.items())
    cookie.update(response_cookies)
    if ms_token := response.headers.get("x-ms-token"):
        cookie["msToken"] = ms_token
        return ms_token
    return response_cookies.get("msToken", "")


async def fetch_explore_page(
    client: httpx.AsyncClient,
    *,
    template: Mapping[str, Any],
    category_type: str,
    pull_type: str,
    cursor: str,
    count: int,
    cookie: dict[str, str],
    ms_token: str,
    user_agent: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    params = template.get("params")
    if not isinstance(params, list):
        raise ValueError("缺少抓包请求的参数")
    request_params = build_explore_params(
        params,
        category_type=category_type,
        pull_type=pull_type,
        cursor=cursor,
        count=count,
        ms_token=ms_token,
    )
    response = await client.get(
        signed_url(request_params, user_agent),
        headers=headers(cookie, "https://www.tiktok.com/explore"),
    )
    next_ms_token = refresh_session(cookie, response)
    summary: dict[str, Any] = {
        "pull_type": pull_type,
        "http_status": response.status_code,
        "json": "application/json" in response.headers.get("content-type", ""),
        "request_field_names": [name for name, _ in request_params],
        "request_cursor_present": any(
            name.lower() == "cursor" for name, _ in request_params
        ),
        "request_ms_token_present": any(
            name.lower() == "mstoken" for name, _ in request_params
        ),
    }
    if not summary["json"]:
        return None, summary, next_ms_token
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None, summary | {"json": False}, next_ms_token
    if not isinstance(payload, dict):
        return None, summary, next_ms_token
    item_list = payload.get("itemList")
    items = (
        [item for item in item_list if isinstance(item, dict)]
        if isinstance(item_list, list)
        else []
    )
    next_cursor, has_more = extract_explore_pagination(payload, cursor)
    summary |= {
        "item_count": len(items),
        "item_id_hashes": [
            hashlib.sha256(item["id"].encode()).hexdigest()[:12]
            for item in items
            if isinstance(item.get("id"), str)
        ],
        "media_url_count": sum(
            bool(
                flatten_item(item).get("play_url")
                or flatten_item(item).get("download_url")
            )
            for item in items
        ),
        "has_more": has_more,
        "cursor_sha256": hashlib.sha256(next_cursor.encode()).hexdigest()[:12],
        "received_ms_token": bool(next_ms_token),
    }
    return payload, summary, next_ms_token


async def collect_explore(
    client: httpx.AsyncClient,
    *,
    initial_template: Mapping[str, Any],
    next_template: Mapping[str, Any],
    category_type: str,
    count: int,
    max_pages: int,
    delay: float,
    cookie: dict[str, str],
    user_agent: str,
    initial_pull_type: str = "1",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cursor = initial_cursor(initial_template)
    next_template_has_cursor = any(
        name.lower() == "cursor"
        for name, _ in next_template.get("params", [])
        if isinstance(name, str)
    )
    request_ms_token = ""
    seen_ids: set[str] = set()
    metadata: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        pull_type = initial_pull_type if page == 1 else "2"
        template = initial_template if page == 1 else next_template
        payload, summary, next_ms_token = await fetch_explore_page(
            client,
            template=template,
            category_type=category_type,
            pull_type=pull_type,
            cursor=cursor,
            count=count,
            cookie=cookie,
            ms_token=request_ms_token,
            user_agent=user_agent,
        )
        summary["page"] = page
        report.append(summary)
        if payload is None:
            break
        # 浏览器首屏响应的 x-ms-token 不会进入第 2 页 query；第 2 页响应后才开始使用。
        if page > 1 and next_ms_token:
            request_ms_token = next_ms_token
        item_list = payload.get("itemList")
        if not isinstance(item_list, list):
            break
        new_item_count = 0
        for item in item_list:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            if item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            metadata.append(flatten_explore_item(item, category_type))
            new_item_count += 1

        next_cursor, has_more = extract_explore_pagination(payload, cursor)
        if not has_more:
            break
        if page > 1 and not new_item_count:
            break
        if next_template_has_cursor:
            cursor = (
                initial_cursor(next_template)
                if page == 1 and next_cursor == cursor
                else next_cursor
            )
        else:
            cursor = ""
        if page < max_pages:
            await asyncio.sleep(delay)

    return metadata, report


async def download_media(
    client: httpx.AsyncClient,
    metadata: Sequence[dict[str, Any]],
    *,
    output_dir: Path,
    mode: str,
    concurrency: int,
    max_retry: int,
    chunk_size: int,
    user_agent: str,
) -> list[dict[str, Any]]:
    download_dir = output_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    manifest: list[dict[str, Any]] = []

    async def download(url: str, destination: Path) -> bool:
        async with semaphore:
            return await download_one(
                client,
                url,
                destination,
                download_headers(user_agent),
                max_retry,
                chunk_size,
            )

    tasks: list[asyncio.Task[bool]] = []
    records: list[tuple[dict[str, Any], Path]] = []
    for item in metadata:
        item_dir = download_dir / item_directory_name(item)
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "metadata.json").write_text(
            json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for label, url in select_urls(item, mode):
            destination = item_dir / f"{item.get('id')}.mp4"
            record = {
                "id": item.get("id"),
                "category_type": item.get("category_type"),
                "label": label,
                "media_path": destination.relative_to(output_dir).as_posix(),
                "ok": False,
                "bytes": None,
            }
            manifest.append(record)
            records.append((record, destination))
            tasks.append(asyncio.create_task(download(url, destination)))
    for (record, destination), ok in zip(records, await asyncio.gather(*tasks)):
        record["ok"] = ok
        if destination.is_file():
            record["bytes"] = destination.stat().st_size
    return manifest


async def persist_to_db(
    metadata: Sequence[dict[str, Any]], manifest: Sequence[dict[str, Any]]
) -> None:
    """将采集元数据与下载结果持久化到 tiktok 库（media-pipeline 模块）。

    失败由调用方捕获，不影响 JSON 落盘。采集侧统一指定 interxt 作为上传后端，
    per-video 的 s3_prefix 留给上传消费器分配。
    """
    from media_pipeline.repositories.base import get_tiktok_db_session, init_tiktok_db
    from media_pipeline.repositories.tiktok_explore_repository import (
        TiktokExploreItemRepository,
    )

    await init_tiktok_db()
    async with get_tiktok_db_session() as session:
        repo = TiktokExploreItemRepository(session)
        await repo.upsert_batch(metadata, default_s3_provider="interxt")
        for record in manifest:
            await repo.update_media(
                record["id"],
                record["media_path"],
                record["bytes"],
                int(record["ok"]),
            )


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须为正整数") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return parsed


def nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须为非负数") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须为非负数")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对抓包的 TikTok Explore 分类进行可选式纯签名采集。"
    )
    parser.add_argument("--live", action="store_true", help="发送签名请求。")
    parser.add_argument("--download", action="store_true", help="下载返回的媒体。")
    parser.add_argument("--category-type", default=DEFAULT_CATEGORY_TYPE)
    parser.add_argument("--count", type=positive_int, default=DEFAULT_COUNT)
    parser.add_argument("--max-pages", type=positive_int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--delay", type=nonnegative_float, default=DEFAULT_DELAY)
    parser.add_argument("--har", type=Path, default=DEFAULT_HAR_PATH)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--url-mode", choices=("play_url", "download_url", "all"), default="play_url"
    )
    parser.add_argument("--concurrency", type=positive_int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-retry", type=positive_int, default=DEFAULT_MAX_RETRY)
    parser.add_argument("--chunk-kb", type=positive_int, default=DEFAULT_CHUNK_KB)
    parser.add_argument("--proxy", default=os.getenv("TIKTOK_POC_PROXY", DEFAULT_PROXY))
    return parser.parse_args()


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(MEDIA_PIPELINE_ROOT / ".env")
    except ImportError:
        pass
    args = parse_args()
    if not args.live:
        print("未指定 --live，拒绝联网。")
        return 2
    try:
        cookie, user_agent = load_session(args.settings)
        if args.har.is_file():
            har = json.loads(args.har.read_text(encoding="utf-8"))
            if not isinstance(har, Mapping):
                raise ValueError("HAR 根节点必须是对象")
            requests = captured_explore_requests(har)
            initial_template, initial_pull_type = select_initial_template(
                requests, args.category_type
            )
            next_template = select_template(requests, args.category_type, "2")
        else:
            if templates := load_explore_templates(args.settings):
                initial_template = {"params": templates[0]}
                next_template = {"params": templates[1]}
            else:
                initial_template = {
                    "params": build_base_params(
                        load_device_id(args.settings),
                        load_browser_request_params(args.settings),
                    )
                }
                next_template = {
                    "params": build_base_params(
                        load_device_id(args.settings),
                        load_browser_request_params(args.settings),
                        from_page="",
                    )
                }
            initial_pull_type = "1"
    except (OSError, ValueError, json.JSONDecodeError):
        print("错误: 无法加载本地 Explore 输入。")
        return 2
    if not args.har.is_file():
        print("未找到 HAR，使用合成 Explore 参数模板。")

    async def run() -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
    ]:
        async with httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            cookies=cookie,
            follow_redirects=True,
            proxy=args.proxy or None,
            timeout=30,
            verify=False,
        ) as client:
            metadata, report = await collect_explore(
                client,
                initial_template=initial_template,
                next_template=next_template,
                category_type=args.category_type,
                count=args.count,
                max_pages=args.max_pages,
                delay=args.delay,
                cookie=cookie,
                user_agent=user_agent,
                initial_pull_type=initial_pull_type,
            )
            manifest = (
                await download_media(
                    client,
                    metadata,
                    output_dir=args.output_dir,
                    mode=args.url_mode,
                    concurrency=args.concurrency,
                    max_retry=args.max_retry,
                    chunk_size=args.chunk_kb * 1024,
                    user_agent=user_agent,
                )
                if args.download and metadata
                else []
            )
        try:
            await persist_to_db(metadata, manifest)
        except Exception as exc:
            print(
                f"警告: 持久化到 tiktok 库失败（JSON 照常落盘）: "
                f"{type(exc).__name__}: {exc}"
            )
        return metadata, report, manifest

    try:
        metadata, report, manifest = asyncio.run(run())
    except httpx.HTTPError as error:
        print(f"网络错误: {type(error).__name__}")
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.download:
        (args.output_dir / "download_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    downloaded = sum(record["ok"] for record in manifest)
    print(
        f"Explore 分类 {args.category_type}: {len(metadata)} 个条目，"
        f"{len(report)} 页，已下载 {downloaded}/{len(manifest)} 个媒体文件。"
    )
    return 0 if metadata and (not args.download or downloaded == len(manifest)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
