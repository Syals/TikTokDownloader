"""使用无头 Chromium 采集面向西班牙地区的 TikTok Web 会话。

为什么需要它
------------
用纯 Python（httpx）对 X-Bogus / X-Gnarly 签名目前会得到
``{"status_msg": "url doesn't match"}``，因为 TikTok 会校验请求指纹是否
与生成签名的浏览器环境一致。一致的设备身份必须来自同一个真实的浏览器
上下文。本脚本负责构建该上下文，并记录项目其余部分所需的全部信息。

最佳实践流程（实现如下）
  1. 以持久化配置启动 Chromium，使 cookie / 设备状态在多次运行间保留
     （Volume/browser_profile_tiktok）。
  2. 所有流量经由西班牙代理（Clash / mihomo 节点）。
  3. 在上下文中强制西班牙语区域（es-ES）与 Europe/Madrid 时区。
  4. 在访问 TikTok 之前先确认出口 IP 确实解析到西班牙。
  5. 访问 /explore，让 TikTok 引导会话并下发 device_id
     （wid）、msToken 和完整 cookie。
  6. 从同一个浏览器上下文读取：
       - device_id        <- 页面负载中嵌入的 "wid"
       - cookies          <- context.cookies()  (msToken, sessionid, ...)
       - User-Agent       <- navigator.userAgent
       - platform / os    <- navigator.platform
       - browser language <- navigator.language
       - screen size      <- window.screen
  7. 合并前将身份字段（device_id、msToken、sessionid 等）与 settings 中
     现有数据比对：任一变化即确认采到新会话；--require-change 可在
     完全一致时中止且不写入。
  8. 将采集到的身份合并进 Volume/settings.json：
       - cookie_tiktok        <- cookies (name -> value)
       - browser_info_tiktok  <- UA / platform / os / screen / device_id
     西班牙专属参数（app_language=es、region=ES、tz_name=Europe/Madrid、
     ……）会被保留，且在此处绝不被覆盖。
  9. 使用同一身份签名并发送请求：签名必须用浏览器所用的同一 UA + 参数计算。

首次登录：先带 ``--login``（有头）运行一次，在持久化配置内完成 TikTok
登录，随后正常（无头）重新运行以采集。

用法
----
    # 一次性依赖（Playwright 不在 pyproject.toml 中）
    uv pip install playwright
    uv run playwright install chromium

    # 正常采集（无头），西班牙代理
    uv run python poc/session/harvest_tiktok_session.py --proxy http://127.0.0.1:7890

    # 首次登录（有头，在窗口中手动登录）
    uv run python poc/session/harvest_tiktok_session.py --login --proxy http://127.0.0.1:7890

    # 调试性有头运行，不触发登录提示
    uv run python poc/session/harvest_tiktok_session.py --headed --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._categories import (  # noqa: E402
    find_explore_categories,
    merge_explore_categories,
    save_category_names,
)

SETTINGS_PATH = PROJECT_ROOT / "Volume" / "settings.json"
PROFILE_DIR = PROJECT_ROOT / "Volume" / "browser_profile_tiktok"

EXPLORE_URL = "https://www.tiktok.com/explore"
IP_CHECK_URL = "https://ipinfo.io/json"

WID_PATTERN = re.compile(r'"wid":"(\d{15,20})"')
CHROME_VERSION_PATTERN = re.compile(r"Chrome/([\d.]+)")
REHYDRATION_SCRIPT_PATTERN = re.compile(
    r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)

# 这些西班牙专属字段是权威的，绝不能被浏览器运行时采集到的任何内容覆盖。
# 只有 UA / platform / os / screen / device_id / browser_language 会从浏览器刷新。
SPAIN_LOCKED_PARAMS = {
    "app_language": "es",
    "language": "es",
    "webcast_language": "es",
    "region": "ES",
    "priority_region": "ES",
    "tz_name": "Europe/Madrid",
}
LOGIN_COOKIE_NAMES = frozenset({"sessionid", "sessionid_ss", "sid_tt"})

# 用于判断本次是否真的采到了新会话：任一字段变化即视为新数据。
# UA / screen 等在同 profile 下恒定，没有判别力，不参与比较。
SESSION_IDENTITY_COOKIE_NAMES = (
    "msToken",
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "tt_webid_v2",
    "tt-target-idc",
    "odin_tt",
    "tt_csrf_token",
)

# 与被采集账号/会话绑定的 browser_info 字段。当 cookie 被全新匿名会话
# 整体替换（--require-logged-out）时必须一并删除：残留会拼出矛盾指纹
# （新匿名 device_id + 旧账号 odinId），Explore 服务端会因此拒发内容
# （statusCode=0 且无 itemList 的软降权形态）。
ACCOUNT_BOUND_BROWSER_FIELDS = (
    "odinId",
    "verifyFp",
    "clientABVersions",
    "is_new_user",
    "video_encoding",
    "explore_initial_template",
    "explore_next_template",
)


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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
    print(f"[地理] 正在通过 {IP_CHECK_URL} 检查出口国家/地区 ...")
    try:
        await page.goto(IP_CHECK_URL, wait_until="domcontentloaded", timeout=20000)
        raw = await page.evaluate("() => document.body.innerText")
        info = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"[地理] 检查出口 IP 失败: {type(e).__name__}: {e}")
        return False
    country = str(info.get("country", "")).upper()
    ip = info.get("ip", "?")
    region = info.get("region", "?")
    print(f"[地理] 出口 IP={ip} region={region} country={country or '?'}")
    if country != expected:
        print(
            f"[地理] 警告: 出口国家/地区为 {country or '?'}，期望为 {expected}。"
            "代理并未经西班牙出口；TikTok 内容将不是西班牙语。"
            "请先修复代理再采集。"
        )
        return False
    print("[地理] 正常：已确认西班牙出口。")
    return True


def extract_ssr_json(html: str | None) -> object:
    """从探索页 HTML 提取 __UNIVERSAL_DATA_FOR_REHYDRATION__ 脚本内容。"""
    if not html:
        return None
    match = REHYDRATION_SCRIPT_PATTERN.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None


async def harvest_categories(page, html: str | None = None) -> dict[str, str]:
    """从探索页 SSR 提取分类映射并合并去重。

    优先读浏览器内 SSR 树；失败时回退解析已抓取的 HTML。
    """
    categories = await page.evaluate(
        """
        () => {
            const find = (obj) => {
                if (!obj || typeof obj !== 'object') return null;
                const hit = obj.exploreCategoryList;
                if (hit && typeof hit === 'object') return hit;
                for (const v of Object.values(obj)) {
                    const found = find(v);
                    if (found) return found;
                }
                return null;
            };
            const root = window.__UNIVERSAL_DATA_FOR_REHYDRATION__;
            return root ? find(root) : null;
        }
        """
    )
    merged = merge_explore_categories(categories)
    if merged:
        return merged

    if html is None:
        html = await page.content()
    payload = find_explore_categories(extract_ssr_json(html))
    return merge_explore_categories(payload)


async def harvest(context, page) -> dict:
    print(f"[采集] 正在加载 {EXPLORE_URL} ...")
    await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
    # 让 TikTok 引导会话（cookies、wid、msToken）。
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
        # 兜底：部分版本会把 web device id 放在 localStorage。
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
        "categories": await harvest_categories(page, html),
        "cookie": cookie_dict,
        "User-Agent": ua,
        "browser_platform": nav["platform"],
        "browser_language": nav["language"],
        "browser_version": chrome_version,
        "os": derive_os(nav["platform"]),
        "screen_width": str(nav["screenWidth"]),
        "screen_height": str(nav["screenHeight"]),
    }


def merge_into_settings(
    settings: dict, data: dict, *, replace_cookies: bool = False
) -> dict:
    cookie_tiktok = {} if replace_cookies else settings.get("cookie_tiktok")
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
    if replace_cookies:
        # 匿名会话整体替换 cookie 后，上一账号绑定的字段不可残留，否则
        # 请求会以新匿名设备身份携带旧账号 odinId/AB 版本/旧 query 模板。
        for field in ACCOUNT_BOUND_BROWSER_FIELDS:
            browser_info.pop(field, None)
    # 重新声明锁定的西班牙参数，确保没有陈旧值残留。
    browser_info.update(SPAIN_LOCKED_PARAMS)
    settings["browser_info_tiktok"] = browser_info
    return settings


def diff_session_identity(settings: dict, data: dict) -> tuple[bool, dict[str, str]]:
    """将新采集的身份字段与 settings 中现有数据比较。

    返回 (是否为新数据, 字段状态)。状态为 "changed" / "unchanged" /
    "absent"（旧值或新值缺失，不具判别力，不计入变化）。旧数据完全
    无可比字段时视为首次写入，直接判定为新数据。
    """
    old_info = settings.get("browser_info_tiktok")
    old_cookies = settings.get("cookie_tiktok")
    old_info = old_info if isinstance(old_info, dict) else {}
    old_cookies = old_cookies if isinstance(old_cookies, dict) else {}

    fields = [("device_id", old_info.get("device_id"), data["device_id"])]
    fields.extend(
        (f"cookie.{name}", old_cookies.get(name), data["cookie"].get(name))
        for name in SESSION_IDENTITY_COOKIE_NAMES
    )

    statuses: dict[str, str] = {}
    for name, old, new in fields:
        if not old or not new:
            statuses[name] = "absent"
        elif str(old) != str(new):
            statuses[name] = "changed"
        else:
            statuses[name] = "unchanged"

    if not any(status != "absent" for status in statuses.values()):
        return True, statuses
    return any(status == "changed" for status in statuses.values()), statuses


def report_session_diff(is_new: bool, statuses: dict[str, str]) -> None:
    if not any(status != "absent" for status in statuses.values()):
        print("[会话比对] settings 中无可比数据，按首次写入处理。")
        return
    print("[会话比对] 与 settings 中现有身份数据对比:")
    for name, status in statuses.items():
        print(f"    {name:<20} : {status}")
    if is_new:
        changed = sum(1 for status in statuses.values() if status == "changed")
        print(f"[会话比对] 已确认新会话数据（{changed} 项变化）。")
    else:
        print(
            "[会话比对] 警告: 身份字段与上次写入完全一致，"
            "疑似页面未刷新或读到陈旧会话。"
        )


def login_cookie_names(cookies: Mapping[str, str]) -> list[str]:
    return sorted(name for name in cookies if name.lower() in LOGIN_COOKIE_NAMES)


def report(data: dict) -> None:
    print("\n" + "=" * 60)
    print("[采集结果]")
    print("=" * 60)
    print(f"  device_id        : {data['device_id'] or '(未找到)'}")
    print(f"  User-Agent       : {data['User-Agent']}")
    print(f"  platform / os    : {data['browser_platform']} / {data['os']}")
    print(f"  browser_language : {data['browser_language']}")
    print(f"  screen           : {data['screen_width']}x{data['screen_height']}")
    print(f"  cookie 数量      : {len(data['cookie'])}")
    print(f"  explore 分类     : {len(data.get('categories', {}))} 个")
    has_session = "sessionid" in data["cookie"]
    has_mstoken = "msToken" in data["cookie"]
    print(f"  sessionid        : {'yes' if has_session else 'NO'}")
    print(f"  msToken          : {'yes' if has_mstoken else 'NO'}")
    if not data["device_id"]:
        print("  注意: 未找到 device_id (wid)；页面可能被拦截。")
    if not has_session:
        print(
            "  注意: 没有 sessionid -> 未登录。可用 --require-logged-out "
            "将此状态保存为隔离的无登录会话。"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="使用无头 Chromium 采集（西班牙）TikTok Web 会话。",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="西班牙代理 URL，例如 http://127.0.0.1:7890（Clash/mihomo）。",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--login",
        action="store_true",
        help="有头运行并暂停以供手动登录 TikTok，随后退出。",
    )
    mode_group.add_argument(
        "--require-logged-out",
        action="store_true",
        help="拒绝登录 Cookie，并以捕获的 Cookie 覆盖 settings 中的旧会话。",
    )
    parser.add_argument(
        "--require-change",
        action="store_true",
        help=(
            "当身份字段与 settings 现有数据完全一致时中止且不写入，"
            "用于确认采到的是新会话。"
        ),
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="有头（可见）运行，但不触发登录暂停。",
    )
    parser.add_argument(
        "--skip-geo",
        action="store_true",
        help="跳过西班牙出口 IP 检查。",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=PROFILE_DIR,
        help="Chromium 持久化 profile 路径。",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=SETTINGS_PATH,
        help="写入浏览器会话的 settings JSON 路径。",
    )
    args = parser.parse_args()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "[致命] 未安装 Playwright。请运行：\n"
            "  uv pip install playwright\n"
            "  uv run playwright install chromium"
        )
        return 2

    args.profile_dir.mkdir(parents=True, exist_ok=True)
    headless = not (args.login or args.headed)
    launch_kwargs: dict = {
        "headless": headless,
        "locale": "es-ES",
        "timezone_id": "Europe/Madrid",
        "viewport": {"width": 1536, "height": 864},
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }
    if args.proxy:
        launch_kwargs["proxy"] = {"server": args.proxy}
        print(f"[初始化] proxy = {args.proxy}")
    else:
        print(
            "[初始化] 警告: 未提供 --proxy。若主机当前未经由西班牙出口，"
            "TikTok 内容将不是西班牙语。"
        )
    print(f"[初始化] profile = {args.profile_dir}")
    print(f"[初始化] headless = {headless}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(args.profile_dir),
            **launch_kwargs,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            if not args.skip_geo:
                ok = await check_egress_country(page, "ES")
                if not ok and not args.login:
                    print("[中止] 出口非西班牙；请修复代理后重试。")
                    print("        （可用 --skip-geo 绕过，不推荐）")
                    return 1

            if args.login:
                print("[登录] 正在打开 TikTok 以供手动登录 ...")
                await page.goto(EXPLORE_URL, wait_until="domcontentloaded")
                print(
                    "[登录] 请在浏览器窗口中完成登录，然后回到这里按"
                    "回车保存会话并退出。"
                )
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, input)
                await context.close()
                return 0

            data = await harvest(context, page)
            report(data)
            if args.require_logged_out:
                if names := login_cookie_names(data["cookie"]):
                    print("\n[中止] profile 包含登录 Cookie: " + ", ".join(names))
                    return 1
            if not data["device_id"] and "sessionid" not in data["cookie"]:
                print("\n[中止] 未采集到有用信息（被拦截 / 未登录）。")
                return 1

            settings = load_settings(args.settings)
            is_new, statuses = diff_session_identity(settings, data)
            report_session_diff(is_new, statuses)
            if not is_new and args.require_change:
                print(
                    "[中止] --require-change 生效且未检测到任何变化；"
                    "settings 与分类映射保持不变。"
                )
                return 1
            settings = merge_into_settings(
                settings, data, replace_cookies=args.require_logged_out
            )
            save_settings(args.settings, settings)
            print(f"\n[完成] 已合并进 {args.settings}")
            print(
                "     已保留的西班牙参数: "
                + ", ".join(f"{k}={v}" for k, v in SPAIN_LOCKED_PARAMS.items())
            )
            if categories := data.get("categories"):
                categories_path = save_category_names(categories)
                print(
                    f"[完成] 已写入分类映射 {categories_path} "
                    f"({len(categories)} 个分类)"
                )
            else:
                print(
                    "[警告] 未从页面提取到 explore 分类列表；"
                    "Volume/explore_categories.json 未更新。"
                )
            return 0
        finally:
            await context.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
