import asyncio
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.explore import upload as upload_module
from src.explore.models import TiktokBase, TiktokExploreItemModel
from src.explore.repository import TiktokExploreItemRepository


def test_mark_media_abandoned_sets_terminal_state() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await _create_tables(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            session.add(
                TiktokExploreItemModel(
                    id=1,
                    video_id="missing-video",
                    is_downloaded=1,
                    is_uploaded=2,
                    media_path="downloads/missing/missing.mp4",
                    media_bytes=123,
                    uploaded_at=datetime.now(),
                )
            )
            session.add(
                TiktokExploreItemModel(
                    id=2,
                    video_id="uploaded-video",
                    is_downloaded=1,
                    is_uploaded=1,
                )
            )
            await session.commit()

            repository = TiktokExploreItemRepository(session)
            assert await repository.mark_media_abandoned("missing-video")
            # 已上传成功的行不在 claim(=2)状态，不会被放弃标记触碰。
            assert not await repository.mark_media_abandoned("uploaded-video")
            await session.commit()

            item = await session.scalar(
                select(TiktokExploreItemModel).where(
                    TiktokExploreItemModel.video_id == "missing-video"
                )
            )
            uploaded_item = await session.scalar(
                select(TiktokExploreItemModel).where(
                    TiktokExploreItemModel.video_id == "uploaded-video"
                )
            )

        assert item is not None
        assert item.is_downloaded == -1
        assert item.is_uploaded == 0
        assert item.uploaded_at is None
        assert item.media_path is None
        assert item.media_bytes is None
        assert uploaded_item is not None
        assert uploaded_item.is_downloaded == 1
        assert uploaded_item.is_uploaded == 1
        await engine.dispose()

    asyncio.run(run())


def test_update_media_revives_abandoned_and_resets_stale_upload() -> None:
    """重采下载成功后:已放弃/上传失败卡死的行复活进上传队列;已上传的保持。"""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await _create_tables(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            session.add(
                TiktokExploreItemModel(
                    id=1,
                    video_id="abandoned-video",
                    is_downloaded=-1,
                    is_uploaded=0,
                )
            )
            session.add(
                TiktokExploreItemModel(
                    id=2,
                    video_id="stuck-video",
                    is_downloaded=1,
                    is_uploaded=2,
                )
            )
            session.add(
                TiktokExploreItemModel(
                    id=3,
                    video_id="uploaded-video",
                    is_downloaded=1,
                    is_uploaded=1,
                )
            )
            await session.commit()

            repository = TiktokExploreItemRepository(session)
            for video_id in ("abandoned-video", "stuck-video", "uploaded-video"):
                assert await repository.update_media(
                    video_id, f"downloads/{video_id}.mp4", 42, 1
                )
            await session.commit()

            rows = {
                row.video_id: row
                for row in await session.scalars(select(TiktokExploreItemModel))
            }

        assert rows["abandoned-video"].is_downloaded == 1
        assert rows["abandoned-video"].is_uploaded == 0
        assert rows["stuck-video"].is_uploaded == 0
        assert rows["uploaded-video"].is_uploaded == 1
        await engine.dispose()

    asyncio.run(run())


def test_update_media_failure_keeps_zero_download_state() -> None:
    """下载失败时不清除既有上传状态（终态/成功标记不由失败路径触碰）。"""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await _create_tables(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            session.add(
                TiktokExploreItemModel(
                    id=1,
                    video_id="failed-video",
                    is_downloaded=0,
                    is_uploaded=0,
                    media_path="stale/path.mp4",
                )
            )
            await session.commit()

            repository = TiktokExploreItemRepository(session)
            assert await repository.update_media("failed-video", None, None, 0)
            await session.commit()

            item = await session.scalar(select(TiktokExploreItemModel))

        assert item is not None
        assert item.is_downloaded == 0
        assert item.media_path is None
        await engine.dispose()

    asyncio.run(run())


def test_missing_media_is_abandoned_not_uploaded(monkeypatch) -> None:
    records = [
        {
            "video_id": "missing-video",
            "media_path": "downloads/missing-video/missing-video.mp4",
            "category_type": "100",
            "s3_bucket": None,
            "s3_prefix": None,
            "s3_provider": "s3",
        }
    ]
    repository_calls = {"abandoned": []}

    class FakeRepository:
        def __init__(self, session):
            pass

        async def get_pending_upload(self, **kwargs):
            return records

        async def claim_batch(self, video_ids):
            return len(video_ids)

        async def mark_media_abandoned(self, video_id):
            repository_calls["abandoned"].append(video_id)
            return True

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeGateway:
        async def upload_file(self, local_file_path, remote_s3_path, bucket=None):
            raise AssertionError("missing media must not be uploaded")

        def close(self):
            pass

    async def noop_init():
        pass

    monkeypatch.setattr(upload_module, "init_tiktok_db", noop_init)
    monkeypatch.setattr(
        upload_module, "get_tiktok_db_session", lambda: SessionContext()
    )
    monkeypatch.setattr(upload_module, "TiktokExploreItemRepository", FakeRepository)
    monkeypatch.setattr(upload_module, "build_category_slug_map", lambda _: {})

    result = asyncio.run(
        upload_module.upload_pending_explore(
            media_root=Path("."),
            gateway=FakeGateway(),
            categories_path=Path("categories.json"),
        )
    )

    assert result == {"success": 0, "failed": 0, "skipped": 1}
    assert repository_calls["abandoned"] == ["missing-video"]


async def _create_tables(engine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(TiktokBase.metadata.create_all)
