import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx

from poc.explore.tiktok_explore_poc import (
    BROWSER_REQUEST_FIELDS,
    build_base_params,
    build_explore_params,
    collect_explore,
    initial_cursor,
    load_browser_request_params,
    load_explore_templates,
    refresh_session,
)
from poc.explore.tiktok_explore_replay import load_device_id


def test_build_base_params_supports_har_free_explore_requests() -> None:
    params = dict(
        build_explore_params(
            build_base_params("1234567890"),
            category_type="120",
            pull_type="1",
            cursor="0",
            count=5,
            ms_token="current-ms-token",
        )
    )

    assert params["aid"] == "1988"
    assert params["device_id"] == "1234567890"
    assert "from_page" not in params
    assert params["categoryType"] == "120"
    assert params["pullType"] == "1"
    assert params["cursor"] == "0"
    assert params["count"] == "5"
    assert params["msToken"] == "current-ms-token"
    assert params["WebIdLastTime"].isdigit()


def test_build_explore_params_omits_empty_cursor_and_ms_token() -> None:
    params = dict(
        build_explore_params(
            build_base_params("1234567890"),
            category_type="120",
            pull_type="1",
            cursor="",
            count=5,
            ms_token="",
        )
    )

    assert "cursor" not in params
    assert "msToken" not in params


def test_build_base_params_includes_confirmed_browser_fields() -> None:
    params = dict(
        build_base_params(
            "1234567890",
            {
                "clientABVersions": "ab-versions",
                "odinId": "odin-id",
                "verifyFp": "verify-fp",
                "is_new_user": "false",
                "video_encoding": "h264",
                "ignored": "value",
            },
        )
    )

    assert {field: params[field] for field in BROWSER_REQUEST_FIELDS} == {
        "clientABVersions": "ab-versions",
        "odinId": "odin-id",
        "verifyFp": "verify-fp",
        "is_new_user": "false",
        "video_encoding": "h264",
    }
    assert "ignored" not in params


def test_build_base_params_uses_empty_from_page_for_following_pages() -> None:
    assert "from_page" not in dict(build_base_params("1234567890"))
    assert dict(build_base_params("1234567890", from_page=""))["from_page"] == ""


def test_initial_cursor_matches_browser_empty_cursor_behavior() -> None:
    assert initial_cursor({"params": [("aid", "1988")]}) == ""
    assert initial_cursor({"params": "invalid"}) == ""
    assert initial_cursor({"params": [("cursor", "next-page")]}) == "next-page"


def test_refresh_session_returns_only_a_new_response_ms_token() -> None:
    cookie = {"msToken": "stale"}
    response = httpx.Response(
        200,
        headers={"x-ms-token": "fresh"},
        request=httpx.Request("GET", "https://www.tiktok.com/"),
    )

    assert refresh_session(cookie, response) == "fresh"
    assert cookie["msToken"] == "fresh"


def test_collect_explore_continues_after_missing_initial_item_list(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_fetch_explore_page(*_: Any, **kwargs: Any):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"hasMore": True, "cursor": "next"}, {}, ""
        return (
            {
                "hasMore": False,
                "cursor": "end",
                "itemList": [{"id": "video-1"}],
            },
            {},
            "",
        )

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_poc.fetch_explore_page",
        fake_fetch_explore_page,
    )

    metadata, report = asyncio.run(
        collect_explore(
            client=httpx.AsyncClient(),
            initial_template={"params": [("cursor", "0")]},
            next_template={"params": [("cursor", "next")]},
            category_type="100",
            count=8,
            max_pages=2,
            delay=0,
            cookie={},
            user_agent="test",
        )
    )

    assert [call["pull_type"] for call in calls] == ["1", "2"]
    assert [item["id"] for item in metadata] == ["video-1"]
    assert report[0]["item_list_missing"] is True


def test_collect_explore_reports_progress_and_new_item_count(
    monkeypatch,
) -> None:
    pages = [
        ({"hasMore": True, "cursor": "next", "itemList": [{"id": "v1"}]}, {}, ""),
        (
            {
                "hasMore": False,
                "cursor": "end",
                "itemList": [{"id": "v1"}, {"id": "v2"}],
            },
            {},
            "",
        ),
    ]
    remaining = iter(pages)

    async def fake_fetch_explore_page(*_: Any, **__: Any):
        return next(remaining)

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_poc.fetch_explore_page",
        fake_fetch_explore_page,
    )

    progress_events: list[tuple[int, int, int]] = []

    metadata, report = asyncio.run(
        collect_explore(
            client=httpx.AsyncClient(),
            initial_template={"params": [("cursor", "0")]},
            next_template={"params": [("cursor", "next")]},
            category_type="100",
            count=8,
            max_pages=2,
            delay=0,
            cookie={},
            user_agent="test",
            progress=lambda page, new, total: progress_events.append(
                (page, new, total)
            ),
        )
    )

    assert [item["id"] for item in metadata] == ["v1", "v2"]
    assert progress_events == [(1, 1, 1), (2, 1, 2)]
    assert [page["new_item_count"] for page in report] == [1, 1]


def test_load_browser_fields_and_device_id_from_settings() -> None:
    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        settings_path = Path(temp_dir) / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "cookie_tiktok": {"sessionid": "session"},
                    "browser_info_tiktok": {
                        "User-Agent": "Mozilla/5.0",
                        "device_id": "1234567890",
                        "clientABVersions": "ab-versions",
                        "odinId": "odin-id",
                        "verifyFp": "verify-fp",
                        "is_new_user": "false",
                        "video_encoding": "h264",
                        "explore_initial_template": [
                            ["categoryType", "120"],
                            ["pullType", "1"],
                        ],
                        "explore_next_template": [
                            ["categoryType", "120"],
                            ["pullType", "2"],
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

        assert load_device_id(settings_path) == "1234567890"
        assert load_browser_request_params(settings_path) == {
            "clientABVersions": "ab-versions",
            "odinId": "odin-id",
            "verifyFp": "verify-fp",
            "is_new_user": "false",
            "video_encoding": "h264",
        }
        assert load_explore_templates(settings_path) == (
            [("categoryType", "120"), ("pullType", "1")],
            [("categoryType", "120"), ("pullType", "2")],
        )


def test_load_browser_request_params_tolerates_missing_optional_verify_fp() -> None:
    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        settings_path = Path(temp_dir) / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "cookie_tiktok": {"sessionid": "session"},
                    "browser_info_tiktok": {
                        "User-Agent": "Mozilla/5.0",
                        "device_id": "1234567890",
                        "clientABVersions": "ab-versions",
                        "odinId": "odin-id",
                        "is_new_user": "false",
                        "video_encoding": "h264",
                    },
                }
            ),
            encoding="utf-8",
        )

        assert load_browser_request_params(settings_path) == {
            "clientABVersions": "ab-versions",
            "odinId": "odin-id",
            "is_new_user": "false",
            "video_encoding": "h264",
        }


def test_load_browser_request_params_empty_when_required_missing() -> None:
    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        settings_path = Path(temp_dir) / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "cookie_tiktok": {"sessionid": "session"},
                    "browser_info_tiktok": {
                        "User-Agent": "Mozilla/5.0",
                        "device_id": "1234567890",
                        "clientABVersions": "ab-versions",
                        "odinId": "odin-id",
                        "is_new_user": "false",
                    },
                }
            ),
            encoding="utf-8",
        )

        assert load_browser_request_params(settings_path) == {}
