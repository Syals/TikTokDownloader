"""匿名 TikTok Explore 批处理的离线回归测试。"""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore.tiktok_explore_anonymous import (  # noqa: E402
    AnonymousBootstrap,
    AnonymousValidationError,
    _client_kwargs,
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

    with pytest.raises(AnonymousValidationError, match="一条 media_path"):
        resolve_config(parse_args(["--url-mode", "all"]), {})


def test_http_client_preserves_tls_verification_and_shared_proxy() -> None:
    kwargs = _client_kwargs(_bootstrap(), "http://proxy.example:8080")
    assert kwargs["proxy"] == "http://proxy.example:8080"
    assert "verify" not in kwargs


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

    with pytest.raises(AnonymousValidationError, match="证据门"):
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

    assert not (project_tmp / "104" / "metadata.json").exists()
