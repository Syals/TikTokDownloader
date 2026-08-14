"""TikTok Explore 分类 ID→英文路径段映射。

真相源是 ``Volume/explore_categories.json``（形如 ``{"104": "Comedy"}``），
由 poc 会话采集脚本从 explore 页面 SSR 自动生成。本模块在 src 侧读取
该文件并把显示名 slug 化，供上传路径构造使用。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CATEGORIES_PATH = PROJECT_ROOT / "Volume" / "explore_categories.json"


def load_category_names(path: Path | None = None) -> dict[str, str]:
    """读取分类映射；文件缺失或内容非法返回 {}。

    仅保留纯数字 ID 键，忽略 ``_meta`` 等元数据键，使元数据与映射可共存。
    """
    if path is None:
        path = DEFAULT_CATEGORIES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    names: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.isdigit() and isinstance(value, str):
            if value.strip():
                names[key] = value.strip()
    return names


def slugify_category_name(name: str) -> str:
    """把分类显示名转成安全路径段：``Singing & Dancing`` → ``singing_dancing``。"""
    return re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_")


def build_category_slug_map(path: Path | None = None) -> dict[str, str]:
    """返回 ``{分类ID: "ID-英文slug"}``，如 ``{"104": "104-comedy"}``。

    保留 ID 前缀以区分同名分类（如 104/206 均为 Comedy）；slug 化后为空
    的条目被跳过，上传时回退数字目录。
    """
    return {
        id_: f"{id_}-{slug}"
        for id_, name in load_category_names(path).items()
        if (slug := slugify_category_name(name))
    }
