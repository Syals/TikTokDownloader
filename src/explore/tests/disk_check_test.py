"""回归测试：src.explore.disk_check 的磁盘检测工具。"""

import shutil
import tempfile
from pathlib import Path

import pytest

from src.explore.disk_check import (
    DiskUsageInfo,
    calc_free_gb,
    calc_used_percent,
    check_disk_usage,
    get_disk_usage,
)


@pytest.fixture
def disk_path() -> Path:
    """项目内临时目录，避开系统 %TEMP% 可能无写权限的问题。"""
    with tempfile.TemporaryDirectory(dir=".") as tmp:
        yield Path(tmp)


def test_get_disk_usage_matches_shutil(disk_path: Path) -> None:
    total, used, free = shutil.disk_usage(disk_path)
    assert get_disk_usage(disk_path) == DiskUsageInfo(total=total, used=used, free=free)


def test_get_disk_usage_missing_path_falls_back_to_ancestor(disk_path: Path) -> None:
    missing = disk_path / "explore" / "batch" / "downloads"
    total, used, free = shutil.disk_usage(disk_path)
    assert get_disk_usage(missing) == DiskUsageInfo(total=total, used=used, free=free)


def test_get_disk_usage_without_existing_ancestor_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.explore.disk_check._nearest_existing_path", lambda _: None)
    with pytest.raises(FileNotFoundError):
        get_disk_usage(".output/definitely/not/here")


def test_calc_used_percent_and_free_gb() -> None:
    usage = DiskUsageInfo(total=200, used=50, free=150)
    assert calc_used_percent(usage) == 25.0
    assert calc_free_gb(usage) == pytest.approx(150 / 1024.0**3)


def test_calc_used_percent_zero_total() -> None:
    assert calc_used_percent(DiskUsageInfo(total=0, used=0, free=0)) == 0.0


def test_check_disk_usage_used_percent_threshold(disk_path: Path) -> None:
    used = calc_used_percent(get_disk_usage(disk_path))
    below = check_disk_usage(disk_path, used_percent_threshold=used + 1.0)
    assert below.need_cleanup is False
    assert below.usage.total > 0

    above = check_disk_usage(disk_path, used_percent_threshold=max(used - 1.0, 0.001))
    assert above.need_cleanup is True


def test_check_disk_usage_min_free_gb_threshold(disk_path: Path) -> None:
    free = calc_free_gb(get_disk_usage(disk_path))
    too_little = check_disk_usage(disk_path, min_free_gb=free + 1.0)
    assert too_little.need_cleanup is True

    enough = check_disk_usage(disk_path, min_free_gb=max(free - 1.0, 0.0))
    assert enough.need_cleanup is False


def test_check_disk_usage_zero_or_absent_thresholds_disabled(disk_path: Path) -> None:
    """阈值 <= 0 或不传表示关闭该项检测，不触发清理。"""
    for kwargs in (
        {},
        {"used_percent_threshold": 0.0},
        {"min_free_gb": 0.0},
        {"used_percent_threshold": 0.0, "min_free_gb": 0.0},
    ):
        assert check_disk_usage(disk_path, **kwargs).need_cleanup is False  # type: ignore[arg-type]
