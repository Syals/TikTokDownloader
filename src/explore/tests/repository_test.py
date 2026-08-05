from src.explore.repository import (
    _UPSERT_UPDATE_COLUMNS,
    _row_from_item,
    _to_optional_int,
)


def test_to_optional_int_coerces_and_skips_invalid() -> None:
    assert _to_optional_int(None) is None
    assert _to_optional_int("") is None
    assert _to_optional_int("123") == 123
    assert _to_optional_int(456) == 456
    assert _to_optional_int("abc") is None
    assert _to_optional_int({}) is None


def test_row_from_item_maps_fields_and_copies_payload() -> None:
    item = {
        "id": "vid123",
        "category_type": "119",
        "description": "hello",
        "create_time": "1699999999",
        "author_id": "a1",
        "author_nickname": "nick",
        "play_count": "100",
        "like_count": "200",
        "share_count": "300",
        "comment_count": "400",
        "play_url": "http://play",
        "download_url": "http://dl",
        "music_title": "music",
        "music_url": "http://music",
        "hashtags": ["a", "b"],
        "extra": "kept",
    }
    row = _row_from_item(
        item,
        default_s3_provider="s3",
        default_s3_prefix="tiktok/explore",
    )

    assert row["video_id"] == "vid123"
    assert row["category_type"] == "119"
    assert row["description"] == "hello"
    assert row["create_time"] == 1_699_999_999
    assert row["author_id"] == "a1"
    assert row["author_nickname"] == "nick"
    assert row["play_count"] == 100
    assert row["like_count"] == 200
    assert row["share_count"] == 300
    assert row["comment_count"] == 400
    assert row["play_url"] == "http://play"
    assert row["download_url"] == "http://dl"
    assert row["music_title"] == "music"
    assert row["music_url"] == "http://music"
    assert row["hashtags"] == ["a", "b"]
    assert row["s3_provider"] == "s3"
    assert row["s3_prefix"] == "tiktok/explore"
    assert row["raw_payload"] == item
    assert row["raw_payload"] is not item


def test_upsert_columns_exclude_category_type_for_s3_key_stability() -> None:
    """video_id 是全局幂等键，category_type 不应在重采时被刷新，
    否则跨分类重复视频会导致 S3 key 路径与 category_type 不一致。
    """
    assert "category_type" not in _UPSERT_UPDATE_COLUMNS
