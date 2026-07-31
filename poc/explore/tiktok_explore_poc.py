"""采集分页的 TikTok For You 推荐内容并下载其媒体。

本脚本验证 TikTok 的推荐端点会返回包含元数据和可播放媒体 URL 的
``itemList``。它会本地保存原始分页响应、所有采集到的条目以及扁平化
后的元数据，随后（可选）批量下载所选的媒体 URL。

在仓库根目录运行：
    uv run python poc/explore/tiktok_explore_poc.py

设置 ``TIKTOK_POC_PROXY`` 可覆盖本地代理；将其设为空字符串则以直连
方式测试。``TIKTOK_POC_COUNT``、``TIKTOK_POC_MAX_PAGES`` 和
``TIKTOK_POC_DELAY`` 分别覆盖默认的 20 条、5 页以及页间 1.5 秒的设定。

媒体下载默认开启，可通过以下变量调节：

- ``TIKTOK_POC_DOWNLOAD``：设为 ``0`` 关闭下载阶段（仅抓取）。
- ``TIKTOK_POC_URL_MODE``：``play_url``（默认）、``download_url`` 或
  ``all``，选择要下载的媒体 URL 字段。
- ``TIKTOK_POC_DOWNLOAD_DIR``：输出目录（默认
  ``.output/explore/downloads``）。
- ``TIKTOK_POC_CONCURRENCY``：并行下载数（默认 5）。
- ``TIKTOK_POC_MAX_RETRY``：单文件重试次数（默认 3）。
- ``TIKTOK_POC_CHUNK_KB``：流式分块大小，单位 KiB（默认 1024）。
- ``TIKTOK_POC_VERIFY``：设为 ``1`` 对本地已保存的元数据和下载内容做审计。

TikTok 的 CDN URL 带有签名并在数小时内失效，因此抓取后应尽快下载。
返回 403 表示链接已过期；请重新获取元数据。
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from time import time
from urllib.parse import quote, urlencode, urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.custom import DATA_HEADERS_TIKTOK, DOWNLOAD_HEADERS_TIKTOK  # noqa: E402
from src.encrypt import XBogus, XGnarly  # noqa: E402
from src.interface.template import APITikTok  # noqa: E402

SETTINGS_PATH = PROJECT_ROOT / "Volume" / "settings.json"
DEFAULT_PROXY = "http://127.0.0.1:64142"
DEFAULT_COUNT = 20
DEFAULT_MAX_PAGES = 5
DEFAULT_PAGE_DELAY = 1.5
ITEM_KEYS = ("itemList", "item_list", "items", "aweme_list")
RECOMMEND_ENDPOINT = "https://www.tiktok.com/api/recommend/item_list/"
RECOMMEND_FROM_PAGE = "foryou"

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / ".output" / "explore" / "samples"
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / ".output" / "explore" / "downloads"
DEFAULT_CONCURRENCY = 5
DEFAULT_MAX_RETRY = 3
DEFAULT_CHUNK_KB = 1024
URL_MODES = ("play_url", "download_url", "all")


def cookie_string(cookie: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookie.items())


def build_params(
    cookie: dict[str, str],
    device_id: str,
    from_page: str,
    cursor: int = 0,
    count: int = DEFAULT_COUNT,
) -> dict:
    return APITikTok.params | {
        "WebIdLastTime": int(time()),
        "device_id": device_id,
        "msToken": cookie.get("msToken", ""),
        "count": str(count),
        "cursor": str(cursor),
        "from_page": from_page,
    }


def signed_url(endpoint: str, params: dict, user_agent: str) -> str:
    query = urlencode(params, safe="=", quote_via=quote)
    x_bogus = XBogus().get_x_bogus(query, None, "GET", user_agent=user_agent)
    x_gnarly = XGnarly().generate(query, "", "GET", user_agent=user_agent)
    return f"{endpoint}?{query}&X-Bogus={x_bogus}&X-Gnarly={x_gnarly}"


def refresh_cookie(cookie: dict[str, str], response: httpx.Response) -> None:
    cookie.update(response.cookies.items())


def response_items(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for source in (payload, payload.get("data")):
        if not isinstance(source, dict):
            continue
        for key in ITEM_KEYS:
            if isinstance(items := source.get(key), list):
                return [item for item in items if isinstance(item, dict)]
    return []


def extract_pagination(
    payload: object,
    cursor: int = 0,
    count: int = DEFAULT_COUNT,
) -> tuple[int, bool]:
    if not isinstance(payload, dict):
        return 0, False
    for source in (payload, payload.get("data")):
        if not isinstance(source, dict):
            continue
        has_more = source.get("hasMore", source.get("has_more"))
        if has_more is None:
            continue
        try:
            next_cursor = int(source.get("cursor", cursor + count))
        except (TypeError, ValueError):
            next_cursor = cursor + count
        if isinstance(has_more, str):
            return next_cursor, has_more.lower() == "true"
        return next_cursor, bool(has_more)
    return 0, False


def first_url(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, str)), "")
    if isinstance(value, dict):
        for key in ("urlList", "url_list", "url"):
            if url := first_url(value.get(key)):
                return url
    return ""


def nested_dict(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def save_json(name: str, data: object) -> Path:
    path = DEFAULT_OUTPUT_DIR / f"tiktok_recommend_{name}.json"
    write_json(path, data)
    return path


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def flatten_item(item: dict) -> dict:
    author = nested_dict(item, "author")
    stats = nested_dict(item, "stats")
    video = nested_dict(item, "video")
    music = nested_dict(item, "music")
    challenges = item.get("challenges")
    hashtags = []
    if isinstance(challenges, list):
        hashtags = [
            challenge["title"]
            for challenge in challenges
            if isinstance(challenge, dict) and isinstance(challenge.get("title"), str)
        ]
    return {
        "id": item.get("id"),
        "description": item.get("desc"),
        "create_time": item.get("createTime", item.get("create_time")),
        "author_id": author.get("uniqueId", author.get("unique_id")),
        "author_nickname": author.get("nickname"),
        "play_count": stats.get("playCount", stats.get("play_count")),
        "like_count": stats.get("diggCount", stats.get("digg_count")),
        "share_count": stats.get("shareCount", stats.get("share_count")),
        "comment_count": stats.get("commentCount", stats.get("comment_count")),
        "play_url": first_url(video.get("playAddr") or video.get("play_addr")),
        "download_url": first_url(
            video.get("downloadAddr") or video.get("download_addr")
        ),
        "music_title": music.get("title"),
        "music_url": first_url(music.get("playUrl") or music.get("play_url")),
        "hashtags": hashtags,
    }


def print_item_summary(item: dict) -> None:
    author = nested_dict(item, "author")
    stats = nested_dict(item, "stats")
    video = nested_dict(item, "video")
    play_url = first_url(video.get("playAddr") or video.get("play_addr"))
    download_url = first_url(video.get("downloadAddr") or video.get("download_addr"))
    media_url = play_url or download_url
    print(f"  样本 ID: {item.get('id', '-')}")
    print(f"  作者: {author.get('uniqueId', author.get('nickname', '-'))}")
    print(
        "  统计: "
        f"播放={stats.get('playCount', stats.get('play_count', '-'))}, "
        f"点赞={stats.get('diggCount', stats.get('digg_count', '-'))}, "
        f"分享={stats.get('shareCount', stats.get('share_count', '-'))}"
    )
    print(f"  播放 URL 可用: {'是' if play_url else '否'}")
    print(f"  下载 URL 可用: {'是' if download_url else '否'}")
    print(f"  媒体主机: {urlparse(media_url).netloc if media_url else '-'}")


def headers(cookie: dict[str, str]) -> dict[str, str]:
    return DATA_HEADERS_TIKTOK | {
        "Cookie": cookie_string(cookie),
        "Referer": "https://www.tiktok.com/explore",
    }


def download_headers(user_agent: str) -> dict[str, str]:
    """TikTok CDN 所用的请求头，携带会话 User-Agent 用于签名。"""
    return DOWNLOAD_HEADERS_TIKTOK | {"User-Agent": user_agent}


def select_urls(item: dict, mode: str) -> list[tuple[str, str]]:
    """返回所选模式下的 ``(标签, url)`` 配对，跳过空 URL。"""
    pairs = []
    if mode in ("play_url", "all") and (url := item.get("play_url")):
        pairs.append(("play", url))
    if mode in ("download_url", "all") and (url := item.get("download_url")):
        pairs.append(("download", url))
    return pairs


def safe_name(value: object) -> str:
    """构造文件系统安全的目录名。"""
    base = str(value or "unknown")
    # 保留字母/数字和安全标点；其余字符替换为 "_"。
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in base)


def author_directory_name(item: dict) -> str:
    return safe_name(item.get("author_id") or item.get("author_nickname"))


def item_directory_name(item: dict) -> str:
    return (
        Path(author_directory_name(item)) / str(item.get("id") or "unknown")
    ).as_posix()


def verify_downloads(metadata: list[dict], mode: str, download_dir: Path) -> dict:
    report = {
        "matched": [],
        "missing": [],
        "invalid": [],
        "orphan_directories": [],
        "orphan_files": [],
    }
    expected_item_directories = {Path(item_directory_name(item)) for item in metadata}
    expected_author_directories = {
        directory.parent for directory in expected_item_directories
    }

    for item in metadata:
        item_id = item.get("id")
        directory_name = item_directory_name(item)
        item_dir = download_dir / directory_name
        metadata_path = item_dir / "metadata.json"
        result = {"id": item_id, "item_directory": directory_name}
        if not item_dir.is_dir():
            report["missing"].append(result | {"reason": "missing_item_directory"})
            continue
        if not metadata_path.is_file():
            report["missing"].append(result | {"reason": "missing_metadata"})
            continue
        try:
            sidecar = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report["invalid"].append(result | {"reason": "invalid_metadata"})
            continue
        if not isinstance(sidecar, dict) or sidecar.get("id") != item_id:
            report["invalid"].append(result | {"reason": "metadata_id_mismatch"})
            continue

        expected_media = [
            (label, item_dir / f"{label}.mp4") for label, _ in select_urls(item, mode)
        ]
        if empty_media := [
            path.name
            for _, path in expected_media
            if path.is_file() and not path.stat().st_size
        ]:
            report["invalid"].append(
                result | {"reason": f"empty_media:{','.join(empty_media)}"}
            )
            continue
        if missing_media := [
            path.name for _, path in expected_media if not path.is_file()
        ]:
            report["missing"].append(
                result | {"reason": f"missing_media:{','.join(missing_media)}"}
            )
            continue
        report["matched"].append(result)

    if download_dir.is_dir():
        for path in sorted(
            download_dir.rglob("*"),
            key=lambda entry: entry.relative_to(download_dir).as_posix(),
        ):
            relative_path = path.relative_to(download_dir)
            if path.is_dir() and relative_path not in (
                expected_author_directories | expected_item_directories
            ):
                report["orphan_directories"].append(relative_path.as_posix())
            elif path.is_file() and relative_path.parent == Path("."):
                report["orphan_files"].append(relative_path.name)

    return report | {"counts": {key: len(value) for key, value in report.items()}}


async def download_one(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    headers: dict[str, str],
    max_retry: int,
    chunk_size: int,
) -> bool:
    """将单个媒体 URL 流式写入 ``dest``，支持基于 Range 的断点续传与重试。"""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  跳过（已存在）: {dest.name}")
        return True
    temp = dest.with_suffix(dest.suffix + ".downloading")
    for attempt in range(1, max_retry + 1):
        position = temp.stat().st_size if temp.exists() else 0
        try:
            async with client.stream(
                "GET",
                url,
                headers=headers | ({"Range": f"bytes={position}-"} if position else {}),
            ) as response:
                response.raise_for_status()
                # 200 表示服务器忽略了 Range：从头开始写入。
                mode = "ab" if response.status_code == 206 and position else "wb"
                with open(temp, mode) as file:
                    async for chunk in response.aiter_bytes(chunk_size):
                        file.write(chunk)
            temp.replace(dest)
            print(f"  完成: {dest.name} ({dest.stat().st_size} 字节)")
            return True
        except httpx.HTTPStatusError as error:
            print(f"  HTTP {error.response.status_code}: {dest.name}")
            if error.response.status_code in (403, 404, 410):
                print("  链接可能已过期；请重新获取元数据。")
                return False
        except httpx.RequestError as error:
            print(f"  重试 {attempt}/{max_retry} {dest.name}: {error}")
    return False


async def download_phase(
    client: httpx.AsyncClient,
    metadata: list[dict],
    mode: str,
    download_dir: Path,
    concurrency: int,
    max_retry: int,
    chunk_size: int,
    user_agent: str,
) -> None:
    download_dir.mkdir(parents=True, exist_ok=True)
    headers = download_headers(user_agent)
    semaphore = asyncio.Semaphore(concurrency)

    async def worker(url: str, dest: Path) -> bool:
        async with semaphore:
            return await download_one(client, url, dest, headers, max_retry, chunk_size)

    manifest = []
    file_records = []
    tasks = []
    for item in metadata:
        directory_name = item_directory_name(item)
        item_dir = download_dir / directory_name
        item_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = item_dir / "metadata.json"
        write_json(metadata_path, item)
        files = []
        manifest.append(
            {
                "id": item.get("id"),
                "author_id": item.get("author_id"),
                "item_directory": directory_name,
                "metadata_path": metadata_path.relative_to(download_dir).as_posix(),
                "metadata": item,
                "files": files,
            }
        )
        for label, url in select_urls(item, mode):
            dest = item_dir / f"{label}.mp4"
            file_record = {
                "url_label": label,
                "media_path": dest.relative_to(download_dir).as_posix(),
                "bytes": None,
                "ok": False,
            }
            files.append(file_record)
            file_records.append((file_record, dest))
            tasks.append(worker(url, dest))

    if tasks:
        print(
            f"\n下载: {len(tasks)} 个文件 -> {download_dir} "
            f"(mode={mode}, concurrency={concurrency})"
        )
        results = await asyncio.gather(*tasks)
    else:
        print("下载: 没有可下载的媒体 URL。")
        results = []
    for (file_record, dest), ok in zip(file_records, results):
        file_record["ok"] = ok
        if dest.is_file():
            file_record["bytes"] = dest.stat().st_size
    ok = sum(1 for r in results if r)
    print(f"下载汇总: 成功 {ok} 个，失败 {len(results) - ok} 个。")
    print(f"已保存下载清单: {save_json('download_manifest', manifest)}")


async def probe_profile(
    client: httpx.AsyncClient,
    cookie: dict[str, str],
) -> bool:
    response = await client.get(
        "https://www.tiktok.com/@elsebasmadridista",
        headers=headers(cookie) | {"Referer": "https://www.tiktok.com/"},
    )
    refresh_cookie(cookie, response)
    content_type = response.headers.get("content-type", "?")
    print(
        "资料页对照: "
        f"status={response.status_code}, content-type={content_type}, "
        f"bytes={len(response.content)}"
    )
    return response.is_success and bool(response.content)


async def fetch_recommend_page(
    client: httpx.AsyncClient,
    cookie: dict[str, str],
    device_id: str,
    user_agent: str,
    cursor: int,
    count: int,
    page: int,
) -> dict | None:
    response = await client.get(
        signed_url(
            RECOMMEND_ENDPOINT,
            build_params(
                cookie,
                device_id,
                RECOMMEND_FROM_PAGE,
                cursor=cursor,
                count=count,
            ),
            user_agent,
        ),
        headers=headers(cookie),
    )
    refresh_cookie(cookie, response)
    content_type = response.headers.get("content-type", "?")
    print(
        f"第 {page} 页: status={response.status_code}, content-type={content_type}, "
        f"bytes={len(response.content)}"
    )
    if "application/json" not in content_type:
        print("结果: 非 JSON")
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        print("结果: JSON 无效")
        return None
    return payload if isinstance(payload, dict) else None


async def fetch_recommend(
    client: httpx.AsyncClient,
    cookie: dict[str, str],
    device_id: str,
    user_agent: str,
    max_pages: int,
    count: int,
    delay: float,
) -> tuple[list[dict], list[dict], int, bool]:
    cursor = 0
    has_more = True
    items: list[dict] = []
    payloads: list[dict] = []
    pages_fetched = 0

    for page in range(1, max_pages + 1):
        payload = await fetch_recommend_page(
            client,
            cookie,
            device_id,
            user_agent,
            cursor,
            count,
            page,
        )
        if payload is None:
            break
        payloads.append(payload)
        page_items = response_items(payload)
        next_cursor, has_more = extract_pagination(payload, cursor, count)
        if not page_items:
            print("结果: 没有 itemList")
            break

        items.extend(page_items)
        pages_fetched = page
        print(f"结果: {len(page_items)} 条推荐，累计={len(items)}, hasMore={has_more}")
        if not has_more:
            break
        if next_cursor == cursor:
            print("结果: 游标未前进；停止以避免重复翻页。")
            break
        cursor = next_cursor
        if page < max_pages:
            await asyncio.sleep(delay)

    return items, payloads, pages_fetched, has_more


def positive_int_from_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


def nonnegative_float_from_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError:
        return default
    return value if value >= 0 else default


def url_mode_from_env(name: str, default: str = "play_url") -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in URL_MODES else default


async def main() -> int:
    url_mode = url_mode_from_env("TIKTOK_POC_URL_MODE")
    download_dir = Path(os.getenv("TIKTOK_POC_DOWNLOAD_DIR", str(DEFAULT_DOWNLOAD_DIR)))
    verify_only = os.getenv("TIKTOK_POC_VERIFY", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if verify_only:
        metadata_path = DEFAULT_OUTPUT_DIR / "tiktok_recommend_metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"致命错误: 无法读取已保存的元数据: {error}")
            return 2
        if not isinstance(metadata, list) or not all(
            isinstance(item, dict) for item in metadata
        ):
            print("致命错误: 已保存的元数据必须是 JSON 对象数组")
            return 2
        report = verify_downloads(metadata, url_mode, download_dir)
        print(f"已保存校验报告: {save_json('verify_report', report)}")
        print(
            "校验汇总: "
            + ", ".join(f"{key}={value}" for key, value in report["counts"].items())
        )
        return 0

    if not SETTINGS_PATH.is_file():
        print(f"致命错误: 未找到配置文件: {SETTINGS_PATH}")
        return 2
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    cookie = dict(settings.get("cookie_tiktok", {}))
    browser_info = settings.get("browser_info_tiktok", {})
    device_id = browser_info.get("device_id", "")
    user_agent = browser_info.get("User-Agent", "")
    if not cookie.get("sessionid") or not device_id or not user_agent:
        print("致命错误: 缺少 cookie_tiktok、device_id 或 User-Agent")
        return 2

    proxy = os.getenv("TIKTOK_POC_PROXY", DEFAULT_PROXY) or None
    count = positive_int_from_env("TIKTOK_POC_COUNT", DEFAULT_COUNT)
    max_pages = positive_int_from_env("TIKTOK_POC_MAX_PAGES", DEFAULT_MAX_PAGES)
    delay = nonnegative_float_from_env("TIKTOK_POC_DELAY", DEFAULT_PAGE_DELAY)
    download_enabled = os.getenv("TIKTOK_POC_DOWNLOAD", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    concurrency = positive_int_from_env("TIKTOK_POC_CONCURRENCY", DEFAULT_CONCURRENCY)
    max_retry = positive_int_from_env("TIKTOK_POC_MAX_RETRY", DEFAULT_MAX_RETRY)
    chunk_size = positive_int_from_env("TIKTOK_POC_CHUNK_KB", DEFAULT_CHUNK_KB) * 1024
    print(
        f"初始化: proxy={proxy or 'direct'}, sessionid=yes, "
        f"msToken={'yes' if cookie.get('msToken') else 'no'}, device_id={device_id}, "
        f"count={count}, max_pages={max_pages}, delay={delay}s"
    )
    print(
        f"下载: enabled={download_enabled}, url_mode={url_mode}, "
        f"dir={download_dir}, concurrency={concurrency}, max_retry={max_retry}, "
        f"chunk={chunk_size // 1024}KiB"
    )
    async with httpx.AsyncClient(
        timeout=30,
        proxy=proxy,
        follow_redirects=True,
        verify=False,
        headers={"User-Agent": user_agent},
    ) as client:
        try:
            await probe_profile(client, cookie)
            items, payloads, pages_fetched, has_more = await fetch_recommend(
                client,
                cookie,
                device_id,
                user_agent,
                max_pages,
                count,
                delay,
            )
            metadata = [flatten_item(item) for item in items] if items else []
            if download_enabled and metadata:
                await download_phase(
                    client,
                    metadata,
                    url_mode,
                    download_dir,
                    concurrency,
                    max_retry,
                    chunk_size,
                    user_agent,
                )
        except httpx.RequestError as error:
            print(f"网络错误: {type(error).__name__}: {error}")
            return 2

    print("\n=== 结论 ===")
    if items:
        print(f"已保存原始分页: {save_json('pages', payloads)}")
        print(f"已保存 itemList: {save_json('items', items)}")
        print(f"已保存扁平化元数据: {save_json('metadata', metadata)}")
        print_item_summary(items[0])
        author_ids = {item["author_id"] for item in metadata if item["author_id"]}
        print(
            f"汇总: pages={pages_fetched}, items={len(items)}, authors={len(author_ids)}"
        )
        if has_more and pages_fetched == max_pages:
            print("已停止: 已达到 TIKTOK_POC_MAX_PAGES。")
        elif has_more:
            print("已停止: 在 TikTok 报告最后一页之前分页已结束。")
        else:
            print("已停止: TikTok 报告没有更多推荐。")
        print("放行: 推荐响应包含元数据和媒体 URL。")
        return 0
    print("终止: 推荐端点未返回 itemList。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
