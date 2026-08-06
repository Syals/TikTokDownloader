"""回归测试：tiktok_explore_batch 的解析与配置函数。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._batch_config import (  # noqa: E402
    load_batch_config,
)
from poc.explore._categories import (  # noqa: E402
    save_category_names,
)
from poc.explore.tiktok_explore_batch import (  # noqa: E402
    DEFAULT_CATEGORIES,
    DEFAULT_CATEGORY_DELAY,
    DEFAULT_CONCURRENCY,
    DEFAULT_COUNT,
    DEFAULT_DELAY,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RETRY,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_CHUNK_KB,
    main,
    parse_args,
    parse_categories,
    resolve_config,
    run_batch,
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


def test_parse_args_default_sentinels() -> None:
    """可配置字段默认 None（哨兵），以支持 CLI > 配置 > 内置默认 的合并。"""
    args = parse_args([])
    assert args.live is False
    assert args.config is None
    assert args.profile is None
    assert args.categories is None
    assert args.download is None
    assert args.no_db is None
    assert args.count is None
    assert args.max_pages is None
    assert args.output_dir is None


def test_parse_args_accepts_category_list() -> None:
    args = parse_args(["--categories", "119,120"])
    assert args.categories == "119,120"


def test_parse_args_boolean_optional() -> None:
    assert parse_args(["--no-download"]).download is False
    assert parse_args(["--download"]).download is True
    assert parse_args(["--db"]).no_db is False
    assert parse_args(["--no-db"]).no_db is True


def test_resolve_without_config_uses_builtin_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TIKTOK_POC_PROXY", raising=False)
    resolved = resolve_config(parse_args([]), {})
    assert resolved["categories"] == DEFAULT_CATEGORIES
    assert resolved["count"] == DEFAULT_COUNT
    assert resolved["max_pages"] == DEFAULT_MAX_PAGES
    assert resolved["delay"] == DEFAULT_DELAY
    assert resolved["category_delay"] == DEFAULT_CATEGORY_DELAY
    assert resolved["concurrency"] == DEFAULT_CONCURRENCY
    assert resolved["max_retry"] == DEFAULT_MAX_RETRY
    assert resolved["chunk_kb"] == DEFAULT_CHUNK_KB
    assert resolved["url_mode"] == "play_url"
    assert resolved["download"] is False
    assert resolved["no_db"] is False
    assert resolved["output_dir"] == str(DEFAULT_OUTPUT_DIR)
    assert resolved["proxy"] is None


def test_resolve_merges_defaults_then_profile(project_tmp: Path) -> None:
    cfg_path = project_tmp / "batch_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "defaults": {"download": True, "concurrency": 3},
                "profiles": {"night": {"categories": "104,112", "count": 10}},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_batch_config(cfg_path, "night")
    resolved = resolve_config(parse_args([]), cfg)
    assert resolved["categories"] == "104,112"
    assert resolved["count"] == 10
    assert resolved["download"] is True
    assert resolved["concurrency"] == 3
    assert resolved["max_pages"] == DEFAULT_MAX_PAGES


def test_resolve_cli_overrides_profile(project_tmp: Path) -> None:
    cfg_path = project_tmp / "batch_config.json"
    cfg_path.write_text(
        json.dumps({"profiles": {"night": {"count": 10, "download": True}}}),
        encoding="utf-8",
    )
    cfg = load_batch_config(cfg_path, "night")
    resolved = resolve_config(parse_args(["--count", "99", "--no-download"]), cfg)
    assert resolved["count"] == 99
    assert resolved["download"] is False
    assert resolved["categories"] == DEFAULT_CATEGORIES


def test_resolve_config_path_string_converts_later(project_tmp: Path) -> None:
    cfg_path = project_tmp / "batch_config.json"
    cfg_path.write_text(
        json.dumps({"profiles": {"night": {"output_dir": ".output/x"}}}),
        encoding="utf-8",
    )
    cfg = load_batch_config(cfg_path, "night")
    resolved = resolve_config(parse_args([]), cfg)
    assert resolved["output_dir"] == ".output/x"


def test_resolve_env_proxy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_POC_PROXY", "http://127.0.0.1:7890")
    resolved = resolve_config(parse_args([]), {})
    assert resolved["proxy"] == "http://127.0.0.1:7890"


def test_resolve_cli_proxy_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_POC_PROXY", "http://env:7890")
    resolved = resolve_config(parse_args(["--proxy", "http://cli:7890"]), {})
    assert resolved["proxy"] == "http://cli:7890"


def test_resolve_config_proxy_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_POC_PROXY", "http://env:7890")
    resolved = resolve_config(parse_args([]), {"proxy": "http://cfg:7890"})
    assert resolved["proxy"] == "http://cfg:7890"


def test_resolve_explicit_empty_proxy_stays_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_POC_PROXY", "http://env:7890")
    resolved = resolve_config(parse_args(["--proxy", ""]), {})
    assert resolved["proxy"] == ""


def test_main_list_categories_missing_file_exit_2(
    monkeypatch: pytest.MonkeyPatch, project_tmp: Path
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--list-categories",
            "--categories-file",
            str(project_tmp / "nope.json"),
        ],
    )
    assert main() == 2


def test_main_list_categories_prints_mapping(
    monkeypatch: pytest.MonkeyPatch, project_tmp: Path, capsys: pytest.CaptureFixture
) -> None:
    categories_path = project_tmp / "categories.json"
    save_category_names({"112": "Sports"}, path=categories_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--list-categories", "--categories-file", str(categories_path)],
    )
    assert main() == 0
    assert "Sports" in capsys.readouterr().out


def test_main_unknown_category_exit_2(
    monkeypatch: pytest.MonkeyPatch, project_tmp: Path
) -> None:
    empty = project_tmp / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--live", "--categories", "999", "--categories-file", str(empty)],
    )
    assert main() == 2


def test_save_json_writes_pretty_json() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        path = Path(tmp_dir) / "nested" / "data.json"
        save_json(path, {"a": 1, "b": [2, 3]})
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert '"a": 1' in content
        assert content.endswith("\n")


def test_run_batch_aggregates_despite_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存储持久化失败应降级为警告，不影响该分类 JSON 聚合与状态。"""

    async def fake_collect_explore(
        *_: Any, **__: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [{"id": "v1", "category_type": "119"}], [{"page": 1}]

    async def fake_download_media(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": "v1",
                "category_type": "119",
                "ok": True,
                "media_path": "119/downloads/v1.mp4",
                "bytes": 1024,
            }
        ]

    async def fake_persist_to_db(*_: Any, **__: Any) -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.collect_explore", fake_collect_explore
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.download_media", fake_download_media
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.persist_to_db", fake_persist_to_db
    )

    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        output_dir = Path(tmp_dir)

        metadata, manifest, summary = asyncio.run(
            run_batch(
                categories=["119"],
                category_names={"119": "Singing & Dancing"},
                device_id="d1",
                browser_request_params={},
                browser_templates=None,
                count=8,
                max_pages=1,
                delay=0,
                category_delay=0,
                output_dir=output_dir,
                download=True,
                url_mode="play_url",
                concurrency=1,
                max_retry=1,
                chunk_size=1024,
                cookie={},
                user_agent="test",
                proxy=None,
                persist_and_upload=True,
                gateway="fake-gateway",
            )
        )

    assert len(metadata) == 1
    assert len(manifest) == 1
    assert metadata[0]["category_name"] == "Singing & Dancing"
    assert summary["119"]["status"] == "ok"
    assert summary["119"]["category_name"] == "Singing & Dancing"
    assert "storage_warning" in summary["119"]
    assert "RuntimeError: db unavailable" in summary["119"]["storage_warning"]
