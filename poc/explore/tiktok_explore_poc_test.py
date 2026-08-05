import json
import tempfile
from pathlib import Path

import httpx

from poc.explore.tiktok_explore_poc import (
    BROWSER_REQUEST_FIELDS,
    build_base_params,
    build_explore_params,
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
