"""TikTok Explore 和 For You POC 共用的媒体与响应工具。"""

import json
from pathlib import Path

import httpx

from src.custom import DATA_HEADERS_TIKTOK, DOWNLOAD_HEADERS_TIKTOK


DEFAULT_CONCURRENCY = 5
DEFAULT_MAX_RETRY = 3
DEFAULT_CHUNK_KB = 1024
URL_MODES = ("play_url", "download_url", "all")


def cookie_string(cookie: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookie.items())


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


def headers(cookie: dict[str, str], referer: str) -> dict[str, str]:
    return DATA_HEADERS_TIKTOK | {"Cookie": cookie_string(cookie), "Referer": referer}


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
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in base)


def author_directory_name(item: dict) -> str:
    return safe_name(item.get("author_id") or item.get("author_nickname"))


def item_directory_name(item: dict) -> str:
    return (
        Path(author_directory_name(item)) / str(item.get("id") or "unknown")
    ).as_posix()


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
