"""回归测试：TikTok Explore 分类映射工具。"""

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._categories import (  # noqa: E402
    find_explore_categories,
    get_category_name,
    list_categories,
    load_category_names,
    merge_explore_categories,
    save_category_names,
)

SSR_CATEGORY_LIST = {
    "v0": [
        {"text": "pc_web_explorePage_all", "name": "All", "type": "120"},
        {"text": "pc_web_explorePage_topics_sports", "name": "Sports", "type": "112"},
    ],
    "v1": [
        {"text": "pc_web_explorePage_topics_sports", "name": "Sports", "type": "112"},
        {"text": "pc_web_explorePage_topics_comedy", "name": "Comedy", "type": "104"},
    ],
}


def test_merge_explore_categories_dedupes_by_id() -> None:
    merged = merge_explore_categories(SSR_CATEGORY_LIST)
    assert merged == {"120": "All", "112": "Sports", "104": "Comedy"}


def test_merge_explore_categories_excludes_v2_dynamic_group() -> None:
    payload = {
        "v0": [{"text": "pc_web_explorePage_all", "name": "All", "type": "120"}],
        "v2": [
            {
                "text": "Web_explorePage_dynamicCategories_food",
                "name": "Food",
                "type": "207",
            },
            {
                "text": "pc_web_explorePage_topics_technology",
                "name": "Technology",
                "type": "215",
            },
        ],
    }
    assert merge_explore_categories(payload) == {"120": "All"}


def test_merge_explore_categories_rejects_malformed_entries() -> None:
    payload = {
        "v0": [
            {"name": "no id"},
            {"type": 123, "name": "numeric id"},
            {"type": "99", "name": "  "},
            "not-a-dict",
        ]
    }
    assert merge_explore_categories(payload) == {"123": "numeric id"}


def test_find_explore_categories_nested_lookup() -> None:
    ssr = {"webapp": {"app": {"nested": {"exploreCategoryList": SSR_CATEGORY_LIST}}}}
    assert find_explore_categories(ssr) is SSR_CATEGORY_LIST


def test_find_explore_categories_descends_into_lists() -> None:
    ssr = {"a": [{"nested": {"exploreCategoryList": SSR_CATEGORY_LIST}}]}
    assert find_explore_categories(ssr) is SSR_CATEGORY_LIST


def test_find_explore_categories_returns_none_when_absent() -> None:
    assert find_explore_categories({"webapp": {"user": {}}}) is None


def test_load_category_names_ignores_meta_and_non_id_keys() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        path = Path(tmp_dir) / "categories.json"
        path.write_text(
            json.dumps(
                {
                    "_meta": {"source": "explore_ssr"},
                    "112": "Sports",
                    "str-key": "Ignored",
                }
            ),
            encoding="utf-8",
        )
        assert load_category_names(path) == {"112": "Sports"}


def test_load_category_names_missing_file_returns_empty() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        assert load_category_names(Path(tmp_dir) / "missing.json") == {}


def test_get_category_name_known_and_unknown() -> None:
    mapping = {"112": "Sports"}
    assert get_category_name("112", mapping) == "Sports"
    assert get_category_name("999", mapping) is None


def test_list_categories_sorted_numeric() -> None:
    mapping = {"120": "All", "104": "Comedy", "112": "Sports"}
    assert list_categories(mapping) == [
        ("104", "Comedy"),
        ("112", "Sports"),
        ("120", "All"),
    ]


def test_save_and_reload_roundtrip() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        target = Path(tmp_dir) / "explore_categories.json"
        save_category_names({"112": "Sports"}, path=target)
        assert load_category_names(target) == {"112": "Sports"}
        raw = json.loads(target.read_text(encoding="utf-8"))
        assert raw["_meta"]["source"] == "explore_ssr"
        assert "harvested_at" in raw["_meta"]


def test_save_category_names_merges_existing() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        target = Path(tmp_dir) / "explore_categories.json"
        save_category_names({"112": "Sports"}, path=target)
        save_category_names({"104": "Comedy"}, path=target)
        assert load_category_names(target) == {"112": "Sports", "104": "Comedy"}
