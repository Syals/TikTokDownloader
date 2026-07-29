"""Collect paginated TikTok For You recommendations and download their media.

The script verifies that TikTok's recommendation endpoint returns an
``itemList`` containing metadata and playable media URLs. It saves raw page
responses, all collected items, and flattened metadata locally, then
(optionally) batch-downloads the selected media URLs.

Run from the repository root:
    uv run python poc/tiktok_explore_poc.py

Set ``TIKTOK_POC_PROXY`` to override the local proxy. Set it to an empty
string to test a direct connection. ``TIKTOK_POC_COUNT``,
``TIKTOK_POC_MAX_PAGES``, and ``TIKTOK_POC_DELAY`` override the defaults of
20 items, 5 pages, and 1.5 seconds between pages.

Media download is enabled by default and can be tuned with:

- ``TIKTOK_POC_DOWNLOAD``: ``0`` disables the download phase (fetch only).
- ``TIKTOK_POC_URL_MODE``: ``play_url`` (default), ``download_url``, or
  ``all`` selects which media URL field(s) to download.
- ``TIKTOK_POC_DOWNLOAD_DIR``: output directory (default ``poc/downloads``).
- ``TIKTOK_POC_CONCURRENCY``: parallel downloads (default 5).
- ``TIKTOK_POC_MAX_RETRY``: per-file retries (default 3).
- ``TIKTOK_POC_CHUNK_KB``: streaming chunk size in KiB (default 1024).

TikTok CDN URLs are signed and expire within hours, so download soon after
fetching. A 403 response means the link expired; re-fetch the metadata.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from time import time
from urllib.parse import quote, urlencode, urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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

DEFAULT_DOWNLOAD_DIR = Path(__file__).with_name("downloads")
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
    path = Path(__file__).with_name(f"tiktok_recommend_{name}.json")
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


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
    print(f"  Sample ID: {item.get('id', '-')}")
    print(f"  Author: {author.get('uniqueId', author.get('nickname', '-'))}")
    print(
        "  Stats: "
        f"plays={stats.get('playCount', stats.get('play_count', '-'))}, "
        f"likes={stats.get('diggCount', stats.get('digg_count', '-'))}, "
        f"shares={stats.get('shareCount', stats.get('share_count', '-'))}"
    )
    print(f"  Play URL available: {'yes' if play_url else 'no'}")
    print(f"  Download URL available: {'yes' if download_url else 'no'}")
    print(f"  Media host: {urlparse(media_url).netloc if media_url else '-'}")


def headers(cookie: dict[str, str]) -> dict[str, str]:
    return DATA_HEADERS_TIKTOK | {
        "Cookie": cookie_string(cookie),
        "Referer": "https://www.tiktok.com/explore",
    }


def download_headers(user_agent: str) -> dict[str, str]:
    """Headers for the TikTok CDN, with the session User-Agent for signing."""
    return DOWNLOAD_HEADERS_TIKTOK | {"User-Agent": user_agent}


def select_urls(item: dict, mode: str) -> list[tuple[str, str]]:
    """Return ``(label, url)`` pairs for the selected mode, skipping empty URLs."""
    pairs = []
    if mode in ("play_url", "all") and (url := item.get("play_url")):
        pairs.append(("play", url))
    if mode in ("download_url", "all") and (url := item.get("download_url")):
        pairs.append(("download", url))
    return pairs


def safe_name(item: dict, label: str | None) -> str:
    """Build a filesystem-safe base name from author and id."""
    author = str(item.get("author_id") or item.get("author_nickname") or "unknown")
    item_id = str(item.get("id") or "unknown")
    base = f"{author}_{item_id}"
    if label:
        base = f"{base}_{label}"
    # Keep letters/digits and safe punctuation; replace the rest with "_".
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in base)


async def download_one(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    headers: dict[str, str],
    max_retry: int,
    chunk_size: int,
) -> bool:
    """Stream one media URL to ``dest`` with Range-based resume and retries."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  SKIP (exists): {dest.name}")
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
                # 200 means the server ignored Range: restart from scratch.
                mode = "ab" if response.status_code == 206 and position else "wb"
                with open(temp, mode) as file:
                    async for chunk in response.aiter_bytes(chunk_size):
                        file.write(chunk)
            temp.replace(dest)
            print(f"  DONE: {dest.name} ({dest.stat().st_size} bytes)")
            return True
        except httpx.HTTPStatusError as error:
            print(f"  HTTP {error.response.status_code}: {dest.name}")
            if error.response.status_code in (403, 404, 410):
                print("  Link may have expired; re-fetch the metadata.")
                return False
        except httpx.RequestError as error:
            print(f"  RETRY {attempt}/{max_retry} {dest.name}: {error}")
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
    # In "all" mode every item downloads both variants with distinct labels;
    # otherwise a single URL keeps the plain base name.
    single = mode != "all"

    async def worker(item: dict, label: str, url: str) -> bool:
        name = safe_name(item, None if single else label)
        dest = download_dir / f"{name}.mp4"
        async with semaphore:
            return await download_one(client, url, dest, headers, max_retry, chunk_size)

    tasks = [
        worker(item, label, url)
        for item in metadata
        for label, url in select_urls(item, mode)
    ]
    if not tasks:
        print("Download: no media URLs to download.")
        return
    print(
        f"\nDownload: {len(tasks)} file(s) -> {download_dir} "
        f"(mode={mode}, concurrency={concurrency})"
    )
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r)
    print(f"Download summary: {ok} succeeded, {len(results) - ok} failed.")
    manifest = [
        {
            "id": item.get("id"),
            "author_id": item.get("author_id"),
            "file": f"{safe_name(item, None if single else label)}.mp4",
            "url_label": label,
            "ok": ok_flag,
        }
        for (item, label), ok_flag in zip(
            (
                (item, label)
                for item in metadata
                for label, _ in select_urls(item, mode)
            ),
            results,
        )
    ]
    print(f"Saved download manifest: {save_json('download_manifest', manifest)}")


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
        "Profile control: "
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
        f"Page {page}: status={response.status_code}, content-type={content_type}, "
        f"bytes={len(response.content)}"
    )
    if "application/json" not in content_type:
        print("Result: not JSON")
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        print("Result: invalid JSON")
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
            print("Result: no itemList")
            break

        items.extend(page_items)
        pages_fetched = page
        print(
            f"Result: {len(page_items)} recommendation(s), "
            f"total={len(items)}, hasMore={has_more}"
        )
        if not has_more:
            break
        if next_cursor == cursor:
            print("Result: cursor did not advance; stopping to avoid a duplicate page.")
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
    if not SETTINGS_PATH.is_file():
        print(f"FATAL: settings file not found: {SETTINGS_PATH}")
        return 2
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    cookie = dict(settings.get("cookie_tiktok", {}))
    browser_info = settings.get("browser_info_tiktok", {})
    device_id = browser_info.get("device_id", "")
    user_agent = browser_info.get("User-Agent", "")
    if not cookie.get("sessionid") or not device_id or not user_agent:
        print("FATAL: cookie_tiktok, device_id, or User-Agent is missing")
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
    url_mode = url_mode_from_env("TIKTOK_POC_URL_MODE")
    download_dir = Path(os.getenv("TIKTOK_POC_DOWNLOAD_DIR", str(DEFAULT_DOWNLOAD_DIR)))
    concurrency = positive_int_from_env("TIKTOK_POC_CONCURRENCY", DEFAULT_CONCURRENCY)
    max_retry = positive_int_from_env("TIKTOK_POC_MAX_RETRY", DEFAULT_MAX_RETRY)
    chunk_size = positive_int_from_env("TIKTOK_POC_CHUNK_KB", DEFAULT_CHUNK_KB) * 1024
    print(
        f"Setup: proxy={proxy or 'direct'}, sessionid=yes, "
        f"msToken={'yes' if cookie.get('msToken') else 'no'}, device_id={device_id}, "
        f"count={count}, max_pages={max_pages}, delay={delay}s"
    )
    print(
        f"Download: enabled={download_enabled}, url_mode={url_mode}, "
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
            print(f"NETWORK ERROR: {type(error).__name__}: {error}")
            return 2

    print("\n=== Verdict ===")
    if items:
        print(f"Saved raw pages: {save_json('pages', payloads)}")
        print(f"Saved itemList: {save_json('items', items)}")
        print(f"Saved flattened metadata: {save_json('metadata', metadata)}")
        print_item_summary(items[0])
        author_ids = {item["author_id"] for item in metadata if item["author_id"]}
        print(
            f"Summary: pages={pages_fetched}, items={len(items)}, authors={len(author_ids)}"
        )
        if has_more and pages_fetched == max_pages:
            print("Stopped: reached TIKTOK_POC_MAX_PAGES.")
        elif has_more:
            print("Stopped: pagination ended before TikTok reported the final page.")
        else:
            print("Stopped: TikTok reported no further recommendations.")
        print("GO: recommendation responses contain metadata and media URLs.")
        return 0
    print("STOP: the recommendation endpoint returned no itemList.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
