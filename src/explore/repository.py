"""TikTok Explore 采集条目数据访问层。

风格对齐 ``media_pipeline`` 的 ``TiktokExploreItemRepository``：构造注入
``AsyncSession``，状态标记用 update。幂等写入走 MySQL
``INSERT ... ON DUPLICATE KEY UPDATE``（按 ``video_id``），重采只刷新元数据列，
不覆盖已维护的下载/上传状态。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.explore.models import TiktokExploreItemModel

# upsert 时刷新的列：采集产物（每次重采都应更新）。
# 刻意排除 id/video_id/created_at，以及下载/上传状态列（由其他阶段维护）。
# 同时排除 category_type：video_id 是全局幂等键，跨 Explore 分类的同一视频
# 只保留首次写入的 category_type，避免 S3 key 路径与 category_type 漂移。
_UPSERT_UPDATE_COLUMNS: tuple[str, ...] = (
    "description",
    "author_id",
    "author_nickname",
    "create_time",
    "play_count",
    "like_count",
    "share_count",
    "comment_count",
    "play_url",
    "download_url",
    "music_title",
    "music_url",
    "hashtags",
    "raw_payload",
    "s3_provider",
    "s3_prefix",
)


def _to_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_from_item(
    item: dict[str, Any],
    *,
    default_s3_provider: str | None,
    default_s3_prefix: str | None,
) -> dict[str, Any]:
    """将采集 dict 映射为列字典。

    ``item["id"]`` 映射到 ``video_id``，其余采集字段同名。原始 dict 快照存入
    ``raw_payload`` 以备字段扩展。计数/时间戳统一转 int。
    """
    return {
        "video_id": item.get("id"),
        "category_type": item.get("category_type"),
        "description": item.get("description"),
        "create_time": _to_optional_int(item.get("create_time")),
        "author_id": item.get("author_id"),
        "author_nickname": item.get("author_nickname"),
        "play_count": _to_optional_int(item.get("play_count")),
        "like_count": _to_optional_int(item.get("like_count")),
        "share_count": _to_optional_int(item.get("share_count")),
        "comment_count": _to_optional_int(item.get("comment_count")),
        "play_url": item.get("play_url"),
        "download_url": item.get("download_url"),
        "music_title": item.get("music_title"),
        "music_url": item.get("music_url"),
        "hashtags": item.get("hashtags"),
        "raw_payload": dict(item),
        "s3_provider": item.get("s3_provider") or default_s3_provider,
        "s3_prefix": item.get("s3_prefix") or default_s3_prefix,
    }


class TiktokExploreItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_batch(
        self,
        items: Sequence[dict[str, Any]],
        *,
        default_s3_provider: str | None = None,
        default_s3_prefix: str | None = None,
    ) -> int:
        """按 ``video_id`` 幂等写入/更新一批采集条目。"""
        if not items:
            return 0
        rows = [
            _row_from_item(
                item,
                default_s3_provider=default_s3_provider,
                default_s3_prefix=default_s3_prefix,
            )
            for item in items
        ]
        stmt = mysql_insert(TiktokExploreItemModel).values(rows)
        stmt = stmt.on_duplicate_key_update(
            {col: stmt.inserted[col] for col in _UPSERT_UPDATE_COLUMNS}
        )
        result = await self.session.execute(stmt)
        return getattr(result, "rowcount", None) or 0

    async def claim_batch(self, video_ids: Sequence[str]) -> int:
        """将待上传条目批量标记为处理中（is_uploaded=2）。

        上传前先 claim，即使最终 mark_uploaded 失败，记录也不会被再次消费，
        避免同一文件重复上传到 S3。
        """
        if not video_ids:
            return 0
        stmt = (
            update(TiktokExploreItemModel)
            .where(TiktokExploreItemModel.video_id.in_(video_ids))
            .values(is_uploaded=2, uploaded_at=datetime.now())
        )
        result = await self.session.execute(stmt)
        return getattr(result, "rowcount", None) or 0

    async def exists(self, video_id: str) -> bool:
        stmt = (
            select(TiktokExploreItemModel.id)
            .where(TiktokExploreItemModel.video_id == video_id)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def update_media(
        self,
        video_id: str,
        media_path: str | None,
        media_bytes: int | None,
        is_downloaded: int,
    ) -> bool:
        """下载完成后回填本地媒体路径与字节数。"""
        stmt = (
            update(TiktokExploreItemModel)
            .where(TiktokExploreItemModel.video_id == video_id)
            .values(
                media_path=media_path,
                media_bytes=media_bytes,
                is_downloaded=is_downloaded,
            )
        )
        result = await self.session.execute(stmt)
        return (getattr(result, "rowcount", None) or 0) > 0

    async def set_upload_target(
        self,
        video_id: str,
        provider: str,
        prefix: str,
        bucket: str | None = None,
    ) -> bool:
        """指定上传后端与前缀。"""
        values: dict[str, Any] = {"s3_provider": provider, "s3_prefix": prefix}
        if bucket is not None:
            values["s3_bucket"] = bucket
        stmt = (
            update(TiktokExploreItemModel)
            .where(TiktokExploreItemModel.video_id == video_id)
            .values(**values)
        )
        result = await self.session.execute(stmt)
        return (getattr(result, "rowcount", None) or 0) > 0

    async def mark_uploaded(self, video_id: str, object_key: str) -> bool:
        """标记上传成功并回填完整对象 key。"""
        stmt = (
            update(TiktokExploreItemModel)
            .where(TiktokExploreItemModel.video_id == video_id)
            .values(
                is_uploaded=1,
                s3_object_key=object_key,
                uploaded_at=datetime.now(),
            )
        )
        result = await self.session.execute(stmt)
        return (getattr(result, "rowcount", None) or 0) > 0

    async def mark_upload_failed(self, video_id: str) -> bool:
        """标记上传失败（``is_uploaded=2``），可由上传器重试消费。"""
        stmt = (
            update(TiktokExploreItemModel)
            .where(TiktokExploreItemModel.video_id == video_id)
            .values(is_uploaded=2, uploaded_at=datetime.now())
        )
        result = await self.session.execute(stmt)
        return (getattr(result, "rowcount", None) or 0) > 0

    async def get_pending_upload(
        self,
        limit: int = 100,
        provider: str | None = None,
        category_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """取已下载未上传的条目，供上传消费器处理。"""
        stmt = (
            select(
                TiktokExploreItemModel.id,
                TiktokExploreItemModel.video_id,
                TiktokExploreItemModel.media_path,
                TiktokExploreItemModel.s3_provider,
                TiktokExploreItemModel.s3_prefix,
                TiktokExploreItemModel.category_type,
            )
            .where(
                TiktokExploreItemModel.is_downloaded == 1,
                TiktokExploreItemModel.is_uploaded == 0,
            )
            .order_by(TiktokExploreItemModel.id)
            .limit(limit)
        )
        if provider is not None:
            stmt = stmt.where(TiktokExploreItemModel.s3_provider == provider)
        if category_type is not None:
            stmt = stmt.where(TiktokExploreItemModel.category_type == category_type)
        result = await self.session.execute(stmt)
        return [
            {
                "id": row.id,
                "video_id": row.video_id,
                "media_path": row.media_path,
                "s3_provider": row.s3_provider,
                "s3_prefix": row.s3_prefix,
                "category_type": row.category_type,
            }
            for row in result.all()
        ]
