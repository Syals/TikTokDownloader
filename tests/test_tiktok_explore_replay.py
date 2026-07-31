import hashlib

from poc.tiktok_explore_replay import (
    build_replay_plan,
    cursor_progression,
    item_progression,
    summarize_response,
)


def test_build_replay_plan_uses_one_initial_and_next_page_per_category() -> None:
    requests = [
        {"category_type": "120", "pull_type": "1"},
        {"category_type": "120", "pull_type": "2"},
        {"category_type": "123", "pull_type": "1"},
        {"category_type": "123", "pull_type": "2"},
        {"category_type": "119", "pull_type": "2"},
        {"category_type": "119", "pull_type": "2"},
        {"category_type": "120", "pull_type": "2"},
    ]

    plan = build_replay_plan(requests)

    assert plan == requests[:6]


def test_summarize_response_requires_nonempty_items_and_cursor() -> None:
    summary = summarize_response(
        200,
        {
            "itemList": [{"id": "123"}],
            "cursor": "next-page",
            "hasMore": True,
        },
    )

    assert summary == {
        "http_status": 200,
        "item_count": 1,
        "item_id_hashes": [hashlib.sha256(b"123").hexdigest()[:12]],
        "cursor": {
            "present": True,
            "length": 9,
            "sha256": hashlib.sha256(b"next-page").hexdigest()[:12],
        },
        "has_more": True,
        "valid": True,
    }


def test_summarize_response_rejects_empty_items_or_missing_cursor() -> None:
    summary = summarize_response(200, {"itemList": [], "hasMore": True})

    assert summary["item_count"] == 0
    assert summary["item_id_hashes"] == []
    assert summary["cursor"] == {
        "present": False,
        "length": 0,
        "sha256": None,
    }
    assert summary["valid"] is False


def test_cursor_progression_requires_two_distinct_cursor_hashes_per_category() -> None:
    results = [
        {"category_type": "120", "cursor": {"sha256": "one"}},
        {"category_type": "120", "cursor": {"sha256": "two"}},
        {"category_type": "123", "cursor": {"sha256": "same"}},
        {"category_type": "123", "cursor": {"sha256": "same"}},
    ]

    assert cursor_progression(results) == {"120": True, "123": False}


def test_item_progression_requires_new_item_ids_on_a_later_request() -> None:
    results = [
        {"category_type": "120", "item_id_hashes": ["first", "shared"]},
        {"category_type": "120", "item_id_hashes": ["shared", "second"]},
        {"category_type": "123", "item_id_hashes": ["only"]},
        {"category_type": "123", "item_id_hashes": ["only"]},
    ]

    assert item_progression(results) == {
        "120": {"request_count": 2, "new_item_counts": [2, 1], "advanced": True},
        "123": {"request_count": 2, "new_item_counts": [1, 0], "advanced": False},
    }
