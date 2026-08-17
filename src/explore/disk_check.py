"""磁盘空间检测工具。

供下载/上传类脚本在写入前检测剩余空间，避免写满磁盘。
``check_disk_usage`` 的阈值语义：``used_percent_threshold``（使用率百分比）
与 ``min_free_gb``（剩余空间 GB）任一触发即 ``need_cleanup=True``；
取值 <= 0 或 None 表示关闭该项检测。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import NamedTuple


class DiskUsageInfo(NamedTuple):
    """磁盘使用情况快照，单位为字节。

    total: 总容量
    used: 已用空间
    free: 剩余空间
    """

    total: int
    used: int
    free: int


class DiskCheckResult(NamedTuple):
    """按阈值检测磁盘后的结果。"""

    need_cleanup: bool
    usage: DiskUsageInfo
    used_percent: float
    free_gb: float


def _nearest_existing_path(path: Path) -> Path | None:
    """返回 path 自身或最近的已存在祖先目录；连根都不存在时返回 None。"""
    current = path
    while not current.exists():
        if current.parent == current:
            return None
        current = current.parent
    return current


def get_disk_usage(path: Path | str) -> DiskUsageInfo:
    """获取 path 所在分区的磁盘使用情况。

    path 尚不存在时回退到最近的已存在祖先目录（批量输出目录
    常在首次写入前不存在，但分区信息与祖先一致）。

    Raises:
        FileNotFoundError: 连祖先目录都不存在时抛出。
    """
    existing = _nearest_existing_path(Path(path))
    if existing is None:
        raise FileNotFoundError(f"Path does not exist: {path}")
    total, used, free = shutil.disk_usage(existing)
    return DiskUsageInfo(total=total, used=used, free=free)


def calc_used_percent(usage: DiskUsageInfo) -> float:
    """根据 DiskUsageInfo 计算磁盘已用百分比。"""
    if usage.total <= 0:
        return 0.0
    return usage.used / usage.total * 100.0


def calc_free_gb(usage: DiskUsageInfo) -> float:
    """根据 DiskUsageInfo 计算剩余空间（GB）。"""
    return usage.free / 1024.0**3


def check_disk_usage(
    path: Path | str,
    *,
    used_percent_threshold: float | None = None,
    min_free_gb: float | None = None,
) -> DiskCheckResult:
    """按阈值检测磁盘是否需要停止写入/触发清理。

    Args:
        path: 用于检测磁盘的任意路径（位于目标分区即可）。
        used_percent_threshold: 使用率 >= 该值时触发，例如 90.0。
        min_free_gb: 剩余空间 <= 该值（GB）时触发，例如 5.0。

    Returns:
        need_cleanup: 任一阈值触发即为 True。
        usage / used_percent / free_gb: 检测时刻的快照。
    """
    usage = get_disk_usage(path)
    used_percent = calc_used_percent(usage)
    free_gb = calc_free_gb(usage)

    need_cleanup = False
    if used_percent_threshold is not None and used_percent_threshold > 0:
        need_cleanup = used_percent >= used_percent_threshold
    if min_free_gb is not None and min_free_gb > 0:
        need_cleanup = need_cleanup or free_gb <= min_free_gb
    return DiskCheckResult(
        need_cleanup=need_cleanup,
        usage=usage,
        used_percent=used_percent,
        free_gb=free_gb,
    )
