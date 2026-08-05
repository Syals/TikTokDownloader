from unittest.mock import MagicMock

import pytest

from src.explore.s3 import S3Uploader, _resolve_bucket


def test_resolve_bucket_prefers_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_S3_BUCKET", "env-bucket")
    assert _resolve_bucket("explicit-bucket") == "explicit-bucket"


def test_resolve_bucket_falls_back_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIKTOK_S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
    monkeypatch.setenv("AWS_S3_BUCKET", "aws-bucket")
    assert _resolve_bucket(None) == "aws-bucket"


def test_resolve_bucket_raises_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TIKTOK_S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="S3 bucket not configured"):
        _resolve_bucket(None)


@pytest.mark.anyio
async def test_upload_file_uses_bucket_override() -> None:
    client = MagicMock()
    uploader = S3Uploader(bucket="default-bucket", client=client)
    result = await uploader.upload_file(
        "/local/file.mp4", "remote/key.mp4", bucket="override-bucket"
    )

    assert result is True
    client.upload_file.assert_called_once_with(
        "/local/file.mp4", "override-bucket", "remote/key.mp4"
    )


@pytest.mark.anyio
async def test_upload_file_uses_default_bucket_when_override_is_none() -> None:
    client = MagicMock()
    uploader = S3Uploader(bucket="default-bucket", client=client)
    result = await uploader.upload_file("/local/file.mp4", "remote/key.mp4")

    assert result is True
    client.upload_file.assert_called_once_with(
        "/local/file.mp4", "default-bucket", "remote/key.mp4"
    )
