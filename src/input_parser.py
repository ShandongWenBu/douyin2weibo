"""Parse input tasks from 待发.txt, CSV rows, or single-link CLI arguments."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

_DOUYIN_URL_RE = re.compile(
    r"https?://(?:v\.douyin\.com|www\.douyin\.com|douyin\.com|m\.douyin\.com|www\.iesdouyin\.com)/[^\s,，\"'<>]+"
)
_WEIBO_UID_RE = re.compile(r"weibo\.com/(?:u/)?(\d{5,})")
_SHARE_NOISE_MARKERS = ("复制打开抖音", "复制此链接", "打开Dou音", "长按复制")
_SHARE_CODE_TAIL_RE = re.compile(r"\s[A-Za-z0-9@._\-]{2,}:/\s*$")


@dataclass
class TaskInput:
    douyin_url: str
    weibo_name: str = ""
    weibo_uid: str = ""
    douyin_name: str = ""
    raw_line: str = ""


def _clean_url(url: str) -> str:
    return url.strip().rstrip(".,;，。；\"'")


def _extract_weibo_uid(text: str) -> str:
    m = _WEIBO_UID_RE.search(text)
    if m:
        return m.group(1)
    if text.strip().isdigit() and len(text.strip()) >= 6:
        return text.strip()
    return ""


def parse_line(line: str) -> TaskInput | None:
    """Parse one non-empty line from 待发.txt or pasted CSV/share text."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    # Skip CSV header lines
    if "外站主页链接" in stripped or "微博账号链接" in stripped:
        return None

    # Try CSV format only when line looks like a real CSV row (has weibo URL or >= 2 commas, not share text)
    is_share_blob = any(m in stripped for m in _SHARE_NOISE_MARKERS) or bool(_SHARE_CODE_TAIL_RE.search(stripped))
    if not is_share_blob and (stripped.count(",") >= 2 or ("," in stripped and "weibo.com" in stripped)) and _DOUYIN_URL_RE.search(stripped):
        row = next(csv.reader(io.StringIO(stripped)))
        douyin_url = ""
        weibo_uid = ""
        weibo_name = ""
        douyin_name = ""
        for cell in row:
            c = cell.strip()
            if not c:
                continue
            m_dy = _DOUYIN_URL_RE.search(c)
            if m_dy:
                douyin_url = _clean_url(m_dy.group(0))
                continue
            uid = _extract_weibo_uid(c)
            if uid and not weibo_uid:
                weibo_uid = uid
                continue
        # Standard 5-col CSV: weibo_url, phone, weibo_name, douyin_name, douyin_url
        if len(row) >= 5:
            weibo_name = row[2].strip()
            douyin_name = row[3].strip()
        elif len(row) >= 2 and not weibo_name:
            for cell in row:
                c = cell.strip()
                if c and not _DOUYIN_URL_RE.search(c) and not _WEIBO_UID_RE.search(c) and not c.replace(".", "").isdigit():
                    weibo_name = c
                    break
        if douyin_url:
            return TaskInput(
                douyin_url=douyin_url,
                weibo_name=weibo_name,
                weibo_uid=weibo_uid,
                douyin_name=douyin_name,
                raw_line=stripped,
            )

    # Standard format: <douyin_url_or_share_text> [tab/space <weibo_name> [tab/space <weibo_uid_or_url>]]
    m_dy = _DOUYIN_URL_RE.search(stripped)
    if not m_dy:
        return None

    douyin_url = _clean_url(m_dy.group(0))
    weibo_name = ""
    weibo_uid = ""

    if not is_share_blob:
        parts = re.split(r"\t+|\s{2,}", stripped)
        if len(parts) == 1:
            after = stripped[m_dy.end():].strip()
            tokens = after.split() if after else []
        else:
            tokens = [p.strip() for p in parts if p.strip() and douyin_url not in p]

        for tok in tokens:
            if _DOUYIN_URL_RE.search(tok):
                continue
            uid = _extract_weibo_uid(tok)
            if uid and not weibo_uid:
                weibo_uid = uid
            elif not weibo_name:
                weibo_name = tok

    return TaskInput(
        douyin_url=douyin_url,
        weibo_name=weibo_name,
        weibo_uid=weibo_uid,
        raw_line=stripped,
    )


def parse_text_block(
    text: str,
    default_weibo_name: str = "",
    default_weibo_uid: str = "",
) -> list[TaskInput]:
    """Parse a multi-line pasted block of links / share texts / CSV rows, deduplicating URLs."""
    tasks: list[TaskInput] = []
    seen_urls: set[str] = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        urls_in_line = [_clean_url(m.group(0)) for m in _DOUYIN_URL_RE.finditer(stripped)]
        if len(urls_in_line) > 1:
            for u in urls_in_line:
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    tasks.append(
                        TaskInput(
                            douyin_url=u,
                            weibo_name=default_weibo_name,
                            weibo_uid=default_weibo_uid,
                            raw_line=u,
                        )
                    )
            continue
        t = parse_line(stripped)
        if t and t.douyin_url not in seen_urls:
            seen_urls.add(t.douyin_url)
            if not t.weibo_name and default_weibo_name:
                t.weibo_name = default_weibo_name
            if not t.weibo_uid and default_weibo_uid:
                t.weibo_uid = default_weibo_uid
            tasks.append(t)
    return tasks


def load_tasks_from_file(path: Path) -> list[TaskInput]:
    """Read all valid tasks from a UTF-8 file (待发.txt or CSV)."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig")
    tasks: list[TaskInput] = []
    for line in text.splitlines():
        task = parse_line(line)
        if task:
            tasks.append(task)
    return tasks
