"""回归测试：tiktok_explore_batch 的解析与配置函数。"""

import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore.tiktok_explore_batch import (  # noqa: E402
    DEFAULT_CATEGORIES,
    DEFAULT_OUTPUT_DIR,
    parse_args,
    parse_categories,
    save_json,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("119,120,123", ["119", "120", "123"]),
        (" 119 , 120 ,123 ", ["119", "120", "123"]),
        ("120,120,123", ["120", "123"]),
        ("", []),
        (",,,", []),
    ],
)
def test_parse_categories(text: str, expected: list[str]) -> None:
    assert parse_categories(text) == expected


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.live is False
    assert args.download is False
    assert args.categories == DEFAULT_CATEGORIES
    assert args.output_dir == DEFAULT_OUTPUT_DIR


def test_parse_args_accepts_category_list() -> None:
    args = parse_args(["--categories", "119,120"])
    assert args.categories == "119,120"


def test_save_json_writes_pretty_json() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        path = Path(tmp_dir) / "nested" / "data.json"
        save_json(path, {"a": 1, "b": [2, 3]})
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert '"a": 1' in content
        assert content.endswith("\n")
