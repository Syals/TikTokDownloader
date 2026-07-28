"""Harvest a TikTok web session from a headless Chromium for the Spain region.

Why this exists
---------------
Signing X-Bogus / X-Gnarly in pure Python (httpx) currently yields
``{"status_msg": "url doesn't match"}`` because TikTok validates that the
request fingerprint matches the browser environment that produced the
signature. A consistent identity must come from a single, real browser
context. This script builds that context and records everything the rest of
the project needs.

Best-practice flow (implemented below)
  1. Launch Chromium with a PERSISTENT profile so cookies / device state
     survive across runs (Volume/browser_profile_tiktok).
  2. Route all traffic through the Spain proxy (Clash / mihomo node).
  3. Force Spanish locale (es-ES) and Europe/Madrid timezone in the context.
  4. Verify the egress IP actually resolves to Spain before touching TikTok.
  5. Visit /explore so TikTok bootstraps the session and emits device_id
     (wid), msToken and the full cookie jar.
  6. Read, from that one browser context:
       - device_id        <- "wid" embedded in the page payload
       - cookies          <- context.cookies()  (msToken, sessionid, ...)
       - User-Agent       <- navigator.userAgent
       - platform / os    <- navigator.platform
       - browser language <- navigator.language
       - screen size      <- window.screen
  7. Merge the harvested identity into Volume/settings.json:
       - cookie_tiktok        <- cookies (name -> value)
       - browser_info_tiktok  <- UA / platform / os / screen / device_id
     Spain-specific params (app_language=es, region=ES, tz_name=Europe/Madrid,
     ...) are preserved and never overwritten here.
  8. Sign and send requests using THIS SAME identity: the signature must be
     computed with the same UA + params the browser used.

First-time login: run once with ``--login`` (headed) to complete TikTok login
inside the persistent profile, then re-run normally (headless) to harvest.

Usage
-----
    # one-time dependency (Playwright is NOT in pyproject.toml)
    uv pip install playwright
    uv run playwright install chromium

    # normal harvest (headless), Spain proxy
    uv run python harvest_tiktok_session.py --proxy http://127.0.0.1:7890

    # first-time login (headed, manual login in the window)
    uv run python harvest_tiktok_session.py --login --proxy http://127.0.0.1:7890

    # debug headed run without login prompt
    uv run python harvest_tiktok_session.py --headed --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_ROOT / "Volume" / "settings.json"
PROFILE_DIR = PROJECT_ROOT / "Volume" / "browser_profile_tiktok"

EXPLORE_URL = "https://www.tiktok.com/explore"
IP_CHECK_URL = "https://ipinfo.io/json"

WID_PATTERN = re.compile(r'"wid":"(\d{15,20})"')
CHROME_VERSION_PATTERN = re.compile(r"Chrome/([\d.]+)")

# These Spain-specific fields are authoritative and must NOT be overwritten by
# anything harvested from the browser runtime. Only UA / platform / os /
# screen / device_id / browser_language are refreshed from the browser.
SPAIN_LOCKED_PARAMS = {
    "app_language": "es",
    "language": "es",
    "webcast_language": "es",
    "region": "ES",
    "priority_region": "ES",
    "tz_name": "Europe/Madrid",
}


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        print(f"[FATAL] settings.json not found at {SETTINGS_PATH}")
        raise SystemExit(2)
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def derive_os(platform: str) -> str:
    p = platform.lower()
    if "linux" in p:
        return "linux"
    if "mac" in p or "darwin" in p:
        return "mac"
    if "win" in p:
        return "windows"
    return "linux"


async def check_egress_country(page, expected: str = "ES") -> bool:
    print(f"[GEO] Checking egress country via {IP_CHECK_URL} ...")
    try:
        await page.goto(IP_CHECK_URL, wait_until="domcontentloaded", timeout=20000)
        raw = await page.evaluate("() => document.body.innerText")
        info = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"[GEO] FAILED to inspect egress IP: {type(e).__name__}: {e}")
        return False
    country = str(info.get("country", "")).upper()
    ip = info.get("ip", "?")
    region = info.get("region", "?")
    print(f"[GEO] Egress IP={ip} region={region} country={country or '?'}")
    if country != expected:
        print(
            f"[GEO] WARNING: egress country is {country or '?'}, expected {expected}. "
            "The proxy is NOT routing through Spain; TikTok content will not be "
            "Spanish. Fix the proxy before harvesting."
        )
        return False
    print("[GEO] OK: Spain egress confirmed.")
    return True


async def harvest(context, page) -> dict:
    print(f"[HARVEST] Loading {EXPLORE_URL} ...")
    await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
    # Let TikTok bootstrap the session (cookies, wid, msToken).
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(3)

    html = await page.content()
    nav = await page.evaluate(
        """() => ({
            userAgent: navigator.userAgent,
            platform: navigator.platform,
            language: navigator.language,
            screenWidth: window.screen.width,
            screenHeight: window.screen.height,
        })"""
    )
    cookies = await context.cookies()

    wid = ""
    if m := WID_PATTERN.search(html):
        wid = m.group(1)
    if not wid:
        # Fallback: some builds surface the web device id in localStorage.
        try:
            local = await page.evaluate(
                """() => {
                    const out = {};
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        out[k] = localStorage.getItem(k);
                    }
                    return out;
                }"""
            )
            blob = json.dumps(local)
            if m := WID_PATTERN.search(blob):
                wid = m.group(1)
        except Exception:  # noqa: BLE001
            pass

    cookie_dict = {c["name"]: c["value"] for c in cookies}
    ua = nav["userAgent"]
    chrome_version = ""
    if m := CHROME_VERSION_PATTERN.search(ua):
        chrome_version = m.group(1)

    return {
        "device_id": wid,
        "cookie": cookie_dict,
        "User-Agent": ua,
        "browser_platform": nav["platform"],
        "browser_language": nav["language"],
        "browser_version": chrome_version,
        "os": derive_os(nav["platform"]),
        "screen_width": str(nav["screenWidth"]),
        "screen_height": str(nav["screenHeight"]),
    }


def merge_into_settings(settings: dict, data: dict) -> dict:
    cookie_tiktok = settings.get("cookie_tiktok")
    if not isinstance(cookie_tiktok, dict):
        cookie_tiktok = {}
    cookie_tiktok.update(data["cookie"])
    settings["cookie_tiktok"] = cookie_tiktok

    browser_info = settings.get("browser_info_tiktok")
    if not isinstance(browser_info, dict):
        browser_info = {}
    browser_info.update(
        {
            "User-Agent": data["User-Agent"],
            "browser_platform": data["browser_platform"],
            "browser_language": data["browser_language"],
            "browser_version": data["browser_version"],
            "os": data["os"],
            "screen_width": data["screen_width"],
            "screen_height": data["screen_height"],
            "device_id": data["device_id"],
        }
    )
    # Re-assert the locked Spain params so nothing stale survives.
    browser_info.update(SPAIN_LOCKED_PARAMS)
    settings["browser_info_tiktok"] = browser_info
    return settings


def report(data: dict) -> None:
    print("\n" + "=" * 60)
    print("[HARVEST RESULT]")
    print("=" * 60)
    print(f"  device_id        : {data['device_id'] or '(not found)'}")
    print(f"  User-Agent       : {data['User-Agent']}")
    print(f"  platform / os    : {data['browser_platform']} / {data['os']}")
    print(f"  browser_language : {data['browser_language']}")
    print(f"  screen           : {data['screen_width']}x{data['screen_height']}")
    print(f"  cookie count     : {len(data['cookie'])}")
    has_session = "sessionid" in data["cookie"]
    has_mstoken = "msToken" in data["cookie"]
    print(f"  sessionid        : {'yes' if has_session else 'NO'}")
    print(f"  msToken          : {'yes' if has_mstoken else 'NO'}")
    if not data["device_id"]:
        print("  NOTE: device_id (wid) not found; the page may have been blocked.")
    if not has_session:
        print(
            "  NOTE: no sessionid -> not logged in. Run once with --login to "
            "log in inside the persistent profile, then re-run."
        )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Harvest a TikTok web session (Spain) from headless Chromium.",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="Spain proxy URL, e.g. http://127.0.0.1:7890 (Clash/mihomo).",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Run headed and pause for manual TikTok login, then exit.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run headed (visible) without the login pause.",
    )
    parser.add_argument(
        "--skip-geo",
        action="store_true",
        help="Skip the Spain egress IP check.",
    )
    args = parser.parse_args()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "[FATAL] Playwright is not installed. Run:\n"
            "  uv pip install playwright\n"
            "  uv run playwright install chromium"
        )
        return 2

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    headless = not (args.login or args.headed)
    launch_kwargs: dict = {
        "headless": headless,
        "locale": "es-ES",
        "timezone_id": "Europe/Madrid",
        "viewport": {"width": 1536, "height": 864},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if args.proxy:
        launch_kwargs["proxy"] = {"server": args.proxy}
        print(f"[SETUP] proxy = {args.proxy}")
    else:
        print(
            "[SETUP] WARNING: no --proxy given. If the host is not already "
            "routed through Spain, TikTok content will not be Spanish."
        )
    print(f"[SETUP] profile = {PROFILE_DIR}")
    print(f"[SETUP] headless = {headless}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            **launch_kwargs,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            if not args.skip_geo:
                ok = await check_egress_country(page, "ES")
                if not ok and not args.login:
                    print("[ABORT] Egress is not Spain; fix the proxy and retry.")
                    print("        (use --skip-geo to bypass, not recommended)")
                    return 1

            if args.login:
                print("[LOGIN] Opening TikTok for manual login ...")
                await page.goto(EXPLORE_URL, wait_until="domcontentloaded")
                print(
                    "[LOGIN] Complete login in the browser window, then press "
                    "Enter here to save the session and exit."
                )
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, input)
                await context.close()
                return 0

            data = await harvest(context, page)
            report(data)
            if not data["device_id"] and "sessionid" not in data["cookie"]:
                print("\n[ABORT] Nothing useful harvested (blocked / not logged in).")
                return 1

            settings = load_settings()
            settings = merge_into_settings(settings, data)
            save_settings(settings)
            print(f"\n[OK] Merged into {SETTINGS_PATH}")
            print(
                "     Spain params preserved: "
                + ", ".join(f"{k}={v}" for k, v in SPAIN_LOCKED_PARAMS.items())
            )
            return 0
        finally:
            await context.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
