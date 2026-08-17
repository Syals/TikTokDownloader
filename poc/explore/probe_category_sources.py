"""探针：对比 Explore 分类映射「实时获取」两条路线的真实可用性。

背景
----
``tiktok_explore_batch.py`` 计划在批量爬取前实时刷新
``Volume/explore_categories.json``，候选路线：

- 方案 A：纯 HTTP（httpx）请求 ``https://www.tiktok.com/explore``，
  解析 HTML 内嵌 SSR（``__UNIVERSAL_DATA_FOR_REHYDRATION__``）里的
  ``exploreCategoryList``；
- 方案 B：匿名浏览器（Playwright Chromium）读取
  ``window.__UNIVERSAL_DATA_FOR_REHYDRATION__``（harvest 现行做法）。

风险：方案 A 的 HTML 请求可能被 TLS / JS 指纹风控拦截（返回验证页或缺 SSR），
本脚本在生产出口实测并逐项对比，为路线取舍提供证据。

探针矩阵
--------
A1  httpx + ``Volume/settings.json`` 登录会话（cookie + UA）
A2  httpx 匿名（无 cookie，同 UA）
A3  httpx 匿名 + 预热（先访问首页让 cookie jar 收引导 cookie 再请求 explore）
B   匿名浏览器（与 ``tiktok_explore_anonymous`` 相同 context 参数）

每个探针记录：HTTP 状态、重定向链、HTML 大小、SSR / 分类列表存在性、
风控信号、``exploreCategoryList`` 分组结构与各组 ID 顺序、合并后映射，
并与本地 ``Volume/explore_categories.json`` 及 B 探针结果对比。

全程只读：不写 ``Volume/`` 下任何文件；报告写入 ``--output``。

用法
----
    uv run python poc/explore/probe_category_sources.py
    uv run python poc/explore/probe_category_sources.py --headed
    uv run python poc/explore/probe_category_sources.py \
        --proxy http://127.0.0.1:7890 --skip-browser
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore._categories import (  # noqa: E402
    DEFAULT_CATEGORIES_PATH,
    find_explore_categories,
    load_category_names,
    merge_explore_categories,
)
from poc.explore.tiktok_explore_replay import (  # noqa: E402
    DEFAULT_SETTINGS_PATH,
    load_session,
)
from poc.session.harvest_tiktok_session import (  # noqa: E402
    EXPLORE_URL,
    IP_CHECK_URL,
    REHYDRATION_SCRIPT_PATTERN,
    check_egress_country,
    extract_ssr_json,
)


DEFAULT_OUTPUT = Path(".output/explore/probe_categories/report.json")
WARMUP_URL = "https://www.tiktok.com/"
DEFAULT_TIMEOUT = 30.0

# settings.json 缺失/不可用时，匿名探针使用的兜底 UA（Linux Chromium）。
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

# HTML 文本风控特征（小写子串 → 信号名）。
CHALLENGE_TEXT_MARKERS = (
    ("captcha", "captcha_marker"),
    ("are you a robot", "robot_check"),
    ("slide to verify", "slide_verify"),
)

# 与 tiktok_explore_anonymous.harvest_categories 相同的浏览器内查找逻辑。
FIND_CATEGORY_LIST_JS = """
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


def detect_challenge_signals(
    status: int | None,
    final_url: str | None,
    text: str | None,
) -> list[str]:
    """从状态码 / 最终 URL / 响应文本推断风控拦截信号。"""
    signals: list[str] = []
    if status is not None and status != 200:
        signals.append(f"http_status_{status}")
    url_lower = (final_url or "").lower()
    if any(key in url_lower for key in ("/login", "verify", "captcha")):
        signals.append("suspicious_url")
    text_lower = (text or "").lower()
    for marker, label in CHALLENGE_TEXT_MARKERS:
        if marker in text_lower:
            signals.append(label)
    return signals


def analyze_category_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """展开 exploreCategoryList 原始分组，输出结构与顺序信息。

    - ``groups``：各版本组（v0/v1/v2…）的条目数与按序 ID 列表；
    - ``bar_order``：首组 ID 顺序（浏览器分类栏渲染顺序的近似）；
    - ``merged``：按 v0 优先合并后的 ID→名称映射（与 harvest 落盘一致）。
    """
    groups: dict[str, dict[str, Any]] = {}
    for group_key, entries in payload.items():
        if not isinstance(entries, list):
            continue
        # 与 merge_explore_categories 同口径：type/name 不合规或空名条目跳过。
        types = [
            str(entry["type"])
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("type"), (str, int))
            and isinstance(entry.get("name"), str)
            and entry["name"].strip()
        ]
        groups[str(group_key)] = {"count": len(entries), "types": types}
    merged = merge_explore_categories(payload)
    bar_order = next(iter(groups.values()))["types"] if groups else []
    return {
        "groups": groups,
        "group_count": len(groups),
        "bar_order": bar_order,
        "merged": merged,
        "merged_count": len(merged),
    }


def common_prefix_len(a: Sequence[str], b: Sequence[str]) -> int:
    """两个序列的公共前缀长度（用于分类栏顺序稳定性对比）。"""
    matched = 0
    for left, right in zip(a, b):
        if left != right:
            break
        matched += 1
    return matched


def sort_ids(ids: set[str] | list[str]) -> list[str]:
    return sorted(ids, key=int)


def httpx_supports_http2() -> bool:
    try:
        with httpx.Client(http2=True):
            return True
    except ImportError:
        return False


def build_httpx_client(
    *,
    user_agent: str,
    cookie: dict[str, str] | None,
    proxy: str | None,
    timeout: float,
    http2: bool,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
        cookies=cookie or None,
        follow_redirects=True,
        proxy=proxy or None,
        timeout=timeout,
        verify=False,
        http2=http2,
    )


def _base_result(mode: str, transport: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "transport": transport,
        "ok": False,
        "error": None,
        "http_status": None,
        "final_url": None,
        "redirect_chain": [],
        "html_bytes": None,
        "has_rehydration_script": None,
        "ssr_parse_ok": None,
        "found_category_list": None,
        "challenge_signals": [],
        "elapsed_ms": None,
    }


def _analyze_html(text: str, result: dict[str, Any]) -> None:
    result["html_bytes"] = len(text.encode("utf-8", errors="replace"))
    result["has_rehydration_script"] = bool(REHYDRATION_SCRIPT_PATTERN.search(text))
    result["challenge_signals"] = detect_challenge_signals(
        result["http_status"], result["final_url"], text
    )
    payload = extract_ssr_json(text)
    result["ssr_parse_ok"] = payload is not None
    category_list = find_explore_categories(payload) if payload is not None else None
    result["found_category_list"] = category_list is not None
    if isinstance(category_list, dict):
        result |= analyze_category_payload(category_list)
    result["ok"] = result["found_category_list"] is True


async def probe_httpx(
    mode: str,
    *,
    user_agent: str,
    cookie: dict[str, str] | None,
    proxy: str | None,
    timeout: float,
    http2: bool,
    warmup: bool = False,
) -> dict[str, Any]:
    """方案 A 探针：纯 HTTP 请求 explore 页并解析 SSR 分类列表。"""
    result = _base_result(mode, f"httpx/{'2' if http2 else '1.1'}")
    started = time.monotonic()
    try:
        async with build_httpx_client(
            user_agent=user_agent,
            cookie=cookie,
            proxy=proxy,
            timeout=timeout,
            http2=http2,
        ) as client:
            if warmup:
                warmup_resp = await client.get(WARMUP_URL)
                result["warmup_status"] = warmup_resp.status_code
                await asyncio.sleep(1)
            resp = await client.get(EXPLORE_URL)
            result["http_status"] = resp.status_code
            result["final_url"] = str(resp.url)
            result["redirect_chain"] = [str(r.url) for r in resp.history]
            _analyze_html(resp.text, result)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


async def probe_browser(
    *,
    proxy: str | None,
    headed: bool,
    skip_geo: bool,
    timeout: float,
) -> dict[str, Any]:
    """方案 B 探针：匿名浏览器读取 window 内 SSR 分类列表（不落盘）。"""
    result = _base_result("B_browser_anonymous", "playwright-chromium")
    started = time.monotonic()
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        result["error"] = f"未安装 Playwright: {exc}"
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return result

    browser_kwargs: dict[str, Any] = {
        "headless": not headed,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if proxy:
        browser_kwargs["proxy"] = {"server": proxy}

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**browser_kwargs)
            context = await browser.new_context(
                locale="es-ES",
                timezone_id="Europe/Madrid",
                viewport={"width": 1536, "height": 864},
                service_workers="block",
            )
            try:
                page = await context.new_page()
                result["browser_user_agent"] = await page.evaluate(
                    "() => navigator.userAgent"
                )
                if not skip_geo and not await check_egress_country(page, "ES"):
                    result["error"] = "西班牙出口检查失败"
                    return result
                resp = await page.goto(
                    EXPLORE_URL, wait_until="domcontentloaded", timeout=timeout * 1000
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(3)

                if resp is not None:
                    result["http_status"] = resp.status
                    result["final_url"] = page.url
                category_list = await page.evaluate(FIND_CATEGORY_LIST_JS)
                html = await page.content()
                result["html_via_browser"] = len(html.encode("utf-8", errors="replace"))
                result["has_rehydration_script"] = bool(
                    REHYDRATION_SCRIPT_PATTERN.search(html)
                )
                result["challenge_signals"] = detect_challenge_signals(
                    result["http_status"], result["final_url"], html
                )
                if not isinstance(category_list, dict):
                    # JS 读取失败时回退解析 HTML（与 harvest_categories 一致）。
                    payload = extract_ssr_json(html)
                    category_list = (
                        find_explore_categories(payload)
                        if payload is not None
                        else None
                    )
                result["found_category_list"] = isinstance(category_list, dict)
                if isinstance(category_list, dict):
                    result |= analyze_category_payload(category_list)
                result["ok"] = result["found_category_list"] is True
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


def load_local_snapshot(path: Path) -> dict[str, Any]:
    """读取本地映射快照（只读，不修改）。"""
    snapshot: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "id_count": 0,
        "insertion_order": [],
        "meta": None,
    }
    if not path.exists():
        return snapshot
    names = load_category_names(path)
    snapshot["id_count"] = len(names)
    snapshot["insertion_order"] = list(names)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("_meta"), dict):
            snapshot["meta"] = data["_meta"]
    except (OSError, ValueError):
        pass
    return snapshot


def build_comparison(report: Mapping[str, Any]) -> dict[str, Any]:
    """各探针 vs 本地映射、A 系探针 vs B 探针的 ID 集合与顺序对比。"""
    local_ids = set(report["local_file"]["insertion_order"])
    comparison: dict[str, Any] = {"vs_local": {}, "vs_browser": {}}
    merged_views: dict[str, dict[str, Any]] = {}
    for mode, probe in report["probes"].items():
        merged = probe.get("merged")
        if not isinstance(merged, dict):
            continue
        merged_views[mode] = probe
        probe_ids = set(merged)
        comparison["vs_local"][mode] = {
            "ids_equal": probe_ids == local_ids,
            "only_in_probe": sort_ids(probe_ids - local_ids),
            "only_in_local": sort_ids(local_ids - probe_ids),
        }

    browser = merged_views.get("B_browser_anonymous")
    if browser:
        browser_ids = set(browser["merged"])
        for mode, probe in merged_views.items():
            if mode == "B_browser_anonymous":
                continue
            entry = {
                "ids_equal": set(probe["merged"]) == browser_ids,
                "bar_order_equal": probe["bar_order"] == browser["bar_order"],
                "bar_order_common_prefix": common_prefix_len(
                    probe["bar_order"], browser["bar_order"]
                ),
            }
            comparison["vs_browser"][mode] = entry

    hints: list[str] = []
    a1 = report["probes"].get("A1_httpx_logged_in", {})
    a3 = report["probes"].get("A3_httpx_anonymous_warmed", {})
    if a1.get("ok"):
        hints.append(
            "A1 可用：batch 可在 load_session 后用登录 cookie 直接 HTTP 刷新映射。"
        )
    if a3.get("ok"):
        hints.append("A3 可用：无登录会话时也可纯 HTTP 刷新（预热后匿名请求）。")
    if browser and browser.get("ok"):
        hints.append("B 可用：与现行 harvest 行为一致，作为兜底或对照。")
    if not hints:
        hints.append("所有探针均未取得分类列表，优先排查出口 IP 与请求指纹。")
    comparison["hints"] = hints
    return comparison


def print_summary(report: Mapping[str, Any]) -> None:
    local = report["local_file"]
    print(
        f"[本地] {local['path']}: "
        f"{'存在' if local['exists'] else '缺失'}, "
        f"{local['id_count']} 个 ID"
    )
    for mode, probe in report["probes"].items():
        if probe.get("error") and not probe.get("ok"):
            print(f"[{mode:<26}] FAIL {probe['error']} ({probe.get('elapsed_ms')} ms)")
            continue
        merged_count = probe.get("merged_count")
        line = (
            f"[{mode:<26}] {'PASS' if probe.get('ok') else 'FAIL'} "
            f"status={probe.get('http_status')} "
            f"html={probe.get('html_bytes') or probe.get('html_via_browser')} "
            f"groups={probe.get('group_count')} "
            f"merged={merged_count} "
            f"bar={len(probe.get('bar_order') or [])} "
            f"重定向={len(probe.get('redirect_chain') or [])} "
            f"风控信号={probe.get('challenge_signals') or '无'} "
            f"({probe.get('elapsed_ms')} ms)"
        )
        print(line)

    comparison = report["comparison"]
    for mode, entry in comparison["vs_local"].items():
        if entry["ids_equal"]:
            print(f"[对比] {mode} 与本地映射 ID 集合一致")
        else:
            print(
                f"[对比] {mode} 与本地映射不一致: "
                f"新增 {entry['only_in_probe'] or '无'} / "
                f"缺失 {entry['only_in_local'] or '无'}"
            )
    for mode, entry in comparison["vs_browser"].items():
        order = (
            "一致"
            if entry["bar_order_equal"]
            else f"前缀 {entry['bar_order_common_prefix']} 项相同后分歧"
        )
        print(
            f"[对比] {mode} vs B: ID 集合"
            f"{'一致' if entry['ids_equal'] else '不一致'}; "
            f"分类栏顺序{order}"
        )
    for hint in comparison["hints"]:
        print(f"[结论] {hint}")


def save_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对比 Explore 分类映射实时获取的 HTTP 与浏览器路线。",
    )
    parser.add_argument(
        "--live", action="store_true", help="兼容惯例占位；本脚本始终联网。"
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help="A1 探针使用的登录会话 settings.json。",
    )
    parser.add_argument(
        "--categories-file",
        type=Path,
        default=DEFAULT_CATEGORIES_PATH,
        help="本地分类映射（仅作对比基准，只读）。",
    )
    parser.add_argument("--proxy", default=None, help="httpx/浏览器统一代理。")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-geo", action="store_true", help="跳过西班牙出口校验。")
    parser.add_argument("--skip-browser", action="store_true", help="跳过 B 探针。")
    parser.add_argument("--headed", action="store_true", help="浏览器探针有头运行。")
    return parser.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "explore_url": EXPLORE_URL,
            "proxy": bool(args.proxy),
            "timeout": args.timeout,
        },
        "probes": {},
    }

    if not args.skip_geo:
        try:
            async with httpx.AsyncClient(
                proxy=args.proxy or None, timeout=args.timeout, verify=False
            ) as client:
                geo = (await client.get(IP_CHECK_URL)).json()
        except Exception as exc:  # noqa: BLE001
            print(f"[地理] 出口检查失败: {type(exc).__name__}: {exc}")
            return 2
        report["meta"]["geo"] = geo
        country = geo.get("country")
        print(f"[地理] 出口 IP={geo.get('ip')} country={country}")
        if country != "ES":
            print("[地理] 非西班牙出口，中止。需要时可用 --skip-geo 强制继续。")
            return 2
    else:
        report["meta"]["geo"] = {"skipped": True}

    report["local_file"] = load_local_snapshot(args.categories_file)

    http2 = httpx_supports_http2()
    report["meta"]["http2"] = http2
    if not http2:
        print("[提示] 未安装 h2 包，httpx 探针将以 HTTP/1.1 运行。")

    session_cookie: dict[str, str] | None = None
    user_agent = FALLBACK_USER_AGENT
    try:
        session_cookie, user_agent = load_session(args.settings)
        report["meta"]["a1_session"] = {
            "settings": str(args.settings),
            "cookie_count": len(session_cookie),
        }
    except (OSError, ValueError) as exc:
        report["meta"]["a1_session"] = {
            "settings": str(args.settings),
            "skipped_reason": f"{type(exc).__name__}: {exc}",
        }
        print(f"[提示] A1 跳过（settings 会话不可用）: {exc}")

    print("[探针] A1: httpx + 登录会话 ...")
    if session_cookie:
        report["probes"]["A1_httpx_logged_in"] = await probe_httpx(
            "A1_httpx_logged_in",
            user_agent=user_agent,
            cookie=session_cookie,
            proxy=args.proxy,
            timeout=args.timeout,
            http2=http2,
        )
    else:
        skipped = _base_result("A1_httpx_logged_in", "httpx")
        skipped["error"] = "settings 会话不可用，探针跳过"
        report["probes"]["A1_httpx_logged_in"] = skipped

    print("[探针] A2: httpx 匿名 ...")
    report["probes"]["A2_httpx_anonymous"] = await probe_httpx(
        "A2_httpx_anonymous",
        user_agent=user_agent,
        cookie=None,
        proxy=args.proxy,
        timeout=args.timeout,
        http2=http2,
    )
    await asyncio.sleep(1)

    print("[探针] A3: httpx 匿名 + 预热 ...")
    report["probes"]["A3_httpx_anonymous_warmed"] = await probe_httpx(
        "A3_httpx_anonymous_warmed",
        user_agent=user_agent,
        cookie=None,
        proxy=args.proxy,
        timeout=args.timeout,
        http2=http2,
        warmup=True,
    )

    if args.skip_browser:
        print("[探针] B: 已按 --skip-browser 跳过")
    else:
        print("[探针] B: 匿名浏览器 ...")
        report["probes"]["B_browser_anonymous"] = await probe_browser(
            proxy=args.proxy,
            headed=args.headed,
            skip_geo=args.skip_geo,
            timeout=args.timeout,
        )

    report["comparison"] = build_comparison(report)
    save_report(args.output, report)
    print(f"\n[报告] 已写入 {args.output}\n")
    print_summary(report)

    return 0 if any(p.get("ok") for p in report["probes"].values()) else 1


def main() -> int:
    return asyncio.run(amain(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
