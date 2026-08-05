from pathlib import Path

from src.explore.upload import build_object_key, resolve_local_path


def test_build_object_key_uses_record_prefix_when_present() -> None:
    record = {"s3_prefix": "custom/prefix", "video_id": "v1"}
    assert build_object_key(record, "file.mp4", "base") == "custom/prefix/file.mp4"


def test_build_object_key_falls_back_to_category_prefix() -> None:
    record = {"category_type": "119", "video_id": "v1"}
    assert build_object_key(record, "file.mp4", "base") == "base/119/v1/file.mp4"


def test_build_object_key_unknown_category() -> None:
    record = {"video_id": "v1"}
    assert build_object_key(record, "file.mp4", "base") == "base/unknown/v1/file.mp4"


def test_resolve_local_path_flat_absolute() -> None:
    assert resolve_local_path("/abs/file.mp4", Path("/root"), "119", "flat") == Path(
        "/abs/file.mp4"
    )


def test_resolve_local_path_flat_relative() -> None:
    assert resolve_local_path("file.mp4", Path("/root"), "119", "flat") == Path(
        "/root/file.mp4"
    )


def test_resolve_local_path_batch_relative() -> None:
    assert resolve_local_path("file.mp4", Path("/root"), "119", "batch") == Path(
        "/root/119/file.mp4"
    )


def test_resolve_local_path_batch_missing_category_falls_back_to_flat() -> None:
    assert resolve_local_path("file.mp4", Path("/root"), None, "batch") == Path(
        "/root/file.mp4"
    )
