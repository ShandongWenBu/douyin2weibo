"""Configuration, cross-platform path resolution, and text/filename utilities."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_WIN_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_HASHTAG_RE = re.compile(r"#([^\s#]+)")


@dataclass
class TranscodeConfig:
    crf: int = 18
    preset: str = "medium"
    copy_if_1080p: bool = True
    audio_bitrate: str = "192k"
    upscale_sharpen: str = "unsharp=5:5:0.4:5:5:0.0"
    downscale_sharpen: str = "unsharp=3:3:0.25:3:3:0.0"
    cover_max_mb: float = 4.8


@dataclass
class DedupConfig:
    mode: str = "auto"
    enable_global_search: bool = True
    lookback_days: int = 30
    min_date: str = ""
    daily_search_limit: int = 40
    min_delay_sec: float = 8.0
    max_delay_sec: float = 15.0
    max_queries_per_video: int = 2
    check_own_timelines: bool = True
    cache_same_day: bool = True
    headless: bool = True


@dataclass
class AppConfig:
    project_root: Path
    douyin_cookie_path: Path
    weibo_cookie_path: Path
    ffmpeg_path: str
    ffprobe_path: str
    output_dir: Path
    ledger_path: Path
    dedup_state_path: Path
    latest_per_profile: int = 1
    strip_tag_keywords: list[str] = field(default_factory=list)
    transcode: TranscodeConfig = field(default_factory=TranscodeConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)


def _resolve_path(raw: str | None, base: Path, default_rel: str) -> Path:
    val = (raw or default_rel).strip()
    p = Path(val).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def _resolve_binary(raw: str | None, fallback_names: list[str], sibling_of: str | None = None) -> str:
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return str(candidate)
    if sibling_of:
        sib_dir = Path(sibling_of).parent
        for name in fallback_names:
            for ext in ("", ".exe"):
                cand = sib_dir / f"{name}{ext}"
                if cand.is_file():
                    return str(cand)
    for name in fallback_names:
        found = shutil.which(name)
        if found:
            return found
    return fallback_names[0]


def load_config(config_path: Path | None = None) -> AppConfig:
    cfg_file = config_path or (PROJECT_ROOT / "config.yaml")
    raw: dict[str, Any] = {}
    if cfg_file.is_file():
        with cfg_file.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    base = cfg_file.parent.resolve()
    ffmpeg = _resolve_binary(raw.get("ffmpeg_path"), ["ffmpeg"])
    ffprobe = _resolve_binary(raw.get("ffprobe_path"), ["ffprobe"], sibling_of=ffmpeg)

    tc_raw = raw.get("transcode") or {}
    transcode = TranscodeConfig(
        crf=int(tc_raw.get("crf", 18)),
        preset=str(tc_raw.get("preset", "medium")),
        copy_if_1080p=bool(tc_raw.get("copy_if_1080p", True)),
        audio_bitrate=str(tc_raw.get("audio_bitrate", "192k")),
        upscale_sharpen=str(tc_raw.get("upscale_sharpen", "unsharp=5:5:0.4:5:5:0.0")),
        downscale_sharpen=str(tc_raw.get("downscale_sharpen", "unsharp=3:3:0.25:3:3:0.0")),
        cover_max_mb=float(tc_raw.get("cover_max_mb", 4.8)),
    )

    dd_raw = raw.get("dedup") or {}
    dedup = DedupConfig(
        mode=str(dd_raw.get("mode", "auto")).lower(),
        enable_global_search=bool(dd_raw.get("enable_global_search", True)),
        lookback_days=int(dd_raw.get("lookback_days", 30)),
        min_date=str(dd_raw.get("min_date") or "").strip(),
        daily_search_limit=int(dd_raw.get("daily_search_limit", 40)),
        min_delay_sec=float(dd_raw.get("min_delay_sec", 8.0)),
        max_delay_sec=float(dd_raw.get("max_delay_sec", 15.0)),
        max_queries_per_video=int(dd_raw.get("max_queries_per_video", 2)),
        check_own_timelines=bool(dd_raw.get("check_own_timelines", True)),
        cache_same_day=bool(dd_raw.get("cache_same_day", True)),
        headless=bool(dd_raw.get("headless", True)),
    )

    output_dir = _resolve_path(raw.get("output_dir"), base, "~/Desktop/微博待发视频")
    ledger_path = _resolve_path(raw.get("ledger_path"), base, "ledger.json")
    wc_raw = raw.get("weibo_clean") or {}
    strip_keywords = [str(x).strip() for x in (wc_raw.get("strip_tag_keywords") or []) if str(x).strip()]

    return AppConfig(
        project_root=base,
        douyin_cookie_path=_resolve_path(
            raw.get("douyin_cookie_path"),
            base,
            "../social-auto-upload/cookies/douyin_douyin-creator.json",
        ),
        weibo_cookie_path=_resolve_path(
            raw.get("weibo_cookie_path"),
            base,
            "../social-auto-upload/cookies/weibo_weibo-creator.json",
        ),
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        output_dir=output_dir,
        ledger_path=ledger_path,
        dedup_state_path=base / ".dedup_state.json",
        latest_per_profile=int(raw.get("latest_per_profile", 1)),
        strip_tag_keywords=strip_keywords,
        transcode=transcode,
        dedup=dedup,
    )


def sanitize_filename(name: str, max_len: int = 80, fallback: str = "未命名") -> str:
    """Sanitize a string into a Windows- and macOS-safe file or folder name."""
    cleaned = _WIN_FORBIDDEN_RE.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    cleaned = re.sub(r"_+", "_", cleaned).strip("_ ")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" ._")
    if cleaned.upper() in _WIN_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned or fallback


def extract_title_and_hashtags(desc: str) -> tuple[str, list[str], str]:
    """Split Douyin desc into clean title, list of hashtags, and full description."""
    full_desc = (desc or "").strip()
    hashtags = _HASHTAG_RE.findall(full_desc)
    # Remove hashtags to get the core title text
    without_tags = _HASHTAG_RE.sub("", full_desc)
    without_tags = re.sub(r"@[A-Za-z0-9_\-\u4e00-\u9fff]+", "", without_tags)
    title = re.sub(r"\s+", " ", without_tags).strip(" \t\r\n|｜·—-")
    if not title:
        title = full_desc[:40].strip() or "无标题"
    return title, hashtags, full_desc


def save_output_dir_to_config(new_dir: str, config_path: Path | None = None) -> Path:
    """Persist a new output_dir to config.yaml while preserving comments and return resolved Path."""
    cleaned = (new_dir or "").strip()
    if not cleaned:
        raise ValueError("存放目录不能为空")

    cfg_file = config_path or (PROJECT_ROOT / "config.yaml")
    if not cfg_file.is_file():
        example_file = cfg_file.parent / "config.example.yaml"
        if example_file.is_file():
            shutil.copyfile(example_file, cfg_file)
        else:
            cfg_file.write_text("", encoding="utf-8")

    base = cfg_file.parent.resolve()
    resolved = _resolve_path(cleaned, base, cleaned)
    resolved.mkdir(parents=True, exist_ok=True)

    text = cfg_file.read_text(encoding="utf-8")
    new_line = f"output_dir: {json.dumps(cleaned, ensure_ascii=False)}"
    updated, count = re.subn(r"^output_dir\s*:.*$", new_line, text, count=1, flags=re.MULTILINE)
    if count == 0:
        suffix = "\n" if text and not text.endswith("\n") else ""
        updated = f"{text}{suffix}{new_line}\n"
    cfg_file.write_text(updated, encoding="utf-8")
    return resolved
