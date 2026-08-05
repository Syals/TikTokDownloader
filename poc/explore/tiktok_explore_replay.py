"""对抓包到的 TikTok Explore 请求进行可选式纯 HTTP 重放。"""

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx


EXPLORE_ITEM_LIST_ENDPOINT = "https://www.tiktok.com/api/explore/item_list/"
DEFAULT_PROXY = ""
DEFAULT_HAR_PATH = Path("explore-har.json")
DEFAULT_SETTINGS_PATH = Path("Volume/settings.json")
DEFAULT_REPORT_PATH = Path("reverse-records/explore_replay.json")
ALLOWED_HEADER_NAMES = {
    "accept",
    "accept-language",
    "referer",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
}


def build_replay_plan(
    requests: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """为每个分类保留一条抓包的初始请求和一条翻页请求。"""
    initial_categories = {
        request["category_type"]
        for request in requests
        if request.get("pull_type") == "1"
        and isinstance(request.get("category_type"), str)
    }
    selected_initial_categories: set[str] = set()
    next_page_counts: dict[str, int] = {}
    plan: list[Mapping[str, Any]] = []
    for request in requests:
        category_type = request.get("category_type")
        pull_type = request.get("pull_type")
        if not isinstance(category_type, str) or not isinstance(pull_type, str):
            continue
        if pull_type == "1" and category_type not in selected_initial_categories:
            selected_initial_categories.add(category_type)
            plan.append(request)
        elif pull_type == "2" and next_page_counts.get(category_type, 0) < (
            1 if category_type in initial_categories else 2
        ):
            next_page_counts[category_type] = next_page_counts.get(category_type, 0) + 1
            plan.append(request)
    return plan


def summarize_response(status: int, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {
            "http_status": status,
            "item_count": 0,
            "cursor": {"present": False, "length": 0},
            "has_more": None,
            "valid": False,
        }

    item_list = payload.get("itemList")
    item_count = len(item_list) if isinstance(item_list, list) else 0
    item_id_hashes = [
        hashlib.sha256(item["id"].encode()).hexdigest()[:12]
        for item in item_list or []
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    ]
    cursor = payload.get("cursor")
    cursor_text = str(cursor) if cursor is not None else ""
    has_more = payload.get("hasMore")
    return {
        "http_status": status,
        "item_count": item_count,
        "item_id_hashes": item_id_hashes,
        "cursor": {
            "present": bool(cursor_text),
            "length": len(cursor_text),
            "sha256": (
                hashlib.sha256(cursor_text.encode()).hexdigest()[:12]
                if cursor_text
                else None
            ),
        },
        "has_more": has_more if isinstance(has_more, bool) else None,
        "valid": status == 200 and item_count > 0 and bool(cursor_text),
    }


def cursor_progression(results: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    cursors: dict[str, list[str | None]] = {}
    for result in results:
        category_type = result.get("category_type")
        cursor = result.get("cursor")
        if not isinstance(category_type, str) or not isinstance(cursor, Mapping):
            continue
        cursor_hash = cursor.get("sha256")
        cursors.setdefault(category_type, []).append(
            cursor_hash if isinstance(cursor_hash, str) else None
        )
    return {
        category_type: len(values) >= 2
        and all(value is not None for value in values)
        and len(set(values)) == len(values)
        for category_type, values in cursors.items()
    }


def item_progression(results: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    pages: dict[str, list[set[str]]] = {}
    for result in results:
        category_type = result.get("category_type")
        item_id_hashes = result.get("item_id_hashes")
        if not isinstance(category_type, str) or not isinstance(item_id_hashes, list):
            continue
        pages.setdefault(category_type, []).append(
            {item_id for item_id in item_id_hashes if isinstance(item_id, str)}
        )

    progression: dict[str, dict[str, Any]] = {}
    for category_type, item_pages in pages.items():
        seen: set[str] = set()
        new_item_counts: list[int] = []
        for item_ids in item_pages:
            new_item_counts.append(len(item_ids - seen))
            seen.update(item_ids)
        progression[category_type] = {
            "request_count": len(item_pages),
            "new_item_counts": new_item_counts,
            "advanced": len(item_pages) >= 2 and any(new_item_counts[1:]),
        }
    return progression


def headers_from_har(headers: Any) -> dict[str, str]:
    if not isinstance(headers, list):
        return {}
    return {
        header["name"]: str(header.get("value", ""))
        for header in headers
        if isinstance(header, Mapping)
        and isinstance(header.get("name"), str)
        and header["name"].lower() in ALLOWED_HEADER_NAMES
    }


def captured_explore_requests(har: Mapping[str, Any]) -> list[dict[str, Any]]:
    log = har.get("log")
    if not isinstance(log, Mapping) or not isinstance(log.get("entries"), list):
        raise ValueError("HAR 必须包含 log.entries")

    requests: list[dict[str, Any]] = []
    for entry in log["entries"]:
        if not isinstance(entry, Mapping):
            continue
        request = entry.get("request")
        if not isinstance(request, Mapping):
            continue
        url = request.get("url")
        if not isinstance(url, str):
            continue
        parts = urlsplit(url)
        endpoint = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if endpoint != EXPLORE_ITEM_LIST_ENDPOINT:
            continue
        params = parse_qsl(parts.query, keep_blank_values=True)
        param_values = dict(params)
        category_type = param_values.get("categoryType")
        pull_type = param_values.get("pullType")
        if not category_type or not pull_type:
            continue
        requests.append(
            {
                "category_type": category_type,
                "pull_type": pull_type,
                "params": params,
                "headers": headers_from_har(request.get("headers")),
            }
        )
    return requests


def load_session(settings_path: Path) -> tuple[dict[str, str], str]:
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, Mapping):
        raise ValueError("settings 根节点必须是对象")
    cookie = settings.get("cookie_tiktok")
    browser_info = settings.get("browser_info_tiktok")
    if not isinstance(cookie, Mapping) or not isinstance(browser_info, Mapping):
        raise ValueError("缺少 TikTok 会话配置")
    session = {str(name): str(value) for name, value in cookie.items()}
    user_agent = browser_info.get("User-Agent")
    if (
        not session.get("sessionid")
        or not isinstance(user_agent, str)
        or not user_agent
    ):
        raise ValueError("缺少 TikTok sessionid 或 User-Agent")
    return session, user_agent


def load_device_id(settings_path: Path) -> str:
    """读取与 TikTok 会话对应的浏览器 device_id。"""
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, Mapping):
        raise ValueError("settings 根节点必须是对象")
    browser_info = settings.get("browser_info_tiktok")
    if not isinstance(browser_info, Mapping):
        raise ValueError("缺少 TikTok 浏览器配置")
    device_id = browser_info.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("缺少 TikTok device_id")
    return device_id


async def replay_live(
    plan: Sequence[Mapping[str, Any]],
    cookie: dict[str, str],
    user_agent: str,
    proxy: str | None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        cookies=cookie,
        follow_redirects=False,
        proxy=proxy,
        timeout=30,
    ) as client:
        for request in plan:
            result = {
                "category_type": request["category_type"],
                "pull_type": request["pull_type"],
            }
            try:
                response = await client.get(
                    EXPLORE_ITEM_LIST_ENDPOINT,
                    params=request["params"],
                    headers=request["headers"],
                )
                try:
                    payload: Any = response.json()
                except json.JSONDecodeError:
                    payload = None
                result |= summarize_response(response.status_code, payload)
            except httpx.HTTPError as error:
                result |= {
                    "http_status": None,
                    "item_count": 0,
                    "cursor": {"present": False, "length": 0},
                    "has_more": None,
                    "valid": False,
                    "error_type": type(error).__name__,
                }
            results.append(result)

    categories = sorted({result["category_type"] for result in results})
    progression = cursor_progression(results)
    page_progression = item_progression(results)
    return {
        "endpoint": EXPLORE_ITEM_LIST_ENDPOINT,
        "category_types": categories,
        "results": results,
        "cursor_progression": progression,
        "item_progression": page_progression,
        "passed": (
            len(categories) >= 3
            and all(result["valid"] for result in results)
            and all(
                page_progression.get(category_type, {}).get("advanced")
                for category_type in categories
            )
        ),
    }


def save_report(report: Mapping[str, Any], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对本地抓包的 TikTok Explore 请求进行可选式纯 HTTP 重放。"
    )
    parser.add_argument("--live", action="store_true", help="发送抓包请求。")
    parser.add_argument("--har", type=Path, default=DEFAULT_HAR_PATH)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--proxy",
        default=os.getenv("TIKTOK_POC_PROXY", DEFAULT_PROXY),
        help="HTTP 代理；传入空字符串表示直连。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.live:
        print("未指定 --live，拒绝联网。")
        return 2
    try:
        har = json.loads(args.har.read_text(encoding="utf-8"))
        if not isinstance(har, Mapping):
            raise ValueError("HAR 根节点必须是对象")
        plan = build_replay_plan(captured_explore_requests(har))
        cookie, user_agent = load_session(args.settings)
    except (OSError, ValueError, json.JSONDecodeError):
        print("错误: 无法加载本地重放输入。")
        return 2

    report = asyncio.run(replay_live(plan, cookie, user_agent, args.proxy or None))
    save_report(report, args.report)
    print(
        f"重放{'通过' if report['passed'] else '失败'}: "
        f"{len(report['results'])} 个请求，{len(report['category_types'])} 个分类。"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
