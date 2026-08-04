"""诊断 TikTok Explore 关键字段来源。

问题背景
--------
``tiktok_explore_poc.py`` / ``tiktok_explore_batch.py`` 使用合成参数模板
（``APITikTok.params``）请求 ``/api/explore/item_list/`` 时，每个分类只能拿到
约一页（~19 条）内容，cursor=0 重复请求会被服务端识别为重复请求。

真实浏览器请求比合成模板多出几个关键字段，其中 ``clientABVersions``、
``odinId`` 和 ``verifyFp`` 最有可能是服务端推进翻页的依据。

本脚本用同一个已登录的持久化 profile 打开浏览器，监听 Explore 请求并翻页，
同时探测这些字段在页面 SSR 数据 / storage / cookie / 请求参数中的来源。
全程只读，不修改 profile 和文件。

用法
----
    uv run python -m poc.explore.diagnose_explore_fields --headed --proxy http://127.0.0.1:7890

输出只包含字段长度和少量前导字符，避免泄露完整 cookie/token。
"""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poc.explore.tiktok_explore_replay import (  # noqa: E402
    DEFAULT_SETTINGS_PATH,
    load_session,
)
from poc.explore.tiktok_explore_poc import (  # noqa: E402
    BROWSER_REQUEST_FIELDS,
    EXPLORE_TEMPLATE_KEYS,
)
from poc.session.harvest_tiktok_session import (  # noqa: E402
    EXPLORE_URL,
    PROFILE_DIR,
    check_egress_country,
)


TARGET_FIELDS = BROWSER_REQUEST_FIELDS + ("msToken", "cursor")
SSR_KEYS = ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE", "__NEXT_DATA__")


def truncate(value: str, max_length: int = 10) -> str:
    """返回脱敏后的短字符串，避免完整凭证泄露到终端。"""
    if not isinstance(value, str):
        value = str(value) if value is not None else ""
    if len(value) <= max_length + 3:
        return value
    return f"{value[:max_length]}...({len(value)})"


def short_hash(value: str) -> str | None:
    """生成适合终端记录的短哈希，不泄露会话字段原值。"""
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


def capture_explore_query(request, queries: list[dict[str, str]]) -> None:
    """记录一次 /api/explore/item_list/ 请求的 query 参数。"""
    if "/api/explore/item_list/" not in request.url:
        return
    parts = urlsplit(request.url)
    queries.append(dict(parse_qsl(parts.query, keep_blank_values=True)))


def print_separator(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def analyze_queries(queries: list[dict[str, str]]) -> None:
    """打印每个捕获请求的目标字段来源。"""
    print_separator(f"捕获到 {len(queries)} 条 /api/explore/item_list/ 请求")
    for index, params in enumerate(queries, start=1):
        pull_type = params.get("pullType", "?")
        cursor = truncate(params["cursor"], 12) if "cursor" in params else "<absent>"
        print(f"\n[请求 {index}] pullType={pull_type}, cursor={cursor}")
        print(
            f"  categoryType={params.get('categoryType', '?')}, "
            f"count={params.get('count', '?')}, "
            f"from_page={params.get('from_page', '<absent>')}"
        )
        for field in TARGET_FIELDS:
            value = params.get(field)
            if value:
                extra = f", sha256={short_hash(value)}" if field == "msToken" else ""
                print(
                    f"  {field}: source=query, "
                    f"len={len(value)}, sample={truncate(value, 8)}{extra}"
                )
    if queries:
        print("\n首个浏览器请求字段:")
        print(", ".join(sorted(queries[0])))


async def capture_explore_response(response, responses: list[dict[str, Any]]) -> None:
    """记录 Explore 响应中的脱敏分页信息。"""
    if "/api/explore/item_list/" not in response.url:
        return
    try:
        payload = await response.json()
    except Exception:  # noqa: BLE001
        return
    if not isinstance(payload, dict):
        return

    params = dict(
        parse_qsl(urlsplit(response.request.url).query, keep_blank_values=True)
    )
    item_list = payload.get("itemList")
    items = (
        [item for item in item_list if isinstance(item, dict)]
        if isinstance(item_list, list)
        else []
    )
    responses.append(
        {
            "pull_type": params.get("pullType", "?"),
            "cursor_present": "cursor" in params,
            "cursor_length": len(params.get("cursor", "")),
            "ms_token_present": "msToken" in params,
            "ms_token_length": len(params.get("msToken", "")),
            "http_status": response.status,
            "item_count": len(items),
            "item_id_hashes": [
                hashlib.sha256(item["id"].encode()).hexdigest()[:12]
                for item in items
                if isinstance(item.get("id"), str)
            ],
            "response_ms_token_hash": short_hash(
                response.headers.get("x-ms-token", "")
            ),
        }
    )


def analyze_response_progression(responses: list[dict[str, Any]]) -> None:
    """显示浏览器各页的新增 item 数，避免把重复请求误判为翻页。"""
    print_separator(f"捕获到 {len(responses)} 条 Explore 响应")
    seen_ids: set[str] = set()
    for index, summary in enumerate(responses, start=1):
        item_ids = set(summary["item_id_hashes"])
        new_item_count = len(item_ids - seen_ids)
        seen_ids.update(item_ids)
        print(
            f"[响应 {index}] pullType={summary['pull_type']}, "
            f"status={summary['http_status']}, "
            f"items={summary['item_count']}, new={new_item_count}, "
            f"cursor={'present' if summary['cursor_present'] else 'absent'}, "
            f"msToken={'present' if summary['ms_token_present'] else 'absent'}, "
            f"response_x_ms_token={summary['response_ms_token_hash']}"
        )


def extract_browser_request_params(queries: list[dict[str, str]]) -> dict[str, str]:
    """返回第一条包含所有固定浏览器字段的真实请求。"""
    for params in queries:
        extracted = {
            field: params[field]
            for field in BROWSER_REQUEST_FIELDS
            if params.get(field)
        }
        if len(extracted) == len(BROWSER_REQUEST_FIELDS):
            return extracted
    return {}


def extract_explore_templates(
    queries: list[dict[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]] | None:
    """保留浏览器首屏与带 msToken 翻页请求的原始 query 顺序。"""
    initial = next((query for query in queries if query.get("pullType") == "1"), None)
    next_page = next(
        (
            query
            for query in queries
            if query.get("pullType") == "2" and query.get("msToken")
        ),
        None,
    )
    if initial is None or next_page is None:
        return None
    return list(initial.items()), list(next_page.items())


def save_browser_request_params(
    settings_path: Path,
    browser_request_params: dict[str, str],
    templates: tuple[list[tuple[str, str]], list[tuple[str, str]]] | None = None,
) -> None:
    """将确认的浏览器字段写入本地、已忽略的会话设置。"""
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, dict):
        raise ValueError("settings 根节点必须是对象")
    browser_info = settings.get("browser_info_tiktok")
    if not isinstance(browser_info, dict):
        browser_info = {}
    browser_info.update(browser_request_params)
    if templates:
        for key, template in zip(EXPLORE_TEMPLATE_KEYS, templates, strict=True):
            browser_info[key] = [list(item) for item in template]
    settings["browser_info_tiktok"] = browser_info
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )


async def probe_ssr_sources(page) -> dict[str, dict[str, Any]]:
    """检查 SSR 嵌入对象中是否包含目标字段。"""
    return await page.evaluate(
        """
        (args) => {
            const targets = args.targets;
            const ssrKeys = args.ssrKeys;
            const result = {};
            for (const field of targets) {
                const sources = {};
                for (const key of ssrKeys) {
                    if (!window[key]) continue;
                    try {
                        const text = JSON.stringify(window[key]);
                        if (text.includes(field)) {
                            const regex = new RegExp(
                                '"' + field + '":\\s*"(.*?)"'
                            );
                            const match = text.match(regex);
                            sources[key] = match
                                ? 'found (len=' + match[1].length + ')'
                                : 'key_present';
                        }
                    } catch (e) {
                        sources[key] = 'error';
                    }
                }
                result[field] = sources;
            }
            return result;
        }
        """,
        {"targets": TARGET_FIELDS, "ssrKeys": SSR_KEYS},
    )


async def probe_storage(page) -> dict[str, list[dict[str, str]]]:
    """检查 localStorage / sessionStorage 中是否包含目标字段。"""
    return await page.evaluate(
        """
        () => {
            const out = {local: [], session: []};
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                const v = localStorage.getItem(k);
                out.local.push({key: k, value: v});
            }
            for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i);
                const v = sessionStorage.getItem(k);
                out.session.push({key: k, value: v});
            }
            return out;
        }
        """
    )


def match_storage(
    storage_data: dict[str, list[dict[str, str]]],
) -> dict[str, list[str]]:
    """从 storage 条目中筛选包含目标字段的记录。"""
    result: dict[str, list[str]] = {field: [] for field in TARGET_FIELDS}
    for field in TARGET_FIELDS:
        for item in storage_data.get("local", []):
            key = item.get("key") or ""
            value = item.get("value") or ""
            if field in key or field in value:
                result[field].append(
                    f"localStorage[{truncate(key, 16)}] len={len(value)}"
                )
        for item in storage_data.get("session", []):
            key = item.get("key") or ""
            value = item.get("value") or ""
            if field in key or field in value:
                result[field].append(
                    f"sessionStorage[{truncate(key, 16)}] len={len(value)}"
                )
    return result


async def probe_cookies(context) -> dict[str, list[str]]:
    """从浏览器 cookies 中筛选包含目标字段的记录。"""
    cookies = await context.cookies()
    result: dict[str, list[str]] = {}
    for field in TARGET_FIELDS + ("s_v_web_id",):
        matches: list[str] = []
        for cookie in cookies:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            if field in name or field in value:
                matches.append(f"cookie[{name}] len={len(value)}")
        if matches:
            result[field] = matches
    print(f"\nCookies 总数: {len(cookies)}")
    return result


async def diagnose(context, page, args: argparse.Namespace) -> None:
    """执行完整的字段来源诊断。"""
    queries: list[dict[str, str]] = []
    responses: list[dict[str, Any]] = []
    response_tasks: list[asyncio.Task[None]] = []
    page.on("request", lambda request: capture_explore_query(request, queries))
    page.on(
        "response",
        lambda response: response_tasks.append(
            asyncio.create_task(capture_explore_response(response, responses))
        ),
    )

    print(f"[诊断] 正在加载 {EXPLORE_URL} ...")
    await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(3)

    print(f"[诊断] 开始滚动 {args.scroll_count} 次以触发翻页请求 ...")
    for index in range(1, args.scroll_count + 1):
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        print(f"  滚动 {index}/{args.scroll_count}，等待 {args.scroll_delay}s")
        await asyncio.sleep(args.scroll_delay)

    if response_tasks:
        await asyncio.gather(*response_tasks)

    analyze_queries(queries)
    analyze_response_progression(responses)

    browser_request_params = extract_browser_request_params(queries)
    templates = extract_explore_templates(queries)
    print_separator("可复用浏览器请求字段")
    if not browser_request_params:
        print("未捕获完整字段集，未写入 settings.json。")
    else:
        for field, value in browser_request_params.items():
            print(f"{field}: len={len(value)}, sample={truncate(value, 8)}")
        if args.write_settings:
            save_browser_request_params(
                args.settings, browser_request_params, templates
            )
            template_status = "已保存" if templates else "未捕获完整翻页模板"
            print(
                f"已写入 {args.settings} 的 browser_info_tiktok；"
                f"首屏/翻页模板{template_status}。"
            )
        else:
            print("传入 --write-settings 可将这些字段写入本地 settings.json。")

    print_separator("页面数据源探测")
    ssr_present = await page.evaluate(
        """
        (ssrKeys) => {
            const out = {};
            for (const key of ssrKeys) {
                out[key] = !!window[key];
            }
            return out;
        }
        """,
        SSR_KEYS,
    )
    print(f"SSR 嵌入对象存在性: {ssr_present}")

    ssr_sources = await probe_ssr_sources(page)
    for field in TARGET_FIELDS:
        print(f"{field}: SSR={json.dumps(ssr_sources.get(field))}")

    storage_data = await probe_storage(page)
    storage_matches = match_storage(storage_data)
    for field in TARGET_FIELDS:
        matches = storage_matches.get(field, [])
        print(f"{field}: storage={matches if matches else 'none'}")

    cookie_matches = await probe_cookies(context)
    for field, matches in cookie_matches.items():
        print(f"{field}: {matches}")

    print_separator("诊断完成")
    print(
        "请重点查看 clientABVersions / odinId / verifyFp 的 'source=query' 行，"
        "并对比 SSR / storage / cookie 列是否有稳定来源。"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="诊断 TikTok Explore 关键字段在浏览器中的来源。",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="代理 URL，例如 http://127.0.0.1:7890。",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="有头运行（可见浏览器窗口）。",
    )
    parser.add_argument(
        "--scroll-count",
        type=int,
        default=3,
        help="加载页面后滚动的次数，用于触发翻页请求。",
    )
    parser.add_argument(
        "--scroll-delay",
        type=float,
        default=2.0,
        help="每次滚动后等待的秒数。",
    )
    parser.add_argument(
        "--skip-geo",
        action="store_true",
        help="跳过西班牙出口 IP 检查。",
    )
    parser.add_argument(
        "--use-settings-cookie",
        action="store_true",
        help="从 Volume/settings.json 读取 cookie，启动临时 context 诊断。",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help="cookie 设置文件路径（配合 --use-settings-cookie）。",
    )
    parser.add_argument(
        "--write-settings",
        action="store_true",
        help="将捕获的固定浏览器字段写入 settings.json。",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "[致命] 未安装 Playwright。请运行：\n"
            "  uv pip install playwright\n"
            "  uv run playwright install chromium"
        )
        return 2

    browser_kwargs: dict = {
        "headless": not args.headed,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    context_kwargs: dict = {
        "locale": "es-ES",
        "timezone_id": "Europe/Madrid",
        "viewport": {"width": 1536, "height": 864},
    }
    if args.proxy:
        proxy_config = {"server": args.proxy}
        browser_kwargs["proxy"] = proxy_config
        print(f"[初始化] proxy = {args.proxy}")
    else:
        print("[初始化] 警告: 未提供 --proxy。若出口非西班牙，内容池可能不是西语。")

    browser = None
    async with async_playwright() as p:
        if args.use_settings_cookie:
            if not args.settings.is_file():
                print(f"[致命] 未找到 settings 文件: {args.settings}")
                return 2
            try:
                cookie, user_agent = load_session(args.settings)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"[致命] 读取 settings 失败: {type(exc).__name__}: {exc}")
                return 2
            print(f"[初始化] 使用 settings.json cookie ({len(cookie)} 个)")
            print(f"[初始化] User-Agent = {truncate(user_agent, 50)}")
            browser = await p.chromium.launch(**browser_kwargs)
            context = await browser.new_context(**context_kwargs, user_agent=user_agent)
            await context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": ".tiktok.com",
                        "path": "/",
                    }
                    for name, value in cookie.items()
                ]
            )
            page = await context.new_page()
        else:
            if not PROFILE_DIR.exists():
                print(f"[致命] 未找到浏览器 profile: {PROFILE_DIR}")
                print(
                    "请先用 harvest 脚本登录并保存 profile，"
                    "或改用 --use-settings-cookie。"
                )
                return 2
            print(f"[初始化] profile = {PROFILE_DIR}")
            context = await p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                **browser_kwargs,
                **context_kwargs,
            )
            page = context.pages[0] if context.pages else await context.new_page()

        try:
            if not args.skip_geo:
                ok = await check_egress_country(page, "ES")
                if not ok:
                    print("[中止] 出口非西班牙，诊断结果可能无效。")
                    return 1
            await diagnose(context, page, args)
        finally:
            await context.close()
            if browser is not None:
                await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
