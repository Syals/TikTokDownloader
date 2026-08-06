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
