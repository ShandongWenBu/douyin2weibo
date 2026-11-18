"""Fetch Douyin video metadata, watermark-free video URLs, and covers via Patchright."""

from __future__ import annotations

import asyncio
import datetime
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from patchright.async_api import async_playwright

from .config import extract_title_and_hashtags

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_CAPTCHA_KEYWORDS = ("验证码", "安全验证", "拖动滑块", "请完成验证")


class CaptchaRequiredError(RuntimeError):
    """Raised when Douyin presents a CAPTCHA or verification gate."""


@dataclass
class DouyinVideo:
    aweme_id: str
    share_url: str
    video_page_url: str
    author: str
    sec_uid: str
    desc: str
    title: str
    hashtags: list[str]
    create_time: int
    publish_time_str: str
    duration_sec: float
    width: int
    height: int
    video_urls: list[str] = field(default_factory=list)
    cover_urls: list[str] = field(default_factory=list)
    is_top: bool = False


def resolve_redirect_url(url: str, timeout: int = 12) -> str:
    """Follow HTTP redirects (e.g. v.douyin.com short links) to the canonical URL."""
    req = urllib.request.Request(url, headers={"User-Agent": _DESKTOP_UA})

    class _LastRedirectHandler(urllib.request.HTTPRedirectHandler):
        def __init__(self) -> None:
            self.last_url = url

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            self.last_url = newurl
            # Stop once we reach iesdouyin/douyin canonical path so we don't stall on heavy pages
            if "/share/user/" in newurl or "/share/video/" in newurl or "/video/" in newurl or "/user/" in newurl:
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    handler = _LastRedirectHandler()
    opener = urllib.request.build_opener(handler)
    try:
        opener.open(req, timeout=timeout)
    except urllib.error.HTTPError:
        pass
    except Exception:
        return handler.last_url
    return handler.last_url


def classify_douyin_url(url: str) -> tuple[str, str]:
    """Return ('user', sec_uid) or ('video', aweme_id) after resolving short links."""
    resolved = resolve_redirect_url(url)
    m_user = re.search(r"/(?:share/)?user/([A-Za-z0-9_\-]+)", resolved)
    if m_user:
        return "user", m_user.group(1)

    m_sec = re.search(r"[?&]sec_uid=([A-Za-z0-9_\-]+)", resolved)
    if m_sec:
        return "user", m_sec.group(1)

    m_vid = re.search(r"/(?:share/)?video/(\d+)", resolved)
    if m_vid:
        return "video", m_vid.group(1)

    m_modal = re.search(r"[?&]modal_id=(\d+)", resolved)
    if m_modal:
        return "video", m_modal.group(1)

    raise ValueError(f"无法识别的抖音链接（解析后：{resolved}）")


def _pick_best_streams(video_obj: dict[str, Any]) -> tuple[int, int, list[str]]:
    """Select highest-resolution watermark-free MP4 stream URLs."""
    bit_rates = video_obj.get("bit_rate") or []
    if isinstance(bit_rates, list) and bit_rates:
        ranked = sorted(
            bit_rates,
            key=lambda b: (
                ((b.get("play_addr") or {}).get("width") or 0)
                * ((b.get("play_addr") or {}).get("height") or 0),
                1 if str(b.get("format", "")).lower() == "mp4" else 0,
                b.get("bit_rate") or 0,
            ),
            reverse=True,
        )
        urls: list[str] = []
        best_w = 0
        best_h = 0
        for item in ranked:
            pa = item.get("play_addr") or {}
            if not best_w:
                best_w = int(pa.get("width") or 0)
                best_h = int(pa.get("height") or 0)
            for u in pa.get("url_list") or []:
                if u and u not in urls:
                    urls.append(u)
        if urls:
            return best_w, best_h, urls

    pa = video_obj.get("play_addr") or {}
    fallback_urls = [
        u.replace("/playwm/", "/play/")
        for u in (pa.get("url_list") or [])
        if u
    ]
    return int(pa.get("width") or 0), int(pa.get("height") or 0), fallback_urls


def _parse_aweme(aweme: dict[str, Any], share_url: str, fallback_sec_uid: str = "") -> DouyinVideo:
    aweme_id = str(aweme.get("aweme_id") or "")
    desc = (aweme.get("desc") or "").strip()
    title, hashtags, full_desc = extract_title_and_hashtags(desc)
    author_info = aweme.get("author") or {}
    author = (author_info.get("nickname") or "").strip()
    sec_uid = (author_info.get("sec_uid") or fallback_sec_uid or "").strip()
    create_ts = int(aweme.get("create_time") or 0)
    pub_str = (
        datetime.datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d %H:%M:%S")
        if create_ts
        else ""
    )
    video_obj = aweme.get("video") or {}
    dur_ms = int(video_obj.get("duration") or 0)
    width, height, video_urls = _pick_best_streams(video_obj)

    cover_urls: list[str] = []
    for key in ("origin_cover", "cover", "dynamic_cover"):
        for u in ((video_obj.get(key) or {}).get("url_list") or []):
            if u and u not in cover_urls:
                cover_urls.append(u)

    return DouyinVideo(
        aweme_id=aweme_id,
        share_url=share_url,
        video_page_url=f"https://www.douyin.com/video/{aweme_id}",
        author=author,
        sec_uid=sec_uid,
        desc=full_desc,
        title=title,
        hashtags=hashtags,
        create_time=create_ts,
        publish_time_str=pub_str,
        duration_sec=round(dur_ms / 1000.0, 2),
        width=width,
        height=height,
        video_urls=video_urls,
        cover_urls=cover_urls,
        is_top=bool(aweme.get("is_top")),
    )


async def _check_captcha(page) -> None:
    try:
        title = await page.title()
        snippet = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 600)")
    except Exception:
        return
    combined = f"{title}\n{snippet}"
    if any(k in combined for k in _CAPTCHA_KEYWORDS):
        raise CaptchaRequiredError("抖音触发了验证码或安全验证，已立即停止；请人工在浏览器完成验证后再重试。")


async def _fetch_profile_posts(context, sec_uid: str, share_url: str, limit: int) -> list[DouyinVideo]:
    last_err: Exception | None = None
    for attempt in range(2):
        page = await context.new_page()
        got_event = asyncio.Event()
        raw_posts: list[dict[str, Any]] = []

        async def on_response(resp) -> None:
            if "/aweme/v1/web/aweme/post/" in resp.url:
                try:
                    data = await resp.json()
                    items = data.get("aweme_list") or []
                    if items:
                        raw_posts.extend(items)
                    got_event.set()
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            await page.goto(f"https://www.douyin.com/user/{sec_uid}", wait_until="commit", timeout=30000)
            await asyncio.wait_for(got_event.wait(), timeout=20)
            await page.wait_for_timeout(600)
            await _check_captcha(page)
            if not raw_posts:
                raise RuntimeError(f"主页未返回任何作品列表（sec_uid={sec_uid}）")
            # Deduplicate by aweme_id, prefer non-pinned latest videos
            by_id: dict[str, dict[str, Any]] = {}
            for item in raw_posts:
                aid = str(item.get("aweme_id") or "")
                if aid:
                    by_id[aid] = item
            all_items = list(by_id.values())
            non_pinned = [x for x in all_items if not x.get("is_top")]
            pool = non_pinned if non_pinned else all_items
            pool.sort(key=lambda x: int(x.get("create_time") or 0), reverse=True)
            return [_parse_aweme(x, share_url, fallback_sec_uid=sec_uid) for x in pool[:limit]]
        except CaptchaRequiredError:
            raise
        except Exception as exc:
            last_err = exc
            if attempt == 0:
                await asyncio.sleep(1.5)
        finally:
            await page.close()

    raise RuntimeError(f"获取抖音主页作品失败：{last_err}")


async def _fetch_single_video(context, aweme_id: str, share_url: str) -> list[DouyinVideo]:
    last_err: Exception | None = None
    for attempt in range(2):
        page = await context.new_page()
        got_event = asyncio.Event()
        detail_box: list[dict[str, Any]] = []

        async def on_response(resp) -> None:
            if "/aweme/v1/web/aweme/detail/" in resp.url:
                try:
                    data = await resp.json()
                    detail = data.get("aweme_detail")
                    if isinstance(detail, dict) and detail.get("aweme_id"):
                        detail_box.append(detail)
                        got_event.set()
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            await page.goto(f"https://www.douyin.com/video/{aweme_id}", wait_until="commit", timeout=30000)
            await asyncio.wait_for(got_event.wait(), timeout=20)
            await page.wait_for_timeout(400)
            await _check_captcha(page)
            if not detail_box:
                raise RuntimeError(f"未拦截到作品详情（aweme_id={aweme_id}）")
            return [_parse_aweme(detail_box[0], share_url)]
        except CaptchaRequiredError:
            raise
        except Exception as exc:
            last_err = exc
            if attempt == 0:
                await asyncio.sleep(1.5)
        finally:
            await page.close()

    raise RuntimeError(f"获取抖音单个作品失败：{last_err}")


async def fetch_douyin_batch(
    urls: list[str],
    cookie_path: Path,
    latest_per_profile: int = 1,
    headless: bool = True,
) -> dict[str, tuple[list[DouyinVideo], str | None]]:
    """Fetch Douyin metadata & stream URLs for a list of profile or video URLs in one browser session."""
    if not cookie_path.is_file():
        raise FileNotFoundError(f"未找到抖音 cookie 文件：{cookie_path}")

    results: dict[str, tuple[list[DouyinVideo], str | None]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=str(cookie_path),
            viewport={"width": 1280, "height": 800},
        )
        try:
            for url in urls:
                if url in results:
                    continue
                try:
                    kind, ident = classify_douyin_url(url)
                    if kind == "user":
                        videos = await _fetch_profile_posts(context, ident, url, latest_per_profile)
                    else:
                        videos = await _fetch_single_video(context, ident, url)
                    results[url] = (videos, None)
                except CaptchaRequiredError:
                    raise
                except Exception as exc:
                    results[url] = ([], str(exc))
        finally:
            await browser.close()
    return results


def download_first_working_url(urls: list[str], dest_path: Path, timeout: int = 120) -> Path:
    """Download binary asset (mp4 or cover image) trying fallback CDN URLs in order."""
    if not urls:
        raise RuntimeError(f"没有可用的下载地址：{dest_path.name}")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": _DESKTOP_UA,
        "Referer": "https://www.douyin.com/",
    }
    last_err: Exception | None = None
    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
        for u in urls:
            try:
                with client.stream("GET", u) as resp:
                    resp.raise_for_status()
                    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
                    with tmp_path.open("wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                            if chunk:
                                f.write(chunk)
                    if tmp_path.stat().st_size == 0:
                        raise RuntimeError("下载文件为空")
                    tmp_path.replace(dest_path)
                    return dest_path
            except Exception as exc:
                last_err = exc
    raise RuntimeError(f"下载 {dest_path.name} 失败：{last_err}")
