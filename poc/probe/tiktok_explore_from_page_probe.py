"""验证不依赖 HAR 的 TikTok Explore 请求参数。"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._common import headers  # noqa: E402
from poc.explore.tiktok_explore_poc import (  # noqa: E402
    build_base_params,
    build_explore_params,
    signed_url,
)
from poc.explore.tiktok_explore_replay import (  # noqa: E402
    DEFAULT_PROXY,
    load_device_id,
    load_session,
)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须为正整数") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return parsed


async def probe(
    *,
    cookie: dict[str, str],
    device_id: str,
    user_agent: str,
    category_type: str,
    from_page: str,
    count: int,
    proxy: str | None,
) -> dict[str, Any]:
    template = dict(build_base_params(device_id))
    template["from_page"] = from_page
    params = build_explore_params(
        list(template.items()),
        category_type=category_type,
        pull_type="1",
        cursor="0",
        count=count,
        ms_token=cookie.get("msToken", ""),
    )
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        cookies=cookie,
        follow_redirects=True,
        proxy=proxy,
        timeout=30,
        verify=False,
    ) as client:
        response = await client.get(
            signed_url(params, user_agent),
            headers=headers(cookie, "https://www.tiktok.com/explore"),
        )
    content_type = response.headers.get("content-type", "")
    is_json = "application/json" in content_type
    payload: Any = None
    if is_json:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            is_json = False
    items = payload.get("itemList") if isinstance(payload, Mapping) else None
    item_count = len(items) if isinstance(items, list) else 0
    return {
        "http_status": response.status_code,
        "content_type": content_type.split(";", 1)[0],
        "is_json": is_json,
        "item_count": item_count,
        "passed": response.status_code == 200 and is_json and item_count > 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="验证无 HAR 的 TikTok Explore 请求参数。"
    )
    parser.add_argument("--live", action="store_true", help="发送验证请求。")
    parser.add_argument(
        "--settings", type=Path, default=PROJECT_ROOT / "Volume" / "settings.json"
    )
    parser.add_argument("--category-type", default="120")
    parser.add_argument("--from-page", default="explore")
    parser.add_argument("--count", type=positive_int, default=5)
    parser.add_argument("--proxy", default=os.getenv("TIKTOK_POC_PROXY", DEFAULT_PROXY))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.live:
        print("未指定 --live，拒绝联网。")
        return 2
    try:
        cookie, user_agent = load_session(args.settings)
        device_id = load_device_id(args.settings)
    except (OSError, ValueError, json.JSONDecodeError):
        print("错误: 无法加载 TikTok 会话配置。")
        return 2
    try:
        result = asyncio.run(
            probe(
                cookie=cookie,
                device_id=device_id,
                user_agent=user_agent,
                category_type=args.category_type,
                from_page=args.from_page,
                count=args.count,
                proxy=args.proxy or None,
            )
        )
    except httpx.HTTPError as error:
        print(f"网络错误: {type(error).__name__}: {error}")
        return 2
    print(
        f"Explore 探针: HTTP {result['http_status']}，"
        f"content-type={result['content_type'] or '-'}，"
        f"itemList={result['item_count']}。"
    )
    if result["passed"]:
        print(f"通过: from_page={args.from_page!r} 可用于无 HAR Explore 请求。")
        return 0
    print(f"未通过: from_page={args.from_page!r} 未返回有效 itemList。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
