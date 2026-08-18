"""TikTok Explore 分类 ID→名称映射工具。

真相源是 ``Volume/explore_categories.json``，由
``poc/session/harvest_tiktok_session.py`` 在会话采集时从 explore 页面 SSR
自动生成。本模块只负责读取、查询与落盘，不持有任何硬编码映射。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CATEGORIES_PATH = PROJECT_ROOT / "Volume" / "explore_categories.json"


def merge_explore_categories(payload: object) -> dict[str, str]:
    """将 explore ``exploreCategoryList`` 字典（含 v0/v1/v2 分组）合并去重。

    每项形如 ``{"text": "...", "name": "...", "type": "120"}``，以 ``type`` 为
    分类 ID。同 ID 先到先得（v0 优先），避免不同版本显示名差异。

    v2 分组（``Web_explorePage_dynamicCategories_*``，ID 200+）被整体排除：
    其 categoryType 请求实际不返回内容，且与 v0/v1 的 pc_web 分类语义重复。
    """
    if not isinstance(payload, dict):
        return {}
    merged: dict[str, str] = {}
    for group, entries in payload.items():
        if group == "v2" or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            id_ = entry.get("type")
            name = entry.get("name")
            if not isinstance(id_, (str, int)) or not isinstance(name, str):
                continue
            if name.strip() and str(id_) not in merged:
                merged[str(id_)] = name.strip()
    return merged


def find_explore_categories(ssr: object) -> dict[str, object] | None:
    """在 SSR 对象树中查找 ``exploreCategoryList`` 字典。

    使用显式栈遍历，避免超大嵌套对象触发递归深度限制；同时深入
    列表节点，与浏览器端 JS 查找行为保持一致。
    """
    stack = [ssr]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            value = node.get("exploreCategoryList")
            if isinstance(value, dict):
                return value
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def load_category_names(path: Path | None = None) -> dict[str, str]:
    """读取分类映射；文件缺失或内容为空返回 {}。

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


def load_category_meta(path: Path | None = None) -> dict[str, object]:
    """读取分类映射附带的 ``_meta`` 元数据；缺失或损坏返回 {}。"""
    if path is None:
        path = DEFAULT_CATEGORIES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("_meta"), dict):
        return data["_meta"]
    return {}


def get_category_name(category_type: str, mapping: Mapping[str, str]) -> str | None:
    """返回分类 ID 对应的显示名；未知 ID 返回 None。"""
    return mapping.get(category_type)


def list_categories(mapping: Mapping[str, str]) -> list[tuple[str, str]]:
    """按 ID 数值升序返回 ``[(id, name)]``。"""
    items = [(str(id_), name) for id_, name in mapping.items()]
    return sorted(
        items, key=lambda item: int(item[0]) if item[0].isdigit() else item[0]
    )


def save_category_names(
    categories: Mapping[str, str],
    path: Path | None = None,
) -> Path:
    """将分类映射合并写入 ``path``，附带 ``_meta`` 元数据。

    先读取已有映射再合并，避免单次 SSR 缺分类导致已知 ID 收缩。
    """
    if path is None:
        path = DEFAULT_CATEGORIES_PATH
    merged = load_category_names(path)
    merged.update(categories)
    payload: dict[str, object] = {
        "_meta": {
            "source": "explore_ssr",
            "harvested_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    payload.update(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")
    return path
