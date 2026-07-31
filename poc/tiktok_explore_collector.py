"""Opt-in collector for the captured TikTok Explore request sequences."""

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

if __package__:
    from .tiktok_explore_replay import (
        DEFAULT_HAR_PATH,
        DEFAULT_PROXY,
        DEFAULT_SETTINGS_PATH,
        EXPLORE_ITEM_LIST_ENDPOINT,
        captured_explore_requests,
        load_session,
        summarize_response,
    )
else:
    from tiktok_explore_replay import (
        DEFAULT_HAR_PATH,
        DEFAULT_PROXY,
        DEFAULT_SETTINGS_PATH,
        EXPLORE_ITEM_LIST_ENDPOINT,
        captured_explore_requests,
        load_session,
        summarize_response,
    )


DEFAULT_OUTPUT_PATH = Path(".output/tiktok_explore_collection.json")


def build_collection_plan(
    requests: Sequence[Mapping[str, Any]], max_pages_per_category: int
) -> list[Mapping[str, Any]]:
    """Keep captured requests without inventing category pagination fields."""
    selected_counts: dict[str, int] = {}
    plan: list[Mapping[str, Any]] = []
    for request in requests:
        category_type = request.get("category_type")
        if not isinstance(category_type, str):
            continue
        if selected_counts.get(category_type, 0) >= max_pages_per_category:
            continue
        selected_counts[category_type] = selected_counts.get(category_type, 0) + 1
        plan.append(request)
    return plan


def hash_identifier(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def normalize_collection(
    pages: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Return commit-safe category metadata and a deduplicated download checklist."""
    categories: dict[str, dict[str, Any]] = {}
    items: dict[str, dict[str, Any]] = {}
    category_item_ids: dict[str, set[str]] = {}

    for category_type, payload in pages:
        item_list = payload.get("itemList")
        if not isinstance(item_list, list):
            continue
        categories.setdefault(
            category_type,
            {
                "category_type": category_type,
                "category_name": None,
                "page_count": 0,
                "item_count": 0,
            },
        )
        categories[category_type]["page_count"] += 1
        category_item_ids.setdefault(category_type, set())

        for item in item_list:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                continue
            item_id = item["id"]
            category_item_ids[category_type].add(item_id)
            record = items.setdefault(
                item_id,
                {
                    "item_id_sha256": hash_identifier(item_id),
                    "author_id_sha256": None,
                    "category_types": [],
                },
            )
            if category_type not in record["category_types"]:
                record["category_types"].append(category_type)
            author = item.get("author")
            author_id = author.get("id") if isinstance(author, Mapping) else None
            if isinstance(author_id, str) and record["author_id_sha256"] is None:
                record["author_id_sha256"] = hash_identifier(author_id)

    for category_type, item_ids in category_item_ids.items():
        categories[category_type]["item_count"] = len(item_ids)

    normalized_items = list(items.values())
    return {
        "categories": list(categories.values()),
        "items": normalized_items,
        "download_list": [
            {
                "item_id_sha256": item["item_id_sha256"],
                "category_types": item["category_types"],
            }
            for item in normalized_items
        ],
    }


async def collect_live(
    plan: Sequence[Mapping[str, Any]],
    cookie: dict[str, str],
    user_agent: str,
    proxy: str | None,
) -> dict[str, Any]:
    """Replay the captured requests and keep live response payloads in memory only."""
    summaries: list[dict[str, Any]] = []
    pages: list[tuple[str, Mapping[str, Any]]] = []
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        cookies=cookie,
        follow_redirects=False,
        proxy=proxy,
        timeout=30,
    ) as client:
        for request in plan:
            category_type = request["category_type"]
            summary = {
                "category_type": category_type,
                "pull_type": request.get("pull_type"),
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
                summary |= summarize_response(response.status_code, payload)
                if isinstance(payload, Mapping) and isinstance(
                    payload.get("itemList"), list
                ):
                    pages.append((category_type, payload))
            except httpx.HTTPError as error:
                summary |= {
                    "http_status": None,
                    "item_count": 0,
                    "cursor": {"present": False, "length": 0},
                    "has_more": None,
                    "valid": False,
                    "error_type": type(error).__name__,
                }
            summaries.append(summary)

    collection = normalize_collection(pages)
    return {
        "endpoint": EXPLORE_ITEM_LIST_ENDPOINT,
        "requests": summaries,
        **collection,
        "collected": bool(collection["items"]),
    }


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def save_collection(collection: Mapping[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(collection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Opt-in collection using captured TikTok Explore request sequences."
    )
    parser.add_argument("--live", action="store_true", help="Send captured requests.")
    parser.add_argument("--har", type=Path, default=DEFAULT_HAR_PATH)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--max-pages-per-category",
        type=positive_int,
        default=2,
        help="Maximum captured requests to replay for each discovered category.",
    )
    parser.add_argument(
        "--proxy",
        default=os.getenv("TIKTOK_POC_PROXY", DEFAULT_PROXY),
        help="HTTP proxy; pass an empty string for a direct connection.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.live:
        print("Refusing network access without --live.")
        return 2
    try:
        har = json.loads(args.har.read_text(encoding="utf-8"))
        if not isinstance(har, Mapping):
            raise ValueError("HAR root must be an object")
        plan = build_collection_plan(
            captured_explore_requests(har), args.max_pages_per_category
        )
        cookie, user_agent = load_session(args.settings)
    except (OSError, ValueError, json.JSONDecodeError):
        print("ERROR: unable to load local collector inputs.")
        return 2

    collection = asyncio.run(collect_live(plan, cookie, user_agent, args.proxy or None))
    save_collection(collection, args.output)
    print(
        f"Collection {'completed' if collection['collected'] else 'failed'}: "
        f"{len(collection['items'])} item(s), {len(collection['categories'])} category(ies)."
    )
    return 0 if collection["collected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
