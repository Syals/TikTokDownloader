"""回归测试：诊断出的浏览器字段写入本地会话设置。"""

import json
import tempfile
from pathlib import Path

from poc.explore.diagnose_explore_fields import (
    extract_browser_request_params,
    extract_explore_templates,
    save_browser_request_params,
)


def test_extract_browser_request_params_requires_complete_field_set() -> None:
    queries = [
        {"clientABVersions": "ab"},
        {
            "clientABVersions": "ab",
            "odinId": "odin",
            "verifyFp": "verify",
            "is_new_user": "false",
            "video_encoding": "h264",
        },
    ]

    assert extract_browser_request_params(queries) == {
        "clientABVersions": "ab",
        "odinId": "odin",
        "verifyFp": "verify",
        "is_new_user": "false",
        "video_encoding": "h264",
    }


def test_save_browser_request_params_preserves_existing_browser_info() -> None:
    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        settings_path = Path(temp_dir) / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "cookie_tiktok": {"sessionid": "session"},
                    "browser_info_tiktok": {"device_id": "device"},
                }
            ),
            encoding="utf-8",
        )

        save_browser_request_params(
            settings_path,
            {
                "clientABVersions": "ab",
                "odinId": "odin",
                "verifyFp": "verify",
                "is_new_user": "false",
                "video_encoding": "h264",
            },
        )

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["browser_info_tiktok"] == {
            "device_id": "device",
            "clientABVersions": "ab",
            "odinId": "odin",
            "verifyFp": "verify",
            "is_new_user": "false",
            "video_encoding": "h264",
        }


def test_extract_explore_templates_keeps_request_order() -> None:
    queries = [
        {"WebIdLastTime": "first", "pullType": "1", "count": "8"},
        {"WebIdLastTime": "next", "pullType": "2", "msToken": "token"},
    ]

    assert extract_explore_templates(queries) == (
        [("WebIdLastTime", "first"), ("pullType", "1"), ("count", "8")],
        [
            ("WebIdLastTime", "next"),
            ("pullType", "2"),
            ("msToken", "token"),
        ],
    )
