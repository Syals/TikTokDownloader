"""PoC control test: hit the known-good account endpoint to isolate
whether the XBogus + XGnarly signing chain still works against TikTok.

If this returns JSON  -> signing is fine, the search endpoint failure
                        is about endpoint URL / params / extra headers.
If this returns HTML   -> signing chain is broken globally; need a
                        different strategy (browser, unofficial API).

Uses secUid resolved from the profile page of @elsebasmadridista
(account previously downloaded successfully by the user).

Usage: uv run python poc/tiktok_control_poc.py
"""

import asyncio
import json
import re
import sys
import traceback
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.custom import DATA_HEADERS_TIKTOK  # noqa: E402
from src.encrypt import XBogus, XGnarly  # noqa: E402

SETTINGS_PATH = PROJECT_ROOT / "Volume" / "settings.json"
POC_DIR = Path(__file__).resolve().parent
SEC_UID_PATTERN = re.compile(r'"secUid":"([A-Za-z0-9_-]+)"')


def cookie_dict_to_str(cookie: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie.items())


def build_account_params(ms_token: str, device_id: str, sec_uid: str) -> dict:
    """Mirror AccountTikTok.generate_post_params + APITikTok.params."""
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
    print(f"[RESOLVE] Fetching {url}")
    try:
        resp = await client.get(url, headers=headers)
        print(f"          Status: {resp.status_code}, Length: {len(resp.content)}")
        if m := SEC_UID_PATTERN.search(resp.text):
            print(f"          secUid found: {m.group(1)[:30]}...")
            return m.group(1)
        print(f"          secUid NOT FOUND in HTML")
        # Save HTML for inspection
        (POC_DIR / "control_profile.html").write_text(resp.text, encoding="utf-8")
        print(f"          HTML saved: poc/control_profile.html")
        return None
    except httpx.RequestError as e:
        print(f"          [NETWORK ERROR] {type(e).__name__}: {e}")
        return None


async def main() -> int:
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
            print("[BLOCK] Cannot resolve secUid; aborting control test")
            return 1

        print(f"\n{'=' * 72}\n[CONTROL] /api/post/item_list/\n{'=' * 72}")
        params = build_account_params(ms_token, device_id, sec_uid)
        query = sign_query(params, user_agent)
        full_url = f"https://www.tiktok.com/api/post/item_list/?{query}"
        try:
            resp = await client.get(full_url, headers=headers)
            print(f"  Status:        {resp.status_code}")
            print(f"  Content-Type:  {resp.headers.get('content-type', '?')}")
            print(f"  Content-Length: {len(resp.content)} bytes")
            try:
                data = resp.json()
                print(f"  Top-level keys: {list(data.keys())}")
                if "itemList" in data:
                    items = data["itemList"]
                    print(f"  PASS: itemList contains {len(items)} items")
                    if items:
                        first = items[0]
                        print(f"    First item keys: {list(first.keys())[:20]}")
                        if "stats" in first:
                            print(f"    First item stats: {first['stats']}")
                else:
                    print(f"  FAIL: no itemList in response")
                (POC_DIR / "control_post_item_list.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  Saved: poc/control_post_item_list.json")
                return 0 if "itemList" in data else 1
            except json.JSONDecodeError:
                preview = resp.text[:500].replace("\n", " ")
                print(f"  [NOT JSON] Body preview: {preview}")
                (POC_DIR / "control_post_item_list.html").write_text(
                    resp.text, encoding="utf-8"
                )
                print(f"  HTML saved: poc/control_post_item_list.html")
                return 1
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            traceback.print_exc()
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
