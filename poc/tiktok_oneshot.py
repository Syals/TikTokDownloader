"""One-shot PoC: test TikTok search endpoint AND known-good account endpoint
through the system proxy. Single run, clear verdict.

Strategy:
  1. Read cookie + msToken + device_id from Volume/settings.json
  2. Resolve elsebasmadridista secUid (logged-in profile fetch via proxy)
  3. Hit /api/post/item_list/  (CONTROL — known to work today per logs)
  4. Hit /api/search/general/full/  (TARGET — for new search feature)
Both go through XBogus + XGnarly signing, same as project flow.

Verdict matrix:
  Control JSON + Search JSON  -> GO: integrate search endpoint
  Control JSON + Search HTML  -> signing works, search endpoint rejected;
                                  try alternate search paths or browser fallback
  Control HTML + Search HTML  -> proxy/signing broken globally; STOP
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
PROXY = "http://127.0.0.1:64142"
SEC_UID_PATTERN = re.compile(r'"secUid":"([A-Za-z0-9_-]+)"')


def cookie_dict_to_str(cookie: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie.items())


def build_params(ms_token: str, device_id: str, extra: dict) -> dict:
    """APITikTok.params base + endpoint-specific overrides."""
    base = {
        "aid": "1988",
        "app_language": "zh-Hans",
        "app_name": "tiktok_web",
        "browser_language": "zh-CN",
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
        "enable_cache": "true",
        "focus_state": "true",
        "from_page": "user",
        "history_len": "4",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "language": "zh-Hans",
        "os": "windows",
        "priority_region": "US",
        "referer": "",
        "region": "US",
        "screen_height": "864",
        "screen_width": "1536",
        "tz_name": "Asia/Shanghai",
        "user_is_login": "true",
        "webcast_language": "zh-Hans",
        "msToken": ms_token,
    }
    base.update({k: str(v) for k, v in extra.items()})
    return base


def sign_query(params: dict, user_agent: str) -> str:
    query = urlencode(params, safe="=", quote_via=quote)
    xb = XBogus().get_x_bogus(query, None, "GET", user_agent=user_agent)
    xg = XGnarly().generate(query, "", "GET", user_agent=user_agent)
    return f"{query}&X-Bogus={xb}&X-Gnarly={xg}"


async def fetch_json(
    client: httpx.AsyncClient, name: str, url: str, params: dict,
    headers: dict, user_agent: str, referer: str,
) -> dict | None:
    h = dict(headers)
    h["Referer"] = referer
    query = sign_query(params, user_agent)
    full = f"{url}?{query}"
    print(f"\n--- {name} ---")
    print(f"  URL: {url}")
    try:
        resp = await client.get(full, headers=h)
        ct = resp.headers.get("content-type", "?")
        print(f"  Status: {resp.status_code}  Content-Type: {ct}  Bytes: {len(resp.content)}")
        if "application/json" not in ct:
            print(f"  [HTML/shell] preview: {resp.text[:200].replace(chr(10), ' ')}")
            return None
        data = resp.json()
        top = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        print(f"  JSON top-level keys: {top}")
        return data
    except httpx.RequestError as e:
        print(f"  [NETWORK ERROR] {type(e).__name__}: {e}")
        return None
    except Exception as e:
        print(f"  [ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        return None


async def main() -> int:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    cookie_dict = settings["cookie_tiktok"]
    cookie_str = cookie_dict_to_str(cookie_dict)
    ms_token = cookie_dict.get("msToken", "")
    device_id = settings["browser_info_tiktok"]["device_id"]
    user_agent = settings["browser_info_tiktok"]["User-Agent"]
    print(f"[SETUP] proxy={PROXY} | msToken={'yes' if ms_token else 'NO'} | device_id={device_id}")

    headers = DATA_HEADERS_TIKTOK | {"Cookie": cookie_str}

    async with httpx.AsyncClient(
        timeout=30,
        proxy=PROXY,
        follow_redirects=True,
        verify=False,
        headers={"User-Agent": user_agent},
    ) as client:
        # 1. Resolve secUid via logged-in profile fetch (proves proxy + cookie work)
        print("\n=== STEP 1: resolve secUid for @elsebasmadridista ===")
        try:
            r = await client.get(
                "https://www.tiktok.com/@elsebasmadridista",
                headers=headers | {"Referer": "https://www.tiktok.com/"},
            )
            print(f"  Profile fetch: {r.status_code} | {r.headers.get('content-type', '?')} | {len(r.content)} bytes")
            m = SEC_UID_PATTERN.search(r.text)
            if not m:
                print(f"  [FAIL] secUid not found in profile HTML — proxy/cookie not working")
                return 1
            sec_uid = m.group(1)
            print(f"  secUid = {sec_uid}")
        except httpx.RequestError as e:
            print(f"  [NETWORK ERROR] {type(e).__name__}: {e}")
            return 1

        # 2. CONTROL: /api/post/item_list/
        control_params = build_params(ms_token, device_id, {
            "secUid": sec_uid, "count": "5", "cursor": "0",
            "coverFormat": "2", "post_item_list_request_type": "0",
            "needPinnedItemIds": "true", "video_encoding": "mp4",
        })
        control = await fetch_json(
            client, "CONTROL /api/post/item_list/",
            "https://www.tiktok.com/api/post/item_list/",
            control_params, headers, user_agent,
            f"https://www.tiktok.com/@elsebasmadridista",
        )
        control_ok = bool(control and isinstance(control.get("itemList"), list)
                          and control["itemList"])
        if control_ok:
            print(f"  CONTROL PASS: itemList has {len(control['itemList'])} items")
        else:
            print(f"  CONTROL FAIL")

        # 3. TARGET: /api/search/general/full/
        search_params = build_params(ms_token, device_id, {
            "keyword": "funny cats", "offset": "0", "limit": "10",
            "search_id": "", "from_page": "search",
        })
        search = await fetch_json(
            client, "TARGET /api/search/general/full/",
            "https://www.tiktok.com/api/search/general/full/",
            search_params, headers, user_agent,
            "https://www.tiktok.com/search?q=funny%20cats",
        )
        search_ok = False
        if search and isinstance(search, dict):
            for k in ("data", "item_list", "itemList", "list"):
                v = search.get(k)
                if isinstance(v, list) and v:
                    print(f"  TARGET container '{k}': {len(v)} items")
                    search_ok = True
                    break

    # Verdict
    print(f"\n{'=' * 60}\nVERDICT\n{'=' * 60}")
    print(f"  Control (account post list): {'PASS' if control_ok else 'FAIL'}")
    print(f"  Target (search general):     {'PASS' if search_ok else 'FAIL'}")
    if search_ok:
        print("\n  >>> GO: search endpoint works, proceed to integration")
    elif control_ok:
        print("\n  >>> PARTIAL: signing works but search endpoint rejected")
        print("  >>> try alternate search paths or browser fallback")
    else:
        print("\n  >>> STOP: control also failed; proxy/signing broken globally")
    return 0 if search_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
