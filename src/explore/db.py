"""TikTok Explore 数据库连接管理（单库精简版，适配云端定时任务）。

凭据统一走环境变量 ``TIKTOK_DATABASE_URL``，例如::

    mysql+asyncmy://user:pass@host:3306/tiktok?charset=utf8mb4

定时任务（CI/Cron）形态下，engine 池子设得较小，任务结束时调用
``dispose_tiktok_db`` 释放连接，避免冷启动任务泄漏连接。
"""

from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_lock = threading.Lock()


def _build_engine() -> AsyncEngine:
    url = os.environ.get("TIKTOK_DATABASE_URL")
    if not url:
        msg = "TIKTOK_DATABASE_URL environment variable is not set"
        raise RuntimeError(msg)
    return create_async_engine(
        url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        echo=False,
    )


def get_engine() -> AsyncEngine:
    """获取或创建 TikTok 数据库异步引擎（线程安全单例）。"""
    global _engine  # noqa: PLW0603
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = _build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取或创建 TikTok 数据库 session 工厂（线程安全单例）。"""
    global _session_factory  # noqa: PLW0603
    if _session_factory is None:
        with _lock:
            if _session_factory is None:
                _session_factory = async_sessionmaker(
                    bind=get_engine(),
                    class_=AsyncSession,
                    autoflush=False,
                    expire_on_commit=False,
                )
    return _session_factory


@asynccontextmanager
async def get_tiktok_db_session() -> AsyncIterator[AsyncSession]:
    """获取 TikTok 数据库会话（自动提交/回滚/关闭）。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_tiktok_db() -> None:
    """在 tiktok 库中创建 TikTok 专属表（幂等，表已存在时跳过）。"""
    from src.explore.models import TiktokBase

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(TiktokBase.metadata.create_all)


async def dispose_tiktok_db() -> None:
    """释放 engine 与 session factory（定时任务结束时建议调用）。"""
    global _engine, _session_factory  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    _session_factory = None
