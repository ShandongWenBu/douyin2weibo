"""Weibo duplicate checker with local ledger, own-timeline check, and rate-limited s.weibo.com search."""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import json
import random
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from patchright.async_api import async_playwright

from .config import DedupConfig
from .douyin_fetcher import CaptchaRequiredError, DouyinVideo

_TAG_HTML_RE = re.compile(r"<[^>]+>")
_WEIBO_NOISE_RE = re.compile(
    r"(?:#[^#\n]+#|\[超话\]|http[s]?://\S+|L\S+?的微博视频|O网页链接|收起d|\u200b)"
)
_PUNCT_RE = re.compile(r"[\s\|｜·—\-_，。！？、：；“”\"'‘’（）()【】\[\]《》<>.,!?:;~`@#￥$%^&*+=/\\]+")
_CAPTCHA_TEXTS = ("安全验证", "验证码", "访问频次过高", "行为异常", "请先登录", "扫码登录")


@dataclass
class DedupEvidence:
    source: str  # ledger | own_timeline | weibo_search
    author: str
    author_uid: str
    is_own_account: bool
    time_text: str
    post_url: str
    snippet: str
    similarity: float
    duration_diff_sec: float | None = None


@dataclass
class DedupResult:
    status: str  # green | yellow | red | manual | off
    label: str   # 未发现 | 疑似重复 | 已发过 | 待人工复核 | 未查重
    summary: str
    is_own_published: bool = False
    evidences: list[DedupEvidence] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    manual_search_urls: list[dict[str, str]] = field(default_factory=list)
    quota_exceeded: bool = False


def _normalize_text(text: str) -> str:
    cleaned = _TAG_HTML_RE.sub(" ", text or "")
    cleaned = _WEIBO_NOISE_RE.sub(" ", cleaned)
    return _PUNCT_RE.sub("", cleaned).lower()


def _char_bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _longest_common_substring_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    dp = [0] * (len(b) + 1)
    best = 0
    for ch_a in a:
        prev = 0
        for j, ch_b in enumerate(b, start=1):
            cur = dp[j]
            if ch_a == ch_b:
                dp[j] = prev + 1
                if dp[j] > best:
                    best = dp[j]
            else:
                dp[j] = 0
            prev = cur
    return best


def compute_text_similarity(dy_title: str, wb_text: str) -> float:
    """Compute similarity in [0, 1] combining substring containment and bigram overlap."""
    a = _normalize_text(dy_title)
    b = _normalize_text(wb_text)
    if not a or not b:
        return 0.0
    if a in b:
        return 1.0
    lcs = _longest_common_substring_len(a, b)
    lcs_ratio = lcs / max(1, min(len(a), len(b)))
    bg_a = _char_bigrams(a)
    bg_b = _char_bigrams(b)
    jaccard = len(bg_a & bg_b) / max(1, len(bg_a | bg_b))
    return round(max(lcs_ratio * 0.85, jaccard), 3)


def build_search_queries(video: DouyinVideo, max_queries: int = 2) -> list[str]:
    """Generate concise search phrases without hashtags/punctuation noise."""
    raw = re.sub(r"#[^\s#]+", "", video.title or video.desc or "").strip()
    raw = re.sub(r"@[A-Za-z0-9_\-\u4e00-\u9fff]+", "", raw).strip()
    clauses = [c.strip() for c in re.split(r"[|｜，。！？!?,;；\n]+", raw) if len(c.strip()) >= 4]

    candidates: list[str] = []
    # Prefer the most distinctive long clause (skip generic "第一集"/"第二集")
    distinctive = [c for c in clauses if not re.fullmatch(r"第[一二三四五六七八九十\d]+[集期话季]", c)]
    if distinctive:
        best_clause = max(distinctive, key=len)
        candidates.append(best_clause[:26])
    if clauses:
        joined = "".join(clauses)[:26]
        if joined and joined not in candidates:
            candidates.append(joined)
    if not candidates and raw:
        candidates.append(raw[:24])

    return candidates[:max(1, max_queries)]


def compute_time_window(video: DouyinVideo, cfg: DedupConfig) -> tuple[str, str, int]:
    """Return (start_date_str, end_date_str, start_epoch_sec) for search filtering."""
    now = datetime.datetime.now()
    today_date = now.date()
    candidates: list[datetime.date] = []

    if video.create_time:
        dy_dt = datetime.datetime.fromtimestamp(video.create_time)
        candidates.append(dy_dt.date())

    if cfg.lookback_days > 0:
        candidates.append((now - datetime.timedelta(days=cfg.lookback_days)).date())

    if cfg.min_date:
        try:
            candidates.append(datetime.date.fromisoformat(cfg.min_date))
        except ValueError:
            pass

    start_date = max(candidates) if candidates else (today_date - datetime.timedelta(days=30))
    if start_date > today_date:
        start_date = today_date

    start_dt = datetime.datetime.combine(start_date, datetime.time.min)
    if video.create_time:
        start_ts = max(int(start_dt.timestamp()), video.create_time - 3600)
    else:
        start_ts = int(start_dt.timestamp())

    return start_date.isoformat(), today_date.isoformat(), start_ts


def _build_timescope_param(start_date: str, end_date: str) -> str:
    return urllib.parse.quote(f"custom:{start_date}-0:{end_date}-23")


def make_manual_search_urls(queries: list[str], start_date: str = "", end_date: str = "") -> list[dict[str, str]]:
    urls: list[dict[str, str]] = []
    ts_suffix = f"&timescope={_build_timescope_param(start_date, end_date)}&Refer=g" if start_date and end_date else ""
    for q in queries:
        enc = urllib.parse.quote(q)
        urls.append({
            "query": q,
            "video_url": f"https://s.weibo.com/video?q={enc}{ts_suffix}",
            "all_url": f"https://s.weibo.com/weibo?q={enc}{ts_suffix}",
        })
    return urls


def _parse_api_created_at(created_at: str) -> int | None:
    if not created_at:
        return None
    try:
        return int(email.utils.parsedate_to_datetime(created_at).timestamp())
    except Exception:
        return None


def _parse_weibo_time_to_ts(time_text: str) -> int | None:
    """Best-effort parse of s.weibo.com relative/short timestamps into epoch seconds."""
    now = datetime.datetime.now()
    t = (time_text or "").strip()
    if not t:
        return None
    if "刚刚" in t:
        return int(now.timestamp())
    m = re.search(r"(\d+)\s*分钟前", t)
    if m:
        return int((now - datetime.timedelta(minutes=int(m.group(1)))).timestamp())
    m = re.search(r"(\d+)\s*小时前", t)
    if m:
        return int((now - datetime.timedelta(hours=int(m.group(1)))).timestamp())
    m = re.search(r"今天\s*(\d{1,2}):(\d{2})", t)
    if m:
        dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return int(dt.timestamp())
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", t)
    if m:
        dt = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
        return int(dt.timestamp())
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", t)
    if m:
        dt = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
        return int(dt.timestamp())
    m = re.search(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", t)
    if m:
        dt = datetime.datetime(now.year, int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        return int(dt.timestamp())
    return None


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json_dict(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _check_weibo_captcha(page) -> None:
    url = page.url or ""
    if "passport.weibo.com" in url or "login.sina.com.cn" in url:
        raise CaptchaRequiredError("微博 cookie 已失效并跳转到登录页，请更新微博 cookie 后重试。")
    try:
        title = await page.title()
        snippet = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 600)")
    except Exception:
        return
    combined = f"{title}\n{snippet}"
    if any(k in combined for k in _CAPTCHA_TEXTS):
        raise CaptchaRequiredError(
            "微博触发安全验证/验证码或频率限制，已立即停止查重，请人工在浏览器确认或稍后再试。"
        )


async def _sleep_interruptible(seconds: float, stop_event: threading.Event | None) -> bool:
    """Sleep up to `seconds`, waking within 0.25s if stop_event is set. Returns True if stopped."""
    remaining = max(0.0, seconds)
    while remaining > 0:
        if stop_event and stop_event.is_set():
            return True
        step = min(0.25, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return bool(stop_event and stop_event.is_set())


async def run_weibo_dedup(
    items: list[tuple[DouyinVideo, str, str]],  # (video, target_weibo_name, target_weibo_uid)
    all_known_own_uids: set[str],
    weibo_cookie_path: Path,
    ledger_path: Path,
    state_path: Path,
    cfg: DedupConfig,
    stop_event: threading.Event | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, DedupResult]:
    """Check duplicates for each DouyinVideo (keyed by aweme_id)."""
    ledger = _load_json_dict(ledger_path)
    today = datetime.date.today().isoformat()
    state = _load_json_dict(state_path)
    if state.get("date") != today:
        state = {"date": today, "search_count": 0, "cache": {}}

    results: dict[str, DedupResult] = {}

    if cfg.mode == "off":
        for video, _, _ in items:
            queries = build_search_queries(video, cfg.max_queries_per_video)
            start_d, end_d, _ = compute_time_window(video, cfg)
            results[video.aweme_id] = DedupResult(
                status="off",
                label="未查重",
                summary="配置已关闭查重（mode=off）",
                search_queries=queries,
                manual_search_urls=make_manual_search_urls(queries, start_d, end_d),
            )
        return results

    if cfg.mode == "manual":
        for video, _, _ in items:
            queries = build_search_queries(video, cfg.max_queries_per_video)
            start_d, end_d, _ = compute_time_window(video, cfg)
            results[video.aweme_id] = DedupResult(
                status="manual",
                label="待人工复核",
                summary=f"手动查重模式（时间窗 {start_d} ~ {end_d}）：请点击下方微博搜索链接确认",
                search_queries=queries,
                manual_search_urls=make_manual_search_urls(queries, start_d, end_d),
            )
        return results

    # Layer 1: Check local ledger first (zero network)
    pending: list[tuple[DouyinVideo, str, str]] = []
    for video, wb_name, wb_uid in items:
        queries = build_search_queries(video, cfg.max_queries_per_video)
        start_d, end_d, _ = compute_time_window(video, cfg)
        manual_urls = make_manual_search_urls(queries, start_d, end_d)
        entry = (ledger.get("videos") or {}).get(video.aweme_id)
        if isinstance(entry, dict) and entry.get("published"):
            results[video.aweme_id] = DedupResult(
                status="red",
                label="已发过",
                summary=f"本地台账已记录为已发布（微博号：{entry.get('weibo_name', wb_name)}）",
                is_own_published=True,
                search_queries=queries,
                manual_search_urls=manual_urls,
            )
        else:
            pending.append((video, wb_name, wb_uid))

    if not pending:
        return results

    if stop_event and stop_event.is_set():
        for video, _, _ in pending:
            queries = build_search_queries(video, cfg.max_queries_per_video)
            start_d, end_d, _ = compute_time_window(video, cfg)
            results[video.aweme_id] = DedupResult(
                status="manual",
                label="已跳过查重",
                summary="已手动停止查重，直接进入下载处理（可点下方链接人工复核）",
                search_queries=queries,
                manual_search_urls=make_manual_search_urls(queries, start_d, end_d),
            )
        return results

    if not weibo_cookie_path.is_file():
        for video, _, _ in pending:
            queries = build_search_queries(video, cfg.max_queries_per_video)
            start_d, end_d, _ = compute_time_window(video, cfg)
            results[video.aweme_id] = DedupResult(
                status="manual",
                label="待人工复核",
                summary=f"未找到微博 cookie（{weibo_cookie_path.name}），已转为手动查重链接",
                search_queries=queries,
                manual_search_urls=make_manual_search_urls(queries, start_d, end_d),
            )
        return results

    own_timelines: dict[str, list[dict[str, Any]]] = {}
    search_performed_this_run = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        context = await browser.new_context(
            storage_state=str(weibo_cookie_path),
            viewport={"width": 1280, "height": 900},
        )
        try:
            # Layer 2: Fetch own target Weibo timelines once per unique UID
            uids_to_fetch = sorted({uid for _, _, uid in pending if uid} | (all_known_own_uids if cfg.check_own_timelines else set()))
            if cfg.check_own_timelines and uids_to_fetch:
                wb_page = await context.new_page()
                await wb_page.goto("https://weibo.com", wait_until="domcontentloaded", timeout=30000)
                await _check_weibo_captcha(wb_page)
                for uid in uids_to_fetch:
                    try:
                        resp_json = await wb_page.evaluate(
                            """async (uid) => {
                                const r = await fetch(`/ajax/statuses/mymblog?uid=${uid}&page=1&feature=0`, {credentials: 'include'});
                                return await r.json();
                            }""",
                            uid,
                        )
                        own_timelines[uid] = (resp_json.get("data") or {}).get("list") or []
                    except Exception:
                        own_timelines[uid] = []
                await wb_page.close()

            search_page = await context.new_page() if cfg.enable_global_search else None

            for video, wb_name, wb_uid in pending:
                queries = build_search_queries(video, cfg.max_queries_per_video)
                start_d, end_d, start_ts = compute_time_window(video, cfg)
                manual_urls = make_manual_search_urls(queries, start_d, end_d)
                evidences: list[DedupEvidence] = []
                red_hit: DedupEvidence | None = None

                if stop_event and stop_event.is_set():
                    results[video.aweme_id] = DedupResult(
                        status="manual",
                        label="已跳过查重",
                        summary="已手动停止查重，直接进入下载处理（可点下方链接人工复核）",
                        search_queries=queries,
                        manual_search_urls=manual_urls,
                    )
                    continue

                # Evaluate Layer 2 own timelines (filtered by start_ts)
                for uid, posts in own_timelines.items():
                    for post in posts:
                        post_ts = _parse_api_created_at(str(post.get("created_at") or ""))
                        if post_ts is not None and post_ts < start_ts:
                            continue
                        raw_txt = post.get("text_raw") or post.get("text") or ""
                        sim = compute_text_similarity(video.title, raw_txt)
                        wb_dur = ((post.get("page_info") or {}).get("media_info") or {}).get("duration")
                        dur_diff = abs(float(wb_dur) - video.duration_sec) if wb_dur else None
                        if sim >= 0.45 or (dur_diff is not None and dur_diff <= 2.0 and sim >= 0.25):
                            mblogid = post.get("mblogid") or post.get("idstr") or ""
                            author = (post.get("user") or {}).get("screen_name") or (wb_name if uid == wb_uid else uid)
                            ev = DedupEvidence(
                                source="own_timeline",
                                author=author,
                                author_uid=uid,
                                is_own_account=True,
                                time_text=str(post.get("created_at") or ""),
                                post_url=f"https://weibo.com/{uid}/{mblogid}" if mblogid else f"https://weibo.com/u/{uid}",
                                snippet=_TAG_HTML_RE.sub("", raw_txt)[:120],
                                similarity=sim,
                                duration_diff_sec=round(dur_diff, 1) if dur_diff is not None else None,
                            )
                            evidences.append(ev)
                            if sim >= 0.65 or (dur_diff is not None and dur_diff <= 2.0 and sim >= 0.35):
                                red_hit = ev
                                break
                    if red_hit:
                        break

                if red_hit:
                    results[video.aweme_id] = DedupResult(
                        status="red",
                        label="已发过",
                        summary=f"自家微博号「{red_hit.author}」近期已发布（相似度 {red_hit.similarity:.0%}）",
                        is_own_published=True,
                        evidences=evidences,
                        search_queries=queries,
                        manual_search_urls=manual_urls,
                    )
                    continue

                if not cfg.enable_global_search:
                    results[video.aweme_id] = DedupResult(
                        status="green",
                        label="未发现",
                        summary=f"自家微博主页（{start_d} ~ {end_d}）未发现重复；全站搜索已关闭，可点下方链接按需复核",
                        evidences=evidences[:5],
                        search_queries=queries,
                        manual_search_urls=manual_urls,
                    )
                    continue

                # Layer 3: Full-site s.weibo.com search with time-range filter, safety quota & delay
                quota_hit = False
                stopped_by_user = False
                ts_param = _build_timescope_param(start_d, end_d)
                for q in queries:
                    if stop_event and stop_event.is_set():
                        stopped_by_user = True
                        break
                    cache_key = f"video:{start_d}:{end_d}:{q}"
                    cards: list[dict[str, Any]] | None = None
                    if cfg.cache_same_day and cache_key in (state.get("cache") or {}):
                        cards = state["cache"][cache_key]
                    else:
                        if int(state.get("search_count", 0)) >= cfg.daily_search_limit:
                            quota_hit = True
                            break
                        if search_performed_this_run > 0:
                            delay = random.uniform(cfg.min_delay_sec, cfg.max_delay_sec)
                            if log_fn:
                                log_fn(f"  [查重] 安全等待 {delay:.1f}s 后继续搜索（可点「停止查重」直接开始下载）...")
                            if await _sleep_interruptible(delay, stop_event):
                                stopped_by_user = True
                                break

                        if stop_event and stop_event.is_set():
                            stopped_by_user = True
                            break
                        search_url = (
                            f"https://s.weibo.com/video?q={urllib.parse.quote(q)}"
                            f"&timescope={ts_param}&Refer=g"
                        )
                        await search_page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                        if await _sleep_interruptible(1.8, stop_event):
                            stopped_by_user = True
                            break
                        await _check_weibo_captcha(search_page)
                        search_performed_this_run += 1
                        state["search_count"] = int(state.get("search_count", 0)) + 1

                        cards = await search_page.evaluate("""() => {
                            const items = Array.from(document.querySelectorAll('div.card-wrap[action-type="feed_list_item"]'));
                            return items.slice(0, 8).map(el => {
                                const nickEl = el.querySelector('a.name');
                                const txtEl = el.querySelector('p.txt[node-type="feed_list_content_full"]') || el.querySelector('p.txt[node-type="feed_list_content"]');
                                const fromEl = el.querySelector('.from a');
                                const aHref = nickEl ? (nickEl.getAttribute('href') || '') : '';
                                const pHref = fromEl ? (fromEl.getAttribute('href') || '') : '';
                                return {
                                    author: nickEl ? nickEl.innerText.trim() : '',
                                    author_href: aHref,
                                    text: txtEl ? txtEl.innerText.trim() : '',
                                    time_text: fromEl ? fromEl.innerText.trim() : '',
                                    post_href: pHref,
                                };
                            });
                        }""")
                        if cfg.cache_same_day:
                            state.setdefault("cache", {})[cache_key] = cards
                        _save_json_dict(state_path, state)

                    for c in cards or []:
                        sim = compute_text_similarity(video.title, c.get("text") or "")
                        if sim < 0.45:
                            continue
                        wb_ts = _parse_weibo_time_to_ts(c.get("time_text") or "")
                        if wb_ts and wb_ts < start_ts:
                            continue
                        m_uid = re.search(r"weibo\.com/(?:u/)?(\d+)", c.get("author_href") or "")
                        c_uid = m_uid.group(1) if m_uid else ""
                        is_own = bool(c_uid and c_uid in all_known_own_uids)
                        p_href = c.get("post_href") or ""
                        if p_href.startswith("//"):
                            p_href = "https:" + p_href
                        ev = DedupEvidence(
                            source="weibo_search",
                            author=c.get("author") or "",
                            author_uid=c_uid,
                            is_own_account=is_own,
                            time_text=c.get("time_text") or "",
                            post_url=p_href,
                            snippet=(c.get("text") or "")[:120],
                            similarity=sim,
                        )
                        evidences.append(ev)
                        if sim >= 0.72:
                            red_hit = ev
                            break

                    if red_hit or quota_hit:
                        break

                evidences.sort(key=lambda e: e.similarity, reverse=True)
                if red_hit:
                    who = "自家号" if red_hit.is_own_account else "外部账号"
                    results[video.aweme_id] = DedupResult(
                        status="red",
                        label="已发过",
                        summary=f"全站搜索命中（{who}：{red_hit.author}，相似度 {red_hit.similarity:.0%}）",
                        is_own_published=red_hit.is_own_account,
                        evidences=evidences[:5],
                        search_queries=queries,
                        manual_search_urls=manual_urls,
                        quota_exceeded=quota_hit,
                    )
                elif evidences:
                    top = evidences[0]
                    results[video.aweme_id] = DedupResult(
                        status="yellow",
                        label="疑似重复",
                        summary=f"发现相似微博（{top.author}，相似度 {top.similarity:.0%}），请人工点开链接核实",
                        evidences=evidences[:5],
                        search_queries=queries,
                        manual_search_urls=manual_urls,
                        quota_exceeded=quota_hit,
                    )
                elif stopped_by_user:
                    results[video.aweme_id] = DedupResult(
                        status="manual",
                        label="已跳过查重",
                        summary="已手动停止查重，直接进入下载处理（可点下方链接人工复核）",
                        search_queries=queries,
                        manual_search_urls=manual_urls,
                    )
                elif quota_hit:
                    results[video.aweme_id] = DedupResult(
                        status="manual",
                        label="待人工复核",
                        summary=f"已达今日全站搜索安全上限（{cfg.daily_search_limit}次），主页未见重复，请点击链接人工复核",
                        search_queries=queries,
                        manual_search_urls=manual_urls,
                        quota_exceeded=True,
                    )
                else:
                    results[video.aweme_id] = DedupResult(
                        status="green",
                        label="未发现",
                        summary=f"对应主页及全站搜索均未发现重复（今日已搜 {state.get('search_count', 0)}/{cfg.daily_search_limit} 次）",
                        search_queries=queries,
                        manual_search_urls=manual_urls,
                    )
        finally:
            _save_json_dict(state_path, state)
            await browser.close()

    return results
