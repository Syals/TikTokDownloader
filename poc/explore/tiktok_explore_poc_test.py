import json
from pathlib import Path

from poc.explore.tiktok_explore_poc import (
    build_base_params,
    build_explore_params,
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
    assert params["from_page"] == "explore"
    assert params["categoryType"] == "120"
    assert params["pullType"] == "1"
    assert params["cursor"] == "0"
    assert params["count"] == "5"
    assert params["msToken"] == "current-ms-token"
    assert params["WebIdLastTime"].isdigit()


def test_load_device_id_reads_tiktok_browser_configuration(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "cookie_tiktok": {"sessionid": "session"},
                "browser_info_tiktok": {
                    "User-Agent": "Mozilla/5.0",
                    "device_id": "1234567890",
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_device_id(settings_path) == "1234567890"
