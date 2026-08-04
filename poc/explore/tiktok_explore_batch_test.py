"""回归测试：tiktok_explore_batch 的解析与配置函数。"""

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore.tiktok_explore_batch import (  # noqa: E402
    DEFAULT_CATEGORIES,
    DEFAULT_OUTPUT_DIR,
    parse_args,
    parse_categories,
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
        client = AsyncMock()

        metadata, manifest, summary = asyncio.run(
            run_batch(
                client,
                categories=["119"],
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
                persist_and_upload=True,
                gateway="fake-gateway",
            )
        )

    assert len(metadata) == 1
    assert len(manifest) == 1
    assert summary["119"]["status"] == "ok"
    assert "storage_warning" in summary["119"]
    assert "RuntimeError: db unavailable" in summary["119"]["storage_warning"]
