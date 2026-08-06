"""batch 采集的 pydantic 配置 schema 与合并读取逻辑。

配置契约
--------
- 文件格式：严格 JSON，顶层为 ``{"defaults": {...}, "profiles": {name: {...}}}``。
- ``defaults`` 与每个 profile 的字段均为"部分覆盖"：缺失或 null 表示不覆盖，
  最终由内置默认值兜底。
- 合并优先级：CLI 显式参数 > profile > defaults > 内置默认。
- 未知字段仅告警不报错（前向兼容）。
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

UrlMode = Literal["play_url", "download_url", "all"]


class BatchConfigError(ValueError):
    """配置文件缺失、JSON 语法错误或内容不合法。"""


class BatchProfile(BaseModel):
    """可配置字段集合；None 表示不参与覆盖。"""

    model_config = ConfigDict(extra="allow")

    categories: str | None = None
    count: int | None = Field(default=None, ge=1)
    max_pages: int | None = Field(default=None, ge=1)
    delay: float | None = Field(default=None, ge=0)
    category_delay: float | None = Field(default=None, ge=0)
    concurrency: int | None = Field(default=None, ge=1)
    max_retry: int | None = Field(default=None, ge=1)
    chunk_kb: int | None = Field(default=None, ge=1)
    url_mode: UrlMode | None = None
    download: bool | None = None
    no_db: bool | None = None
    output_dir: str | None = None
    proxy: str | None = None


class BatchConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    defaults: BatchProfile = Field(default_factory=BatchProfile)
    profiles: dict[str, BatchProfile] = Field(default_factory=dict)


def _warn_unknown(where: str, fields: dict[str, Any]) -> None:
    if fields:
        warnings.warn(
            f"batch 配置[{where}]包含未知字段，已忽略: {', '.join(sorted(fields))}",
            stacklevel=3,
        )


def load_batch_config(
    config_path: Path | None,
    profile: str | None,
) -> dict[str, Any]:
    """读取配置并合并 defaults + profile，返回非 None 字段。

    ``config_path`` 为 None 时返回空 dict（纯 CLI 模式）。

    Raises:
        BatchConfigError: 文件缺失、JSON 语法错误、顶层非对象、
            未知档位或字段校验失败。
    """
    if config_path is None:
        if profile is not None:
            raise BatchConfigError("--profile 需要配合 --config 指定配置文件")
        return {}
    if not config_path.is_file():
        raise BatchConfigError(f"配置文件不存在: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BatchConfigError(
            f"配置文件 JSON 语法错误: {config_path}（第 {exc.lineno} 行）"
        ) from exc
    if not isinstance(raw, dict):
        raise BatchConfigError(f"配置文件顶层必须是对象: {config_path}")
    try:
        config = BatchConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()
        )
        raise BatchConfigError(f"配置文件校验失败: {config_path}: {details}") from exc

    _warn_unknown("顶层", config.model_extra or {})
    _warn_unknown("defaults", config.defaults.model_extra or {})
    for name, item in config.profiles.items():
        _warn_unknown(f"profiles.{name}", item.model_extra or {})

    if profile is not None and profile not in config.profiles:
        available = ", ".join(sorted(config.profiles)) or "(无)"
        raise BatchConfigError(f"未知档位 '{profile}'，可用档位: {available}")

    merged: dict[str, Any] = config.defaults.model_dump(exclude_none=True)
    if profile is not None:
        merged.update(config.profiles[profile].model_dump(exclude_none=True))
    return merged
