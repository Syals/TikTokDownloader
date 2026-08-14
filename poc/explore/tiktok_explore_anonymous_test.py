"""匿名 TikTok Explore 批处理的离线回归测试。"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore.tiktok_explore_anonymous import (  # noqa: E402
    AnonymousBootstrap,
    AnonymousValidationError,
    BrowserCollection,
    _client_kwargs,
    _evidence_gate_diagnostics,
    main,
    parse_args,
    reject_login_cookies,
    resolve_config,
    run_batch,
    validate_bootstrap,
)


def _bootstrap() -> AnonymousBootstrap:
    return AnonymousBootstrap(
        cookie={"msToken": "ephemeral"},
        user_agent="test-agent",
        browser_request_params={
            "clientABVersions": "versions",
            "odinId": "odin",
            "is_new_user": "false",
            "video_encoding": "h264",
        },
        templates=(
            [("categoryType", "104"), ("pullType", "1")],
            [
                ("categoryType", "104"),
                ("pullType", "2"),
                ("msToken", "ephemeral"),
            ],
        ),
    )


def test_login_cookie_rejects_the_whole_context() -> None:
    with pytest.raises(AnonymousValidationError, match="sessionid_ss"):
        reject_login_cookies([{"name": "sessionid_ss", "value": "secret"}])


def test_validate_bootstrap_requires_all_browser_fields_and_templates() -> None:
    state = _bootstrap()
    assert validate_bootstrap(state) is state

    state.browser_request_params.pop("odinId")
    with pytest.raises(AnonymousValidationError, match="浏览器字段"):
        validate_bootstrap(state)


def test_resolve_config_defaults_to_no_db_and_rejects_all_urls() -> None:
    resolved = resolve_config(parse_args([]), {})
    assert resolved["no_db"] is True
    assert resolved["url_mode"] == "play_url"
    assert resolved["browser_pages"] is False
    assert resolve_config(parse_args(["--browser-pages"]), {})["browser_pages"] is True

    with pytest.raises(AnonymousValidationError, match="一条 media_path"):
        resolve_config(parse_args(["--url-mode", "all"]), {})


def test_http_client_preserves_tls_verification_and_shared_proxy() -> None:
    kwargs = _client_kwargs(_bootstrap(), "http://proxy.example:8080")
    assert kwargs["proxy"] == "http://proxy.example:8080"
    assert "verify" not in kwargs


@pytest.mark.parametrize(
    ("metadata", "report", "expected_reason"),
    [
        (
            [{"id": "1"}],
            [{"http_status": 200, "json": True, "item_count": 1, "has_more": False}],
            "视频元数据中没有 play_url 或 download_url",
        ),
        (
            [{"id": "1", "play_url": "https://cdn.example/1.mp4"}],
            [{"http_status": 403, "json": False, "item_count": 0, "has_more": False}],
            "首响应 HTTP 状态=403（期望 200）",
        ),
        (
            [{"id": "1", "play_url": "https://cdn.example/1.mp4"}],
            [
                {
                    "http_status": 200,
                    "json": True,
                    "item_count": 1,
                    "has_more": True,
                    "item_id_hashes": ["first"],
                }
            ],
            "首响应 has_more=true，但未获得第 2 页响应报告",
        ),
        (
            [{"id": "1", "play_url": "https://cdn.example/1.mp4"}],
            [
                {
                    "http_status": 200,
                    "json": True,
                    "item_count": 1,
                    "has_more": True,
                    "item_id_hashes": ["same"],
                },
                {"item_id_hashes": ["same"]},
            ],
            "翻页响应没有包含相对首屏的新视频 ID",
        ),
    ],
)
def test_evidence_gate_diagnostics_identifies_the_failed_requirement(
    metadata: list[dict[str, object]],
    report: list[dict[str, object]],
    expected_reason: str,
) -> None:
    diagnostics = _evidence_gate_diagnostics(metadata, report)

    assert expected_reason in diagnostics["reasons"]


def test_harvest_categories_requires_live_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fake_harvest(*_: Any, **__: Any) -> dict[str, str]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_anonymous.harvest_anonymous_categories",
        fake_harvest,
    )
    assert main(["--harvest-categories"]) == 2
    assert not called


def test_harvest_categories_saves_mapping_to_file(
    monkeypatch: pytest.MonkeyPatch,
    project_tmp: Path,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_harvest(*_, **kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        path = kwargs["categories_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"100": "Anime & Comics"}, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        return {"100": "Anime & Comics"}

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_anonymous.harvest_anonymous_categories",
        fake_harvest,
    )

    categories_path = project_tmp / "categories.json"
    assert (
        main(
            [
                "--live",
                "--harvest-categories",
                "--categories-file",
                str(categories_path),
            ]
        )
        == 0
    )
    assert captured["categories_file"] == categories_path
    assert categories_path.is_file()
    assert "Anime & Comics" in categories_path.read_text(encoding="utf-8")


def test_run_batch_deduplicates_media_but_keeps_each_category_metadata(
    monkeypatch: pytest.MonkeyPatch, project_tmp: Path
) -> None:
    async def fake_collect_explore(
        *_: Any, **kwargs: Any
    ) -> tuple[list[dict], list[dict]]:
        category = kwargs["category_type"]
        return (
            [
                {
                    "id": "same",
                    "category_type": category,
                    "play_url": "https://cdn.example/same.mp4",
                },
                {
                    "id": f"only-{category}",
                    "category_type": category,
                    "play_url": "https://cdn.example/only.mp4",
                },
            ],
            [{"http_status": 200, "json": True, "item_count": 2, "has_more": False}],
        )

    async def fake_download_media(
        _: Any, metadata: list[dict], **__: Any
    ) -> list[dict]:
        return [
            {
                "id": item["id"],
                "category_type": item["category_type"],
                "media_path": f"downloads/{item['id']}.mp4",
                "ok": True,
                "bytes": 1,
            }
            for item in metadata
        ]

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_anonymous.collect_explore", fake_collect_explore
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_anonymous.download_media", fake_download_media
    )

    metadata, manifest, summary = asyncio.run(
        run_batch(
            categories=["104", "112"],
            category_names={"104": "One", "112": "Two"},
            state=_bootstrap(),
            count=8,
            max_pages=1,
            delay=0,
            category_delay=0,
            output_dir=project_tmp,
            download=True,
            url_mode="play_url",
            concurrency=1,
            max_retry=1,
            chunk_size=1024,
            proxy=None,
            persist_and_upload=False,
        )
    )

    assert len(metadata) == 4
    assert len(manifest) == 3
    assert summary["112"]["duplicates"] == 1
    assert (project_tmp / "112" / "metadata.json").is_file()
    assert (project_tmp / "112" / "download_manifest.json").is_file()


def test_run_batch_uses_browser_collection_when_enabled(
    monkeypatch: pytest.MonkeyPatch, project_tmp: Path
) -> None:
    async def fake_browser_collection(*_: Any, **__: Any) -> BrowserCollection:
        return BrowserCollection(
            cookie={"msToken": "ephemeral"},
            user_agent="test-agent",
            metadata=[
                {
                    "id": "browser-video",
                    "category_type": "104",
                    "play_url": "https://cdn.example/browser-video.mp4",
                }
            ],
            report=[
                {
                    "http_status": 200,
                    "json": True,
                    "item_count": 1,
                    "has_more": False,
                }
            ],
        )

    async def unexpected_http_collection(*_: Any, **__: Any) -> None:
        raise AssertionError("browser mode must not use HTTP pagination")

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_anonymous.collect_explore_in_browser",
        fake_browser_collection,
    )
    monkeypatch.setattr(
        "poc.explore.tiktok_explore_anonymous.collect_explore",
        unexpected_http_collection,
    )

    metadata, _, summary = asyncio.run(
        run_batch(
            categories=["104"],
            category_names={"104": "One"},
            state=None,
            count=8,
            max_pages=3,
            delay=0,
            category_delay=0,
            output_dir=project_tmp,
            download=False,
            url_mode="play_url",
            concurrency=1,
            max_retry=1,
            chunk_size=1024,
            proxy=None,
            persist_and_upload=False,
            browser_pages=True,
        )
    )

    assert [item["id"] for item in metadata] == ["browser-video"]
    assert summary["104"]["pages"] == 1


def test_run_batch_blocks_side_effects_when_evidence_gate_fails(
    monkeypatch: pytest.MonkeyPatch, project_tmp: Path
) -> None:
    async def fake_collect_explore(*_: Any, **__: Any) -> tuple[list[dict], list[dict]]:
        return [], [
            {"http_status": 200, "json": True, "item_count": 0, "has_more": False}
        ]

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_anonymous.collect_explore", fake_collect_explore
    )

    with pytest.raises(AnonymousValidationError, match="证据门") as error:
        asyncio.run(
            run_batch(
                categories=["104"],
                category_names={"104": "One"},
                state=_bootstrap(),
                count=8,
                max_pages=1,
                delay=0,
                category_delay=0,
                output_dir=project_tmp,
                download=True,
                url_mode="play_url",
                concurrency=1,
                max_retry=1,
                chunk_size=1024,
                proxy=None,
                persist_and_upload=False,
            )
        )

    assert "未提取到任何视频元数据" in str(error.value)
    assert not (project_tmp / "104" / "metadata.json").exists()
    diagnostic = json.loads(
        (project_tmp / "104" / "evidence_failure.json").read_text(encoding="utf-8")
    )
    assert "未提取到任何视频元数据" in diagnostic["reasons"]
    assert diagnostic["report"][0]["item_count"] == 0


def test_run_batch_records_http_status_errors_from_direct_replay(
    monkeypatch: pytest.MonkeyPatch, project_tmp: Path
) -> None:
    async def fake_collect_explore(*_: Any, **__: Any) -> tuple[list[dict], list[dict]]:
        request = httpx.Request("GET", "https://www.tiktok.com/api/explore/item_list/")
        response = httpx.Response(
            403, headers={"content-type": "text/html"}, request=request
        )
        raise httpx.HTTPStatusError("Forbidden", request=request, response=response)

    monkeypatch.setattr(
        "poc.explore.tiktok_explore_anonymous.collect_explore", fake_collect_explore
    )

    with pytest.raises(AnonymousValidationError, match="直接 HTTP 请求失败") as error:
        asyncio.run(
            run_batch(
                categories=["104"],
                category_names={"104": "One"},
                state=_bootstrap(),
                count=8,
                max_pages=1,
                delay=0,
                category_delay=0,
                output_dir=project_tmp,
                download=True,
                url_mode="play_url",
                concurrency=1,
                max_retry=1,
                chunk_size=1024,
                proxy=None,
                persist_and_upload=False,
            )
        )

    assert "HTTPStatusError" in str(error.value)
    diagnostic = json.loads(
        (project_tmp / "104" / "evidence_failure.json").read_text(encoding="utf-8")
    )
    assert diagnostic["request_error_type"] == "HTTPStatusError"
    assert diagnostic["report"][0]["http_status"] == 403
