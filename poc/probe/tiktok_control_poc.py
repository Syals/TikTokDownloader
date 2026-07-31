"""对照测试 PoC：请求已知的可用账号端点，用于隔离判断
XBogus + XGnarly 签名链路当前是否仍能通过 TikTok 校验。

若返回 JSON  -> 签名正常，搜索端点失败的原因在于
                端点 URL / 参数 / 额外请求头。
若返回 HTML  -> 签名链路整体失效；需要改用
                其他策略（浏览器、非官方 API）。

使用从 @elsebasmadridista 资料页解析出的 secUid
（该账号此前已被用户成功下载过）。

用法：uv run python poc/probe/tiktok_control_poc.py
"""

import asyncio
import json
import re
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
OUTPUT_DIR = PROJECT_ROOT / ".output" / "probe"
SEC_UID_PATTERN = re.compile(r'"secUid":"([A-Za-z0-9_-]+)"')


def cookie_dict_to_str(cookie: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie.items())


def build_account_params(ms_token: str, device_id: str, sec_uid: str) -> dict:
    """镜像 AccountTikTok.generate_post_params + APITikTok.params。"""
    return {
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
        "coverFormat": "2",
        "data_collection_enabled": "true",
        "device_id": device_id,
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "user",
        "history_len": "4",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "language": "es",
        "needPinnedItemIds": "true",
        "post_item_list_request_type": "0",
        "priority_region": "ES",
        "referer": "",
        "region": "ES",
        "screen_height": "864",
        "screen_width": "1536",
        "secUid": sec_uid,
        "count": "16",
        "cursor": "0",
        "video_encoding": "mp4",
        "tz_name": "Europe/Madrid",
        "user_is_login": "true",
        "webcast_language": "es",
        "msToken": ms_token,
    }


def sign_query(params: dict, user_agent: str) -> str:
    query = urlencode(params, safe="=", quote_via=quote)
    xb = XBogus().get_x_bogus(query, None, "GET", user_agent=user_agent)
    xg = XGnarly().generate(query, "", "GET", user_agent=user_agent)
    return f"{query}&X-Bogus={xb}&X-Gnarly={xg}"


async def resolve_sec_uid(
    client: httpx.AsyncClient, username: str, headers: dict
) -> str | None:
    url = f"https://www.tiktok.com/@{username}"
    print(f"[解析] 正在抓取 {url}")
    try:
        resp = await client.get(url, headers=headers)
        print(f"       状态码: {resp.status_code}, 长度: {len(resp.content)}")
        if m := SEC_UID_PATTERN.search(resp.text):
            print(f"       已找到 secUid: {m.group(1)[:30]}...")
            return m.group(1)
        print("       HTML 中未找到 secUid")
        # 保存 HTML 以便检查
        (OUTPUT_DIR / "control_profile.html").write_text(resp.text, encoding="utf-8")
        print(f"       HTML 已保存: {OUTPUT_DIR / 'control_profile.html'}")
        return None
    except httpx.RequestError as e:
        print(f"       [网络错误] {type(e).__name__}: {e}")
        return None


async def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    cookie_dict = settings["cookie_tiktok"]
    cookie_str = cookie_dict_to_str(cookie_dict)
    ms_token = cookie_dict.get("msToken", "")
    device_id = settings.get("browser_info_tiktok", {}).get("device_id", "")
    user_agent = settings["browser_info_tiktok"]["User-Agent"]

    headers = DATA_HEADERS_TIKTOK | {
        "Cookie": cookie_str,
        "Referer": "https://www.tiktok.com/@elsebasmadridista",
    }

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        verify=False,
        headers={"User-Agent": user_agent},
    ) as client:
        sec_uid = await resolve_sec_uid(client, "elsebasmadridista", headers)
        if not sec_uid:
            print("[阻断] 无法解析 secUid；终止对照测试")
            return 1

        print(f"\n{'=' * 72}\n[对照] /api/post/item_list/\n{'=' * 72}")
        params = build_account_params(ms_token, device_id, sec_uid)
        query = sign_query(params, user_agent)
        full_url = f"https://www.tiktok.com/api/post/item_list/?{query}"
        try:
            resp = await client.get(full_url, headers=headers)
            print(f"  状态码:        {resp.status_code}")
            print(f"  Content-Type:  {resp.headers.get('content-type', '?')}")
            print(f"  Content-Length: {len(resp.content)} 字节")
            try:
                data = resp.json()
                print(f"  顶层键: {list(data.keys())}")
                if "itemList" in data:
                    items = data["itemList"]
                    print(f"  通过: itemList 包含 {len(items)} 个条目")
                    if items:
                        first = items[0]
                        print(f"    首条目键: {list(first.keys())[:20]}")
                        if "stats" in first:
                            print(f"    首条目统计: {first['stats']}")
                else:
                    print("  失败: 响应中没有 itemList")
                (OUTPUT_DIR / "control_post_item_list.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  已保存: {OUTPUT_DIR / 'control_post_item_list.json'}")
                return 0 if "itemList" in data else 1
            except json.JSONDecodeError:
                preview = resp.text[:500].replace("\n", " ")
                print(f"  [非 JSON] 响应预览: {preview}")
                (OUTPUT_DIR / "control_post_item_list.html").write_text(
                    resp.text, encoding="utf-8"
                )
                print(f"  HTML 已保存: {OUTPUT_DIR / 'control_post_item_list.html'}")
                return 1
        except Exception as e:
            print(f"  [错误] {type(e).__name__}: {e}")
            traceback.print_exc()
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
