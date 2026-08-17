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
    DEFAULT_DISK_USED_PERCENT,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RETRY,
    DEFAULT_MIN_FREE_GB,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_CHUNK_KB,
    _collect_stop_reason,
    expand_categories,
    main,
    parse_args,
    parse_categories,
    resolve_config,
    run_batch,
    save_json,
)
from src.explore.disk_check import DiskCheckResult, DiskUsageInfo  # noqa: E402


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


def test_expand_categories_all_returns_sorted_ids() -> None:
    names = {"120": "Music", "100": "Comedy", "112": "Sports"}
    assert expand_categories(["all"], names) == ["100", "112", "120"]


def test_expand_categories_all_with_empty_mapping() -> None:
    assert expand_categories(["all"], {}) == []


def test_expand_categories_passthrough_non_wildcard() -> None:
    assert expand_categories(["119", "120"], {"119": "X"}) is None
    assert expand_categories(["all", "120"], {"120": "X"}) is None


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


def test_parse_args_disk_thresholds() -> None:
    args = parse_args([])
    assert args.disk_used_percent is None
    assert args.min_free_gb is None
    args = parse_args(["--disk-used-percent", "85", "--min-free-gb", "20"])
    assert args.disk_used_percent == 85.0
    assert args.min_free_gb == 20.0


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


def test_resolve_config_disk_thresholds_merge() -> None:
    resolved = resolve_config(parse_args([]), {})
    assert resolved["disk_used_percent"] == DEFAULT_DISK_USED_PERCENT
    assert resolved["min_free_gb"] == DEFAULT_MIN_FREE_GB

    resolved = resolve_config(
        parse_args(["--disk-used-percent", "95"]),
        {"disk_used_percent": 80.0, "min_free_gb": 12.0},
    )
    assert resolved["disk_used_percent"] == 95.0
    assert resolved["min_free_gb"] == 12.0


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
                # 磁盘检测与本用例无关，显式关闭避免受宿主机磁盘水位影响。
                disk_used_percent=0,
                min_free_gb=0,
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


def test_run_batch_skips_download_when_disk_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """磁盘阈值触发时跳过该分类下载，仅保留采集与汇总。"""

    async def fake_collect_explore(
        *_: Any, **__: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [{"id": "v1", "category_type": "119"}], [{"page": 1}]

    async def unreachable_download(*_: Any, **__: Any) -> list[dict[str, Any]]:
        raise AssertionError("磁盘不足时不应调用 download_media")

    def fake_check_disk_usage(*_: Any, **__: Any) -> DiskCheckResult:
        return DiskCheckResult(
            need_cleanup=True,
            usage=DiskUsageInfo(total=100, used=95, free=5),
            used_percent=95.0,
            free_gb=5 / 1024.0**3,
        )

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.collect_explore", fake_collect_explore
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.download_media", unreachable_download
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.check_disk_usage", fake_check_disk_usage
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
            )
        )

    assert len(metadata) == 1
    assert manifest == []
    assert summary["119"]["status"] == "ok"
    assert summary["119"]["download_skipped"] == "disk_low"
    assert summary["119"]["disk"]["used_percent"] == 95.0


def test_run_batch_downloads_when_disk_sufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_collect_explore(
        *_: Any, **__: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [{"id": "v1", "category_type": "119"}], [{"page": 1}]

    async def fake_download_media(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return [{"id": "v1", "category_type": "119", "ok": True}]

    def fake_check_disk_usage(*_: Any, **__: Any) -> DiskCheckResult:
        return DiskCheckResult(
            need_cleanup=False,
            usage=DiskUsageInfo(total=100, used=10, free=90),
            used_percent=10.0,
            free_gb=90 / 1024.0**3,
        )

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.collect_explore", fake_collect_explore
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.download_media", fake_download_media
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.check_disk_usage", fake_check_disk_usage
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
            )
        )

    assert len(metadata) == 1
    assert len(manifest) == 1
    assert summary["119"]["status"] == "ok"
    assert "download_skipped" not in summary["119"]
    assert summary["119"]["disk"]["used_percent"] == 10.0


def test_run_batch_disk_check_failure_does_not_block_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """磁盘检测本身异常时降级为警告，不阻断下载。"""

    async def fake_collect_explore(
        *_: Any, **__: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [{"id": "v1", "category_type": "119"}], [{"page": 1}]

    async def fake_download_media(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return [{"id": "v1", "category_type": "119", "ok": True}]

    def broken_check_disk_usage(*_: Any, **__: Any) -> DiskCheckResult:
        raise OSError("no disk")

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.collect_explore", fake_collect_explore
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.download_media", fake_download_media
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.check_disk_usage",
        broken_check_disk_usage,
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
            )
        )

    assert len(metadata) == 1
    assert len(manifest) == 1
    assert summary["119"]["status"] == "ok"
    assert "OSError: no disk" in summary["119"]["disk_warning"]


@pytest.mark.parametrize(
    ("report", "expected_fragment"),
    [
        ([], "无分页响应"),
        ([{"json": False}], "响应非 JSON"),
        ([{"json": True, "http_status": 200}], "不是有效 JSON 对象"),
        (
            [
                {
                    "json": True,
                    "item_count": 0,
                    "item_list_missing": True,
                    "has_more": False,
                }
            ],
            "缺少 itemList",
        ),
        (
            [
                {
                    "json": True,
                    "item_count": 0,
                    "item_list_missing": True,
                    "has_more": False,
                    "payload_top_keys": ["statusCode", "statusMsg"],
                    "payload_status_probe": {"statusCode": "10205"},
                }
            ],
            "状态字段",
        ),
        (
            [
                {
                    "json": True,
                    "item_count": 0,
                    "item_list_missing": True,
                    "has_more": False,
                    "payload_top_keys": [],
                }
            ],
            "响应顶层字段",
        ),
        (
            [
                {
                    "json": True,
                    "item_count": 0,
                    "item_list_missing": False,
                    "has_more": False,
                }
            ],
            "hasMore=false",
        ),
        (
            [{"json": True, "item_count": 8, "has_more": True, "new_item_count": 0}],
            "全部重复",
        ),
        (
            [{"json": True, "item_count": 8, "has_more": True, "new_item_count": 8}],
            "max_pages 上限",
        ),
    ],
)
def test_collect_stop_reason_derives_last_page_cause(
    report: list[dict[str, Any]], expected_fragment: str
) -> None:
    assert expected_fragment in _collect_stop_reason(report)


def test_run_batch_records_upload_result_and_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """上传结果应写入 summary 并在控制台打印成功/失败/跳过计数。"""

    async def fake_collect_explore(
        *_: Any, **__: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [
            {"id": "v1", "category_type": "119"},
            {"id": "v2", "category_type": "119"},
        ], [{"page": 1}]

    async def fake_download_media(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return [
            {"id": "v1", "category_type": "119", "ok": True},
            {"id": "v2", "category_type": "119", "ok": True},
        ]

    async def fake_persist_to_db(*_: Any, **__: Any) -> None:
        return None

    async def fake_upload_pending_explore(**_: Any) -> dict[str, int]:
        return {"success": 2, "failed": 1, "skipped": 0}

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.collect_explore", fake_collect_explore
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.download_media", fake_download_media
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.persist_to_db", fake_persist_to_db
    )
    monkeypatch.setattr(
        "src.explore.upload.upload_pending_explore", fake_upload_pending_explore
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
                disk_used_percent=0,
                min_free_gb=0,
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

    assert len(metadata) == 2
    assert summary["119"]["status"] == "ok"
    assert summary["119"]["upload"] == {"success": 2, "failed": 1, "skipped": 0}
    out = capsys.readouterr().out
    assert "已持久化 2 条到数据库" in out
    assert "上传完成 成功 2 / 失败 1 / 跳过 0" in out


def test_run_batch_logs_reason_for_empty_category(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """分类返回 0 条目时应打印停止原因并写入 summary，方便定位服务端行为。"""

    async def fake_collect_explore(
        *_: Any, **__: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [], [
            {
                "page": 1,
                "json": True,
                "item_count": 0,
                "item_list_missing": False,
                "has_more": False,
            }
        ]

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.collect_explore", fake_collect_explore
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
            )
        )

    assert metadata == []
    assert manifest == []
    assert summary["119"]["status"] == "ok"
    assert "hasMore=false" in summary["119"]["collect_stop_reason"]
    out = capsys.readouterr().out
    assert "采集完成 0 条目" in out
    assert "hasMore=false" in out


def test_main_prints_empty_category_hint_and_upload_totals(
    monkeypatch: pytest.MonkeyPatch,
    project_tmp: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """main 汇总输出应包含 0 条目原因、上传总数与未启用上传提示。"""

    async def fake_collect_explore(
        *_: Any, **__: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [], [
            {
                "page": 1,
                "json": True,
                "item_count": 0,
                "item_list_missing": False,
                "has_more": False,
            }
        ]

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.collect_explore", fake_collect_explore
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.load_session",
        lambda _: ({}, "ua"),
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.load_device_id",
        lambda _: "123",
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.load_browser_request_params",
        lambda _: {},
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_batch.load_explore_templates",
        lambda _: None,
    )

    settings_path = project_tmp / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    categories_path = project_tmp / "categories.json"
    save_category_names({"119": "Singing & Dancing"}, path=categories_path)
    output_dir = project_tmp / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--live",
            "--no-db",
            "--download",
            "--categories",
            "119",
            "--settings",
            str(settings_path),
            "--categories-file",
            str(categories_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert main() == 0
    out = capsys.readouterr().out
    assert "提示: 未启用数据库持久化与 S3 上传" in out
    assert "采集完成 0 条目" in out
    assert "0 条目提示: hasMore=false" in out
    assert "已上传 0 个（失败 0, 跳过 0）" in out
    assert (output_dir / "summary.json").is_file()
