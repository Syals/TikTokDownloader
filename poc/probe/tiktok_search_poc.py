"""PoC：验证 TikTok 搜索/发现端点是否可通过项目既有的签名基础设施
（XBogus + XGnarly）访问。

故意绕过 APITikTok 基类，以便完全掌控错误处理与原始响应检视。
签名的应用顺序与 src/interface/template.py:APITikTok.deal_url_params
完全一致。

用法（在仓库根目录执行）：
    uv run python poc/probe/tiktok_search_poc.py
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.custom import DATA_HEADERS_TIKTOK  # noqa: E402
from src.encrypt import XBogus, XGnarly  # noqa: E402

SETTINGS_PATH = PROJECT_ROOT / "Volume" / "settings.json"
OUTPUT_DIR = PROJECT_ROOT / ".output" / "explore" / "samples"


def cookie_dict_to_str(cookie: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie.items())


def build_tiktok_params(ms_token: str, device_id: str, search: dict) -> dict:
    """镜像 APITikTok.params（src/interface/template.py:503）+ 搜索覆盖项。"""
    base = {
        "aid": "1988",
        "app_language": "es",
        "app_name": "tiktok_web",
        "browser_language": "es-ES",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": (
            "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "data_collection_enabled": "true",
        "device_id": device_id,
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "search",
        "history_len": "4",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "language": "es",
        "priority_region": "ES",
        "referer": "",
        "region": "ES",
        "screen_height": "864",
        "screen_width": "1536",
        "tz_name": "Europe/Madrid",
        "user_is_login": "true",
        "webcast_language": "es",
        "msToken": ms_token,
    }
    base.update({k: str(v) for k, v in search.items()})
    return base


def sign_query(params: dict, user_agent: str) -> str:
    """按 APITikTok.deal_url_params 的顺序应用 TikTok 签名。"""
    query = urlencode(params, safe="=", quote_via=quote)
    xb = XBogus().get_x_bogus(query, None, "GET", user_agent=user_agent)
    xg = XGnarly().generate(query, "", "GET", user_agent=user_agent)
    return f"{query}&X-Bogus={xb}&X-Gnarly={xg}"


async def probe(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    params: dict,
    headers: dict,
    user_agent: str,
) -> dict | None:
    print(f"\n{'=' * 72}")
    print(f"[POC] {name}")
    print(f"      URL: {url}")
    print(f"{'=' * 72}")
    try:
        query = sign_query(params, user_agent)
        full_url = f"{url}?{query}"
        resp = await client.get(full_url, headers=headers)
        print(f"      状态码:        {resp.status_code}")
        print(f"      Content-Type:  {resp.headers.get('content-type', '?')}")
        print(f"      Content-Length: {len(resp.content)} 字节")

        if resp.status_code != 200:
            preview = resp.text[:400].replace("\n", " ")
            print(f"      [非 200] 响应预览: {preview}")
            return None

        try:
            data = resp.json()
        except json.JSONDecodeError:
            preview = resp.text[:400].replace("\n", " ")
            print(f"      [非 JSON] 响应预览: {preview}")
            return None

        if not isinstance(data, dict):
            print(f"      响应根类型: {type(data).__name__}")
            return data

        print(f"      顶层键: {list(data.keys())}")
        sample = OUTPUT_DIR / f"sample_{name.replace('/', '_')}.json"
        sample.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"      原始响应已保存: {sample.name}")

        for key in (
            "data",
            "item_list",
            "aweme_list",
            "itemList",
            "explore_item_list",
            "list",
            "card_list",
        ):
            items = data.get(key)
            if isinstance(items, list) and items:
                print(f"      条目容器 '{key}': {len(items)} 个条目")
                first = items[0]
                if isinstance(first, dict):
                    print(f"        首条目键: {list(first.keys())[:25]}")
                    for stat in ("digg_count", "play_count", "share_count"):
                        v = (
                            first.get("stats", {}).get(stat)
                            if isinstance(first.get("stats"), dict)
                            else first.get(stat)
                        )
                        if v is not None:
                            print(f"        stats.{stat} = {v}")
                return data
        print("      未找到已知的条目容器")
        for k, v in data.items():
            if isinstance(v, dict):
                print(f"        data['{k}'] 键: {list(v.keys())[:10]}")
        return data
    except httpx.RequestError as e:
        print(f"      [网络错误] {type(e).__name__}: {e}")
        return None
    except Exception as e:
        print(f"      [错误] {type(e).__name__}: {e}")
        traceback.print_exc()
        return None


async def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        print(f"[致命] 未在 {SETTINGS_PATH} 找到 settings.json")
        return 2
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    cookie_dict = settings.get("cookie_tiktok")
    if not isinstance(cookie_dict, dict) or "sessionid" not in cookie_dict:
        print("[致命] cookie_tiktok 缺失或未登录（无 'sessionid'）")
        return 2
    cookie_str = cookie_dict_to_str(cookie_dict)
    ms_token = cookie_dict.get("msToken", "")
    device_id = settings.get("browser_info_tiktok", {}).get("device_id", "")
    user_agent = settings["browser_info_tiktok"]["User-Agent"]
    print(
        f"[初始化] sessionid: yes | msToken: {'yes' if ms_token else 'NO'} "
        f"| device_id: {device_id or 'EMPTY'} | UA 长度: {len(user_agent)}"
    )

    headers = DATA_HEADERS_TIKTOK | {
        "Cookie": cookie_str,
        "Referer": "https://www.tiktok.com/explore",
    }

    endpoints = [
        ("search/general/full", "https://www.tiktok.com/api/search/general/full/"),
        ("search/item/full", "https://www.tiktok.com/api/search/item/full/"),
        ("explore/item/list", "https://www.tiktok.com/api/explore/item/list/"),
    ]
    base_search = {
        "keyword": "funny cats",
        "offset": 0,
        "limit": 10,
        "search_id": "",
    }

    verdicts: dict[str, bool] = {}
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        verify=False,
        headers={"User-Agent": user_agent},
    ) as client:
        for name, url in endpoints:
            params = build_tiktok_params(ms_token, device_id, base_search)
            data = await probe(client, name, url, params, headers, user_agent)
            has_items = bool(
                data
                and isinstance(data, dict)
                and any(
                    isinstance(data.get(k), list) and data[k]
                    for k in (
                        "data",
                        "item_list",
                        "aweme_list",
                        "itemList",
                        "explore_item_list",
                        "list",
                        "card_list",
                    )
                )
            )
            verdicts[name] = has_items

    print(f"\n{'=' * 72}\n[汇总]\n{'=' * 72}")
    for name, ok in verdicts.items():
        print(f"  {name:30s} : {'通过' if ok else '失败'}")
    print(
        f"\n结论: {'至少一个端点可用' if any(verdicts.values()) else '所有端点均失败'}"
    )
    return 0 if any(verdicts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
