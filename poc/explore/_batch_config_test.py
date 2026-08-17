"""回归测试：_batch_config 的加载、合并与错误处理。"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._batch_config import (  # noqa: E402
    BatchConfigError,
    load_batch_config,
)


def write_config(root: Path, payload: object) -> Path:
    path = root / "batch_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_none_config_returns_empty() -> None:
    assert load_batch_config(None, None) == {}


def test_load_profile_without_config_raises() -> None:
    with pytest.raises(BatchConfigError, match="--config"):
        load_batch_config(None, "night")


def test_load_missing_file_raises(project_tmp: Path) -> None:
    with pytest.raises(BatchConfigError, match="配置文件不存在"):
        load_batch_config(project_tmp / "nope.json", None)


def test_load_bad_json_raises(project_tmp: Path) -> None:
    path = project_tmp / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(BatchConfigError, match="JSON 语法错误"):
        load_batch_config(path, None)


def test_load_non_object_raises(project_tmp: Path) -> None:
    path = write_config(project_tmp, [1, 2, 3])
    with pytest.raises(BatchConfigError, match="顶层必须是对象"):
        load_batch_config(path, None)


def test_load_invalid_field_raises(project_tmp: Path) -> None:
    path = write_config(project_tmp, {"defaults": {"count": 0}})
    with pytest.raises(BatchConfigError, match="校验失败"):
        load_batch_config(path, None)


def test_load_bad_url_mode_raises(project_tmp: Path) -> None:
    path = write_config(project_tmp, {"defaults": {"url_mode": "bogus"}})
    with pytest.raises(BatchConfigError, match="url_mode"):
        load_batch_config(path, None)


def test_load_unknown_profile_raises(project_tmp: Path) -> None:
    path = write_config(project_tmp, {"profiles": {"night": {"count": 5}}})
    with pytest.raises(BatchConfigError, match="未知档位 'lunch'") as excinfo:
        load_batch_config(path, "lunch")
    assert "night" in str(excinfo.value)


def test_load_unknown_field_warns(project_tmp: Path) -> None:
    path = write_config(project_tmp, {"defaults": {"dload": True}})
    with pytest.warns(UserWarning, match="未知字段"):
        load_batch_config(path, None)


def test_load_merges_defaults_and_profile(project_tmp: Path) -> None:
    path = write_config(
        project_tmp,
        {
            "defaults": {"download": True, "concurrency": 3},
            "profiles": {"night": {"categories": "104,112", "count": 10}},
        },
    )
    assert load_batch_config(path, "night") == {
        "download": True,
        "concurrency": 3,
        "categories": "104,112",
        "count": 10,
    }


def test_load_no_profile_uses_defaults_only(project_tmp: Path) -> None:
    path = write_config(project_tmp, {"defaults": {"download": True}})
    assert load_batch_config(path, None) == {"download": True}


def test_load_empty_config_ok(project_tmp: Path) -> None:
    path = write_config(project_tmp, {})
    assert load_batch_config(path, None) == {}


def test_load_disk_threshold_fields(project_tmp: Path) -> None:
    path = write_config(
        project_tmp,
        {
            "defaults": {"disk_used_percent": 85.0},
            "profiles": {"night": {"min_free_gb": 20.0}},
        },
    )
    assert load_batch_config(path, "night") == {
        "disk_used_percent": 85.0,
        "min_free_gb": 20.0,
    }


def test_load_negative_disk_threshold_rejects(project_tmp: Path) -> None:
    path = write_config(project_tmp, {"defaults": {"min_free_gb": -1.0}})
    with pytest.raises(BatchConfigError, match="校验失败"):
        load_batch_config(path, None)
