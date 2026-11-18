"""Interactive visible-browser QR login helper to save Douyin / Weibo cookies locally."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Callable, Literal

from patchright.async_api import async_playwright

from .config import load_config


def _has_douyin_login(cookies: list[dict]) -> bool:
    for c in cookies:
        if "douyin.com" in (c.get("domain") or "") and c.get("name") in {"sessionid", "sessionid_ss"} and c.get("value"):
            return True
    return False


def _has_weibo_login(cookies: list[dict], current_url: str) -> bool:
    if "passport.weibo.com" in current_url or "login.php" in current_url:
        return False
    for c in cookies:
        if "weibo.com" in (c.get("domain") or "") and c.get("name") == "SUB" and len(str(c.get("value") or "")) > 10:
            return True
    return False


async def login_and_save_cookie(
    platform: Literal["douyin", "weibo"],
    cookie_path: Path,
    timeout_sec: int = 180,
    log_fn: Callable[[str], None] = print,
) -> Path:
    """Open a visible Chromium window so the user can scan QR code and save storage_state."""
    target_url = "https://www.douyin.com/" if platform == "douyin" else "https://weibo.com/login.php"
    label = "抖音" if platform == "douyin" else "微博（查重小号）"
    cookie_path.parent.mkdir(parents=True, exist_ok=True)

    log_fn(f"正在打开可见浏览器窗口，请用手机扫码登录【{label}】...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1200, "height": 820})
        page = await context.new_page()
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

        logged_in = False
        deadline = asyncio.get_running_loop().time() + timeout_sec
        try:
            while asyncio.get_running_loop().time() < deadline:
                if page.is_closed():
                    break
                cookies = await context.cookies()
                if platform == "douyin":
                    ok = _has_douyin_login(cookies)
                else:
                    ok = _has_weibo_login(cookies, page.url or "")
                if ok:
                    await asyncio.sleep(2.5)
                    await context.storage_state(path=str(cookie_path))
                    logged_in = True
                    break
                await asyncio.sleep(1.0)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    if not logged_in:
        raise RuntimeError(f"未检测到【{label}】登录成功（超时或窗口已关闭），请重试。")

    log_fn(f"【{label}】登录成功！Cookie 已保存到：{cookie_path}")
    return cookie_path


def main() -> None:
    parser = argparse.ArgumentParser(description="扫码登录并保存抖音/微博 Cookie")
    parser.add_argument("platform", choices=["douyin", "weibo"], help="要登录的平台")
    parser.add_argument("--timeout", type=int, default=180, help="等待扫码超时秒数")
    args = parser.parse_args()

    cfg = load_config()
    dst = cfg.douyin_cookie_path if args.platform == "douyin" else cfg.weibo_cookie_path
    asyncio.run(login_and_save_cookie(args.platform, dst, timeout_sec=args.timeout, log_fn=print))


if __name__ == "__main__":
    main()
