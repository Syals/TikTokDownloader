"""TikTok Explore 上传编排。

从 tiktok 库取「已下载未上传」的条目，经 ``S3Uploader`` 上传到 S3 后端，
回填 ``s3_object_key`` 与 ``is_uploaded`` 状态。凭据全部走环境变量，
不在代码或命令行硬编码 ACCESS_KEY/SECRET_KEY。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from src.explore.categories import build_category_slug_map
from src.explore.db import get_tiktok_db_session, init_tiktok_db
from src.explore.repository import TiktokExploreItemRepository


class _UploadGateway(Protocol):
    """上传网关最小协议，便于测试与复用连接。"""

    async def upload_file(
        self,
        local_file_path: str,
        remote_s3_path: str,
        bucket: str | None = None,
    ) -> bool: ...
    def close(self) -> None: ...


def build_object_key(
    record: Mapping[str, object],
    filename: str,
    key_prefix: str,
    category_slugs: Mapping[str, str] | None = None,
) -> str:
    """构造上传对象 key：优先用条目自带的 s3_prefix，否则按分类+video_id 组织。

    分类段优先取 ``category_slugs`` 中的英文 slug（如 ``104-comedy``），
    未命中或未提供映射时回退数字 ID。
    """
    prefix = record.get("s3_prefix")
    if isinstance(prefix, str) and prefix.strip():
        return f"{prefix.rstrip('/')}/{filename}"
    category = record.get("category_type") or "unknown"
    if category_slugs:
        category = category_slugs.get(str(category), category)
    video_id = record.get("video_id")
    return f"{key_prefix.rstrip('/')}/{category}/{video_id}/{filename}"


def resolve_local_path(
    media_path: object,
    media_root: Path,
    category_type: object,
    layout: str,
) -> Path | None:
    """把存储的相对 media_path 解析成本地路径。

    Args:
        media_path: 库中 media_path 字段。
        media_root: 本地文件根目录。
        category_type: 分类标识，batch 布局下用于拼入路径。
        layout: ``flat`` 表示 ``media_root/media_path``；``batch`` 表示
            ``media_root/category_type/media_path``。

    Returns:
        本地文件路径；空值或无法解析时返回 None。
    """
    if not isinstance(media_path, str) or not media_path.strip():
        return None
    path = Path(media_path)
    if path.is_absolute():
        return path
    if layout == "batch":
        category = (
            category_type
            if isinstance(category_type, str) and category_type.strip()
            else None
        )
        if category:
            return media_root / category / path
        logger.warning("batch layout 但 category_type 为空，降级为 flat")
    return media_root / path


async def upload_pending_explore(
    *,
    media_root: Path,
    layout: str = "flat",
    category_type: str | None = None,
    provider: str = "s3",
    key_prefix: str = "tiktok/explore",
    categories_path: Path | None = None,
    limit: int = 100,
    concurrency: int = 1,
    gateway: _UploadGateway | None = None,
) -> dict[str, int]:
    """上传 tiktok 库中已下载未上传的 Explore 条目。

    Args:
        media_root: 本地下载根目录。
        layout: 本地目录布局，``flat`` 或 ``batch``。
        category_type: 仅上传指定分类；None 表示不过滤。
        provider: 要消费的 ``s3_provider`` 值。
        key_prefix: S3 key 前缀根。
        categories_path: 分类映射 JSON 路径，用于把数字分类 ID 映射为
            英文路径段（如 ``104-comedy``）；None 使用
            ``Volume/explore_categories.json``。
        limit: 单次最多处理条目数。
        concurrency: 上传并发数；默认为 1（串行）。
        gateway: 可注入的 S3Uploader，便于测试与复用连接；
            未提供时内部创建并在函数结束时关闭。

    Returns:
        {"success": int, "failed": int, "skipped": int}
    """
    from src.explore.s3 import S3Uploader

    await init_tiktok_db()

    category_slugs = build_category_slug_map(categories_path)
    if not category_slugs:
        logger.warning("分类映射为空或缺失，S3 路径将使用数字分类目录")

    active_gateway: _UploadGateway = gateway or S3Uploader()
    should_close_gateway = gateway is None

    try:
        async with get_tiktok_db_session() as session:
            repo = TiktokExploreItemRepository(session)
            pending = await repo.get_pending_upload(
                limit=limit,
                provider=provider,
                category_type=category_type,
            )
            # 先 claim 为处理中，避免 mark_uploaded 失败时记录仍 pending，
            # 导致下次重复上传同一文件。
            if pending:
                await repo.claim_batch([str(record["video_id"]) for record in pending])
        logger.info(
            f"待上传条目: {len(pending)} (provider={provider}, "
            f"category={category_type or 'all'}, layout={layout})"
        )
        if not pending:
            return {"success": 0, "failed": 0, "skipped": 0}

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _upload_one(
            record: dict[str, Any],
        ) -> tuple[str, str, str | None]:
            """单条上传；返回 (video_id, status, key)。"""
            video_id = str(record["video_id"])
            try:
                local = resolve_local_path(
                    record.get("media_path"),
                    media_root,
                    record.get("category_type"),
                    layout,
                )
                if local is None:
                    logger.warning(f"跳过 {video_id}: media_path 为空")
                    return video_id, "skipped", None
                if not local.is_file():
                    logger.warning(f"跳过 {video_id}: 本地文件不存在 {local}")
                    return video_id, "skipped", None

                key = build_object_key(record, local.name, key_prefix, category_slugs)
                async with semaphore:
                    try:
                        ok = await active_gateway.upload_file(
                            str(local), key, record.get("s3_bucket")
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"上传异常 {video_id} -> {key}: {exc}")
                        ok = False
                return video_id, "success" if ok else "failed", key if ok else None
            except Exception as exc:  # noqa: BLE001
                logger.error(f"处理 {video_id} 异常: {exc}")
                return video_id, "failed", None

        upload_results = await asyncio.gather(
            *(_upload_one(record) for record in pending)
        )

        success = 0
        failed = 0
        skipped = 0
        for video_id, status, key in upload_results:
            if status == "skipped":
                skipped += 1
                continue

            try:
                async with get_tiktok_db_session() as session:
                    repo = TiktokExploreItemRepository(session)
                    if status == "success" and key is not None:
                        persisted = await repo.mark_uploaded(video_id, key)
                    else:
                        persisted = await repo.mark_upload_failed(video_id)
                if not persisted:
                    raise RuntimeError("数据库记录不存在或未更新")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error(f"上传状态回写失败 {video_id}: {exc}")
                continue

            if status == "success" and key is not None:
                success += 1
                logger.info(f"已上传 {video_id} -> {key}")
            else:
                failed += 1
                logger.error(f"上传失败 {video_id}")

        return {"success": success, "failed": failed, "skipped": skipped}
    finally:
        if should_close_gateway:
            active_gateway.close()
