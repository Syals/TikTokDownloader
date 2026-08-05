"""AWS S3 / S3 兼容上传器（boto3 + asyncio.to_thread）。

凭据走 boto3 标准环境变量（``AWS_ACCESS_KEY_ID``、``AWS_SECRET_ACCESS_KEY``、
``AWS_REGION``），bucket 默认读取 ``TIKTOK_S3_BUCKET`` / ``S3_BUCKET`` /
``AWS_S3_BUCKET``。

兼容云存储支持：通过构造参数 ``endpoint_url`` / ``region_name`` 或环境变量
``S3_ENDPOINT_URL`` / ``AWS_ENDPOINT_URL`` / ``AWS_REGION`` / ``AWS_DEFAULT_REGION``
/ ``S3_REGION`` 指定厂家 endpoint。

定时任务短生命周期下，不用长期 pool，直接同步 boto3 upload_file 即可；
``asyncio.to_thread`` 让调用侧保持 async 语义。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger


def _resolve_bucket(bucket: str | None) -> str:
    resolved = (
        bucket
        or os.environ.get("TIKTOK_S3_BUCKET")
        or os.environ.get("S3_BUCKET")
        or os.environ.get("AWS_S3_BUCKET")
    )
    if not resolved:
        msg = "S3 bucket not configured; set TIKTOK_S3_BUCKET/S3_BUCKET/AWS_S3_BUCKET"
        raise RuntimeError(msg)
    return resolved


def _resolve_endpoint() -> str | None:
    """优先项目专属 S3_ENDPOINT_URL，再兜底 AWS_ENDPOINT_URL（boto3 通用）。"""
    return os.environ.get("S3_ENDPOINT_URL") or os.environ.get("AWS_ENDPOINT_URL")


def _resolve_region() -> str | None:
    """按 AWS_REGION / AWS_DEFAULT_REGION / S3_REGION 顺序解析 region。"""
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("S3_REGION")
    )


class S3Uploader:
    """最小 S3 上传网关，兼容 ``upload_pending_explore`` 的 ``_UploadGateway`` Protocol。"""

    def __init__(
        self,
        bucket: str | None = None,
        client: Any | None = None,
        *,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ) -> None:
        self.bucket = _resolve_bucket(bucket)
        if client is not None:
            self._client = client
            return

        endpoint = endpoint_url or _resolve_endpoint()
        region = region_name or _resolve_region()
        kwargs: dict[str, Any] = {}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if region:
            kwargs["region_name"] = region
        self._client = boto3.client("s3", **kwargs)

    async def upload_file(self, local_file_path: str, remote_s3_path: str) -> bool:
        """上传本地文件到 S3。成功返回 True，失败记录日志并返回 False。"""
        try:
            await asyncio.to_thread(
                self._client.upload_file,
                local_file_path,
                self.bucket,
                remote_s3_path,
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            logger.error(
                f"S3 upload failed: {local_file_path} -> {self.bucket}/"
                f"{remote_s3_path}: {exc}"
            )
            return False
        return True

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
