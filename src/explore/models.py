"""TikTok Explore 采集数据模型。

物理隔离于业务库：本模块的表注册在独立的 ``TiktokBase`` metadata 上，
由 ``src.explore.db.init_tiktok_db`` 在 tiktok 库创建，避免与项目其它 SQLAlchemy metadata 冲突。
字段对应采集字段 + S3 上传定位 + 下载/上传状态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TiktokBase(AsyncAttrs, DeclarativeBase):
    """TikTok 专属表的独立声明式基类，metadata 与业务 Base 隔离。"""

    pass


class TiktokExploreItemModel(TiktokBase):
    """TikTok Explore 采集条目：元数据 + 下载/上传状态 + S3 定位。

    幂等键为 ``video_id``（TikTok 视频 ID），重复采集同一分类走 upsert，
    不会产生重复行。原始 item 存入 ``raw_payload`` 以备字段扩展，免改表。
    """

    __tablename__ = "tiktok_explore_items"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="自增ID",
    )
    video_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="TikTok视频ID，幂等键",
    )

    category_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Explore分类",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="视频描述",
    )

    author_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="作者唯一ID",
    )
    author_nickname: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="作者昵称",
    )

    create_time: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="视频创建时间(秒级时间戳)",
    )

    play_count: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="播放数",
    )
    like_count: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="点赞数",
    )
    share_count: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="分享数",
    )
    comment_count: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="评论数",
    )

    play_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="播放URL",
    )
    download_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="下载URL",
    )
    music_title: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="音乐标题",
    )
    music_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="音乐URL",
    )

    hashtags: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="话题标签数组",
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="原始采集item，备扩展",
    )

    # S3 上传定位
    s3_provider: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="S3 provider_key，当前固定为 s3",
    )
    s3_bucket: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="目标bucket，默认跟provider",
    )
    s3_prefix: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="上传key前缀",
    )
    s3_object_key: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="上传成功后的完整key",
    )

    # 下载状态
    media_path: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="本地下载相对路径",
    )
    media_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="下载文件字节数",
    )
    is_downloaded: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        comment="是否已下载:0否1是",
    )

    # 上传状态
    is_uploaded: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        comment="是否已上传:0否1成功2失败",
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="上传完成时间",
    )

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="创建时间",
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        comment="更新时间",
    )

    __table_args__ = (
        Index("idx_tiktok_is_uploaded", "is_uploaded"),
        Index("idx_tiktok_is_downloaded", "is_downloaded"),
        Index("idx_tiktok_category_type", "category_type"),
        Index("idx_tiktok_s3_provider", "s3_provider"),
    )
