#!/usr/bin/env python3
"""
Cloudflare Turnstile token 获取模块

部分站点在签到接口上启用了 Turnstile 校验（new-api 的 TurnstileCheck 中间件），
此时签到请求必须携带 `?turnstile=<token>` 查询参数，否则服务端直接返回
`{"success": false, "message": "Turnstile token 为空"}`。

服务端会把 token 提交到 Cloudflare siteverify 校验（附带请求方 IP），因此 token 必须：
  - 由目标站点的 sitekey 在该站点域名下生成
  - 与后续签到请求来自同一出口 IP（浏览器与签到请求都在同一台机器上运行，天然满足）
  - 尽快使用（Turnstile token 有效期约 5 分钟且只能使用一次）
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from camoufox.async_api import AsyncCamoufox
from curl_cffi import requests as curl_requests
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType

from utils.browser_utils import save_page_content_to_file, take_screenshot
from utils.http_utils import response_resolve

# Turnstile 校验失败时服务端返回的关键字（new-api / one-api TurnstileCheck 中间件）
TURNSTILE_ERROR_KEYWORDS = ("turnstile",)

# 在页面中显式渲染一个 Turnstile widget，并把 token 暴露到 window 上
# 使用官方推荐的 `render=explicit&onload=` 方式，确保 turnstile 全局对象已就绪
_RENDER_TURNSTILE_JS = """
(siteKey) => new Promise((resolve, reject) => {
    window.__tsToken = null;
    window.__tsError = null;

    const render = () => {
        try {
            const container = document.createElement('div');
            container.id = '__ts_container';
            // 必须可见且不被遮挡，交互式 widget 才能被点击
            container.style.position = 'fixed';
            container.style.top = '30px';
            container.style.left = '30px';
            container.style.zIndex = '2147483647';
            container.style.background = '#ffffff';
            container.style.padding = '8px';
            document.body.appendChild(container);

            window.turnstile.render(container, {
                sitekey: siteKey,
                callback: (token) => { window.__tsToken = token; },
                'error-callback': (err) => { window.__tsError = String(err || 'error-callback'); },
                'expired-callback': () => { window.__tsToken = null; },
            });
            resolve('rendered');
        } catch (e) {
            reject('render failed: ' + String(e));
        }
    };

    if (window.turnstile && window.turnstile.render) {
        render();
        return;
    }

    window.__tsOnload = render;

    const script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__tsOnload';
    script.async = true;
    script.defer = true;
    script.onerror = () => reject('failed to load turnstile api.js');
    document.head.appendChild(script);
})
"""


def is_turnstile_error(message: str | None) -> bool:
    """判断签到失败信息是否由 Turnstile 校验引起

    覆盖服务端两种返回：
      - "Turnstile token 为空"（未携带 token）
      - "Turnstile 校验失败，请刷新重试！"（token 无效/过期/已使用）
    """
    if not message:
        return False
    lowered = str(message).lower()
    return any(keyword in lowered for keyword in TURNSTILE_ERROR_KEYWORDS)


def append_turnstile_token(url: str, token: str) -> str:
    """把 turnstile token 作为查询参数附加到 URL 上

    保留 URL 上已有的查询参数（例如带签名的签到地址），并覆盖同名的 turnstile 参数。
    """
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "turnstile"]
    query.append(("turnstile", token))
    return urlunparse(parsed._replace(query=urlencode(query)))


def get_turnstile_site_key(
    session: curl_requests.Session,
    origin: str,
    account_name: str,
    headers: dict | None = None,
    status_path: str = "/api/status",
) -> str | None:
    """从站点 /api/status 接口读取 Turnstile sitekey

    new-api 的 /api/status 会返回 `turnstile_check`（是否启用）与 `turnstile_site_key`。

    Args:
        session: 已建立的 curl_cffi Session（复用已有 cookies，避免被 CF 拦截）
        origin: 站点 origin，例如 https://example.com
        account_name: 账号名称，用于日志输出
        headers: 请求头
        status_path: 状态接口路径

    Returns:
        str | None: sitekey，未启用或获取失败时返回 None
    """
    status_url = f"{origin}{status_path}"

    try:
        response = session.get(status_url, headers=headers or {}, timeout=30)
    except Exception as e:
        print(f"⚠️ {account_name}: Failed to request {status_path} for Turnstile sitekey: {e}")
        return None

    if response.status_code != 200:
        print(f"⚠️ {account_name}: Failed to get Turnstile sitekey: HTTP {response.status_code}")
        return None

    json_data = response_resolve(response, "get_turnstile_site_key", account_name)
    if json_data is None:
        print(f"⚠️ {account_name}: Invalid response format from {status_path}")
        return None

    data = json_data.get("data") or {}
    site_key = data.get("turnstile_site_key") or ""
    turnstile_check = data.get("turnstile_check", False)

    if not site_key:
        print(f"⚠️ {account_name}: Site does not expose turnstile_site_key in {status_path}")
        return None

    if not turnstile_check:
        # 站点声明未启用，但签到接口却要求 token，仍然返回 sitekey 继续尝试
        print(f"⚠️ {account_name}: turnstile_check is disabled but sitekey found, trying anyway")

    print(f"ℹ️ {account_name}: Turnstile sitekey: {site_key}")
    return site_key


async def get_turnstile_token(
    url: str,
    site_key: str,
    account_name: str,
    proxy_config: dict | None = None,
    timeout: int = 60000,
    poll_interval: int = 1000,
) -> str | None:
    """使用 Camoufox 在站点页面上生成一个 Turnstile token

    在目标站点的页面内注入并显式渲染 Turnstile widget，token 由 Cloudflare 直接下发，
    因此 token 的域名与 sitekey 均与站点匹配。

    大多数站点使用非交互（managed）模式，widget 会自动通过；若需要点击，
    则回退到 ClickSolver 点击 checkbox。

    Args:
        url: 站点页面地址（建议使用登录页，其本身已允许加载 Turnstile 脚本）
        site_key: Turnstile sitekey
        account_name: 账号名称，用于日志输出
        proxy_config: 代理配置，需与签到请求使用的代理保持一致（否则出口 IP 不匹配）
        timeout: 等待 token 的最长时间（毫秒）
        poll_interval: 轮询 token 的间隔（毫秒）

    Returns:
        str | None: Turnstile token，失败时返回 None
    """
    print(
        f"ℹ️ {account_name}: Starting browser to get Turnstile token from {url} "
        f"(using proxy: {'true' if proxy_config else 'false'})"
    )

    async with AsyncCamoufox(
        headless=False,
        humanize=True,
        locale="en-US",
        geoip=True if proxy_config else False,
        proxy=proxy_config,
        os="macos",  # 与其他浏览器流程保持一致，避免跨平台指纹不一致
        config={
            "forceScopeAccess": True,
        },
    ) as browser:
        page = await browser.new_page()

        try:
            async with ClickSolver(
                framework=FrameworkType.CAMOUFOX,
                page=page,
                max_attempts=3,
                attempt_delay=3,
            ) as solver:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)

                # 站点本身可能有 Cloudflare 拦截页，先过掉再注入 widget
                page_title = await page.title()
                page_content = await page.content()
                if "Just a moment" in page_title or "Checking your browser" in page_content:
                    print(f"ℹ️ {account_name}: Cloudflare challenge detected, auto-solving...")
                    try:
                        await solver.solve_captcha(
                            captcha_container=page,
                            captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                        )
                        print(f"✅ {account_name}: Cloudflare challenge auto-solved")
                        await page.wait_for_timeout(5000)
                    except Exception as solve_err:
                        print(f"⚠️ {account_name}: Failed to solve Cloudflare challenge: {solve_err}")

                # 注入并渲染 Turnstile widget
                try:
                    await page.evaluate(_RENDER_TURNSTILE_JS, site_key)
                    print(f"ℹ️ {account_name}: Turnstile widget rendered, waiting for token")
                except Exception as e:
                    print(f"❌ {account_name}: Failed to render Turnstile widget: {e}")
                    await save_page_content_to_file(page, "turnstile_render_failed", account_name)
                    return None

                token = await _wait_for_token(page, account_name, timeout, poll_interval)

                # 非交互模式未自动通过时，尝试点击 checkbox 后继续等待
                if not token:
                    print(f"ℹ️ {account_name}: Token not issued automatically, trying to click the widget")
                    try:
                        await solver.solve_captcha(
                            captcha_container=page,
                            captcha_type=CaptchaType.CLOUDFLARE_TURNSTILE,
                        )
                        print(f"✅ {account_name}: Turnstile widget clicked")
                    except Exception as solve_err:
                        print(f"⚠️ {account_name}: Failed to click Turnstile widget: {solve_err}")

                    token = await _wait_for_token(page, account_name, timeout, poll_interval)

                if not token:
                    error = await page.evaluate("() => window.__tsError")
                    print(f"❌ {account_name}: Failed to get Turnstile token" + (f" ({error})" if error else ""))
                    await take_screenshot(page, "turnstile_failed", account_name)
                    await save_page_content_to_file(page, "turnstile_failed", account_name)
                    return None

                print(f"✅ {account_name}: Got Turnstile token: {token[:30]}...")
                return token

        except Exception as e:
            print(f"❌ {account_name}: Error getting Turnstile token: {e}")
            return None

        finally:
            await page.close()


async def _wait_for_token(page, account_name: str, timeout: int, poll_interval: int) -> str | None:
    """轮询等待 Turnstile widget 下发 token"""
    elapsed = 0

    while elapsed < timeout:
        token = await page.evaluate("() => window.__tsToken")
        if token:
            return token

        error = await page.evaluate("() => window.__tsError")
        if error:
            print(f"⚠️ {account_name}: Turnstile error callback: {error}")
            return None

        await page.wait_for_timeout(poll_interval)
        elapsed += poll_interval

    return None
