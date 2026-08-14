"""回归测试：harvest_tiktok_session 的 SSR 分类提取链路。"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._categories import (  # noqa: E402
    find_explore_categories,
    merge_explore_categories,
)
from poc.session.harvest_tiktok_session import (  # noqa: E402
    extract_ssr_json,
    login_cookie_names,
    merge_into_settings,
)

SSR_SCRIPT_TAG = (
    '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
)


def build_html(payload: object) -> str:
    return f"<html><head>{SSR_SCRIPT_TAG}{json.dumps(payload)}</script></head></html>"


def test_extract_ssr_json_hit() -> None:
    payload = {"webapp": {"nested": {"exploreCategoryList": {"v0": []}}}}
    assert extract_ssr_json(build_html(payload)) == payload


def test_extract_ssr_json_miss() -> None:
    assert extract_ssr_json("<html><body>no script</body></html>") is None


def test_extract_ssr_json_empty_input() -> None:
    assert extract_ssr_json("") is None
    assert extract_ssr_json(None) is None


def test_extract_ssr_json_bad_json() -> None:
    html = f"<html>{SSR_SCRIPT_TAG}{{ not json</script></html>"
    assert extract_ssr_json(html) is None


def test_html_to_categories_end_to_end() -> None:
    ssr = {
        "webapp": {
            "app": {"exploreCategoryList": {"v0": [{"type": "112", "name": "Sports"}]}}
        }
    }
    found = find_explore_categories(extract_ssr_json(build_html(ssr)))
    assert merge_explore_categories(found) == {"112": "Sports"}


def test_merge_into_settings_replaces_stale_login_cookies_when_logged_out() -> None:
    settings = {"cookie_tiktok": {"sessionid": "stale", "ttwid": "old"}}
    data = {
        "cookie": {"msToken": "fresh", "ttwid": "new"},
        "User-Agent": "Mozilla/5.0",
        "browser_platform": "Win32",
        "browser_language": "es",
        "browser_version": "141.0.0.0",
        "os": "windows",
        "screen_width": "1920",
        "screen_height": "1080",
        "device_id": "1234567890123456789",
    }

    merged = merge_into_settings(settings, data, replace_cookies=True)

    assert merged["cookie_tiktok"] == {"msToken": "fresh", "ttwid": "new"}


def test_login_cookie_names_includes_all_supported_login_cookies() -> None:
    assert login_cookie_names(
        {"msToken": "ephemeral", "sessionid_ss": "logged-in", "sid_tt": "logged-in"}
    ) == ["sessionid_ss", "sid_tt"]
