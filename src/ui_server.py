"""Foolproof local Web GUI server for douyin2weibo (zero CLI commands needed)."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_SPORTS_ACCOUNT_KEYWORDS,
    DEFAULT_SPORTS_REPLACE_TAGS,
    is_sports_account,
    load_config,
    save_output_dir_to_config,
)
from .cookie_login import login_and_save_cookie
from .douyin_fetcher import CaptchaRequiredError
from .input_parser import TaskInput, parse_line, parse_text_block
from .main import _load_ledger, _save_ledger, execute_pipeline
from .updater import apply_update, check_for_updates, run_git

_STATE_LOCK = threading.Lock()
_STOP_DEDUP_EVENT = threading.Event()
_SERVER_REF: dict[str, Any] = {"server": None, "port": 8765}
_APP_STATE: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "logs": [],
    "cards": [],
    "session_cards": [],
    "failures": [],
    "summary": None,
}


def _normalize_day(raw: str) -> str:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(raw or "").strip())
    return m.group(1) if m else str(raw or "").strip()


def _merge_cards(session_cards: list[dict[str, Any]], disk_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine disk-persisted cards with current-session cards so earlier videos of the same day never disappear."""
    cfg = load_config()
    ledger_videos = (_load_ledger(cfg.ledger_path).get("videos") or {})
    merged: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    for src_list in (disk_cards, session_cards):
        for raw_c in src_list:
            c = dict(raw_c)
            day = _normalize_day(c.get("date_str") or "")
            if not day and c.get("package_dir"):
                day = _normalize_day(Path(str(c["package_dir"])).parent.name)
            c["date_str"] = day or datetime.date.today().isoformat()
            aid = str(c.get("aweme_id") or "")
            key = (aid, c["date_str"]) if aid else (str(c.get("package_dir") or c.get("title") or ""), c["date_str"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if aid and aid in ledger_videos:
                c["published"] = bool((ledger_videos.get(aid) or {}).get("published", False))
            merged.append(c)

    merged.sort(key=lambda x: (x.get("date_str") or "", 0 if not x.get("published") else 1), reverse=False)
    # Keep newest date first; within the same date keep unpublished first, preserving newest-first order
    by_day: dict[str, list[dict[str, Any]]] = {}
    for c in merged:
        by_day.setdefault(c.get("date_str") or "", []).append(c)
    ordered: list[dict[str, Any]] = []
    for day in sorted(by_day.keys(), reverse=True):
        day_items = by_day[day]
        unpub = [x for x in day_items if not x.get("published")]
        pub = [x for x in day_items if x.get("published")]
        ordered.extend(unpub)
        ordered.extend(pub)
    return ordered


def _load_existing_cards() -> list[dict[str, Any]]:
    cfg = load_config()
    ledger = _load_ledger(cfg.ledger_path)
    videos_ledger = ledger.get("videos") or {}
    cards: list[dict[str, Any]] = []
    if not cfg.output_dir.is_dir():
        return cards
    meta_files = sorted(cfg.output_dir.glob("*/*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for mf in meta_files[:200]:
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        pkg_dir = mf.parent
        date_day = _normalize_day(pkg_dir.parent.name)
        cover_path = pkg_dir / "cover.jpg"
        video_path = pkg_dir / "video_1080p.mp4"
        aweme_id = str(meta.get("aweme_id") or "")
        dr = meta.get("dedup") or {}
        src_res = meta.get("source_resolution") or ""
        out_res = meta.get("output_resolution") or ""
        res_info = f"{src_res} -> {out_res}".strip(" ->")
        is_pub = bool((videos_ledger.get(aweme_id) or {}).get("published", False))
        wb_name = meta.get("weibo_name") or meta.get("author") or ""
        author = meta.get("author") or ""
        desc = meta.get("desc") or ""
        is_sports = bool(meta.get("is_sports")) or is_sports_account(
            wb_name, author, desc, cfg.sports_account_keywords
        )
        cards.append({
            "aweme_id": aweme_id,
            "date_str": date_day,
            "weibo_name": wb_name,
            "weibo_uid": meta.get("weibo_uid") or "",
            "author": author,
            "publish_time": meta.get("publish_time") or "",
            "duration_sec": meta.get("duration_sec") or 0,
            "resolution_info": res_info,
            "video_size_mb": meta.get("video_size_mb"),
            "title": meta.get("title") or "",
            "desc": desc,
            "is_sports": is_sports,
            "video_page_url": meta.get("video_page_url") or "",
            "dedup_status": dr.get("status") or "off",
            "dedup_label": dr.get("label") or "未查重",
            "dedup_summary": dr.get("summary") or "",
            "dedup_evidences": dr.get("evidences") or [],
            "manual_search_urls": dr.get("manual_search_urls") or [],
            "package_dir": str(pkg_dir),
            "cover_path": str(cover_path) if cover_path.is_file() else "",
            "video_path": str(video_path) if video_path.is_file() else "",
            "published": is_pub,
        })
    return cards


_FILE_LOGGER: logging.Logger | None = None


def _get_file_logger() -> logging.Logger:
    global _FILE_LOGGER
    if _FILE_LOGGER is not None:
        return _FILE_LOGGER
    cfg = load_config()
    log_dir = cfg.project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"
    lg = logging.getLogger("douyin2weibo.ui")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        handler = RotatingFileHandler(
            str(log_file),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        lg.addHandler(handler)
    _FILE_LOGGER = lg
    return lg


def _append_log(msg: str, level: str = "INFO") -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with _STATE_LOCK:
        _APP_STATE["logs"].append(f"[{ts}] {msg}")
        if len(_APP_STATE["logs"]) > 400:
            _APP_STATE["logs"] = _APP_STATE["logs"][-400:]
    try:
        lg = _get_file_logger()
        if level == "ERROR":
            lg.error(msg)
        elif level == "WARNING":
            lg.warning(msg)
        else:
            lg.info(msg)
    except Exception:
        pass


def _open_native_path(folder_path: Path, select_file: Path | None = None) -> None:
    if sys.platform == "darwin":
        if select_file and select_file.is_file():
            subprocess.Popen(["open", "-R", str(select_file)])
        else:
            subprocess.Popen(["open", str(folder_path)])
    elif sys.platform.startswith("win"):
        # Windows 上 explorer 的 /select, 必须写成 /select,"<完整路径>"（前缀 /select, 留在引号外），
        # 否则 subprocess.list2cmdline 会对含空格参数整体加引号，导致 explorer 回落打开「文档」目录。
        folder_str = str(folder_path)
        if len(folder_str) > 3:
            folder_str = folder_str.rstrip("\\")
        if select_file and select_file.is_file():
            file_str = str(select_file).replace('"', "")
            subprocess.Popen(f'explorer /select,"{file_str}"')
        else:
            folder_str = folder_str.replace('"', "")
            subprocess.Popen(f'explorer "{folder_str}"')
    else:
        subprocess.Popen(["xdg-open", str(folder_path)])


def _resolve_live_package_dir(output_dir: Path, pkg_dir: Path, aweme_id: str = "") -> Path | None:
    """Resolve a package directory even if its parent date folder was renamed (e.g. 共12个 -> 共37个)."""
    out_root = output_dir.resolve()
    try:
        pkg_dir.resolve().relative_to(out_root)
    except ValueError:
        return None

    if pkg_dir.is_dir():
        return pkg_dir.resolve()

    if not out_root.is_dir():
        return None

    # 1. Check sibling date folders sharing the same YYYY-MM-DD prefix
    day = _normalize_day(pkg_dir.parent.name)
    if day and re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        for date_child in sorted(out_root.iterdir()):
            if date_child.is_dir() and (date_child.name == day or date_child.name.startswith(f"{day}_")):
                cand = date_child / pkg_dir.name
                if cand.is_dir():
                    return cand.resolve()

    # 2. Fallback: locate by aweme_id across meta.json files inside output_dir
    if aweme_id:
        for mf in out_root.glob("*/*/meta.json"):
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
                if str(meta.get("aweme_id") or "") == aweme_id:
                    return mf.parent.resolve()
            except Exception:
                continue

    return None


def _pick_native_folder(initial_dir: Path) -> str | None:
    """Open OS native folder chooser dialog and return selected path, or None if cancelled."""
    if sys.platform == "darwin":
        escaped = str(initial_dir).replace('"', '\\"')
        script = (
            f'try\n'
            f'  set defaultLoc to POSIX file "{escaped}"\n'
            f'  set chosen to choose folder with prompt "请选择解析视频存放目录" default location defaultLoc\n'
            f'  return POSIX path of chosen\n'
            f'on error\n'
            f'  return ""\n'
            f'end try'
        )
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        out = (res.stdout or "").strip().rstrip("/")
        return out or None
    if sys.platform.startswith("win"):
        escaped = str(initial_dir).replace("'", "''")
        ps = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description = '请选择解析视频存放目录';"
            f"$d.SelectedPath = '{escaped}';"
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
        )
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        out = (res.stdout or "").strip()
        return out or None
    raise RuntimeError("当前系统不支持弹窗选目录，请直接点「改路径」输入文件夹路径。")


def _schedule_self_restart() -> None:
    def _do_restart() -> None:
        srv = _SERVER_REF.get("server")
        port = int(_SERVER_REF.get("port") or 8765)
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        subprocess.Popen([sys.executable, "-m", "src.ui_server", "--port", str(port), "--no-open"])
        os._exit(0)

    threading.Timer(0.4, _do_restart).start()


def _login_worker(platform: str) -> None:
    try:
        cfg = load_config()
        dst = cfg.douyin_cookie_path if platform == "douyin" else cfg.weibo_cookie_path
        asyncio.run(login_and_save_cookie(platform, dst, log_fn=_append_log))
    except Exception as exc:
        _append_log(f"[登录未完成] {exc}")
    finally:
        with _STATE_LOCK:
            _APP_STATE["running"] = False


def _run_worker(payload: dict[str, Any]) -> None:
    try:
        cfg = load_config()
        preset = payload.get("dedup_preset") or "own_only"
        if preset == "own_only":
            cfg.dedup.mode = "auto"
            cfg.dedup.enable_global_search = False
        elif preset == "full":
            cfg.dedup.mode = "auto"
            cfg.dedup.enable_global_search = True
        elif preset == "manual":
            cfg.dedup.mode = "manual"
        elif preset == "off":
            cfg.dedup.mode = "off"

        cfg.dedup.lookback_days = int(payload.get("lookback_days", 30))
        cfg.latest_per_profile = max(1, int(payload.get("latest_per_profile", 1)))
        tc_profile = str(payload.get("transcode_profile") or "")
        if tc_profile == "medium_18":
            cfg.transcode.preset, cfg.transcode.crf = "medium", 18
        elif tc_profile == "fast_19":
            cfg.transcode.preset, cfg.transcode.crf = "fast", 19
        elif tc_profile == "medium_20":
            cfg.transcode.preset, cfg.transcode.crf = "medium", 20
        elif tc_profile == "slow_18":
            cfg.transcode.preset, cfg.transcode.crf = "slow", 18
        elif "crf" in payload:
            cfg.transcode.crf = int(payload.get("crf", 18))
        if "copy_if_1080p" in payload:
            cfg.transcode.copy_if_1080p = bool(payload.get("copy_if_1080p"))

        tasks: list[TaskInput] = []
        run_mode = payload.get("mode") or "paste"
        if run_mode == "paste":
            paste_text = str(payload.get("paste_text") or "")
            def_wb = str(payload.get("default_weibo") or "").strip()
            def_uid = str(payload.get("default_uid") or "").strip()
            tasks = parse_text_block(paste_text, default_weibo_name=def_wb, default_weibo_uid=def_uid)
            if not tasks:
                raise ValueError("未能从文本框识别到有效的抖音链接，请粘贴 1 条或多条视频链接。")
        elif run_mode == "single":
            raw = f"{payload.get('single_url', '')}\t{payload.get('single_weibo', '')}\t{payload.get('single_uid', '')}".strip()
            t = parse_line(raw)
            if not t:
                raise ValueError("未能识别有效的抖音链接，请检查是否贴全了链接。")
            tasks = [t]
        else:
            lines = payload.get("selected_lines") or []
            for ln in lines:
                t = parse_line(str(ln))
                if t:
                    tasks.append(t)
            if not tasks:
                raise ValueError("请至少勾选或填写 1 条有效账号/链接。")

        def _set_phase(ph: str) -> None:
            with _STATE_LOCK:
                _APP_STATE["phase"] = ph

        res = asyncio.run(
            execute_pipeline(
                cfg,
                tasks,
                dedup_only=bool(payload.get("dedup_only", False)),
                force_download=bool(payload.get("force_download", False)),
                overwrite=bool(payload.get("overwrite", False)),
                log_fn=_append_log,
                stop_dedup_event=_STOP_DEDUP_EVENT,
                phase_fn=_set_phase,
            )
        )
        today_str = datetime.date.today().isoformat()
        for c in res["cards"]:
            c["date_str"] = _normalize_day(c.get("date_str") or today_str)
        disk_cards = _load_existing_cards()
        with _STATE_LOCK:
            prev_session = list(_APP_STATE.get("session_cards") or [])
            # Keep current-run cards + previous session non-disk cards (e.g. skipped/dedup-only), merged with all disk cards
            new_session = res["cards"] + [x for x in prev_session if not x.get("package_dir")]
            merged_cards = _merge_cards(new_session, disk_cards)
            _APP_STATE["session_cards"] = new_session[:100]
            _APP_STATE["cards"] = merged_cards
            _APP_STATE["failures"] = res["failures"]
            _APP_STATE["summary"] = {
                "success_count": res["success_count"],
                "skipped_red_count": res["skipped_red_count"],
                "failure_count": len(res["failures"]),
            }
        _append_log(
            f"全部完成：打包 {res['success_count']} 条，跳过已发 {res['skipped_red_count']} 条，失败 {len(res['failures'])} 条。"
        )
    except CaptchaRequiredError as exc:
        _append_log(f"[已暂停] {exc}", level="WARNING")
        with _STATE_LOCK:
            _APP_STATE["failures"] = [{"url": "-", "weibo_name": "风控/验证码", "error": str(exc)}]
    except Exception as exc:
        _append_log(f"[出错] {exc}", level="ERROR")
        try:
            _get_file_logger().error("Worker traceback:\n%s", traceback.format_exc())
        except Exception:
            pass
        with _STATE_LOCK:
            _APP_STATE["failures"] = [{"url": "-", "weibo_name": "运行错误", "error": str(exc)}]
    finally:
        _STOP_DEDUP_EVENT.clear()
        with _STATE_LOCK:
            _APP_STATE["running"] = False
            _APP_STATE["phase"] = "idle"


_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>douyin2weibo 工作台</title>
  <style>
    :root {
      --bg: #f1f5f9; --card: #ffffff; --text: #0f172a; --muted: #64748b;
      --border: #e2e8f0; --primary: #2563eb; --primary-h: #1d4ed8;
      --green-bg: #ecfdf3; --green-fg: #027a48; --green-bd: #abefc6;
      --yellow-bg: #fffaeb; --yellow-fg: #b54708; --yellow-bd: #fedf89;
      --red-bg: #fef3f2; --red-fg: #b42318; --red-bd: #fecdca;
      --gray-bg: #f8fafc; --gray-fg: #334155; --gray-bd: #cbd5e1;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; background: var(--bg); color: var(--text); }
    .wrap { max-width: 1320px; margin: 0 auto; padding: 16px 18px 64px; }
    .topbar {
      background: var(--card); border: 1px solid var(--border); border-radius: 14px;
      padding: 14px 18px; margin-bottom: 14px;
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px;
    }
    .topbar h1 { margin: 0; font-size: 19px; display: flex; align-items: center; gap: 8px; }
    .sub { color: var(--muted); font-size: 12.5px; }
    .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px 18px; margin-bottom: 14px; }
    .tabs { display: flex; gap: 8px; margin-bottom: 12px; }
    .tab { padding: 7px 14px; border-radius: 8px; border: 1px solid var(--border); background: #f8fafc; font-weight: 600; font-size: 13.5px; cursor: pointer; }
    .tab.active { background: var(--primary); color: #fff; border-color: var(--primary); }
    .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin-bottom: 10px; }
    label.field { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; font-weight: 600; color: #334155; }
    input[type="text"], select, textarea {
      width: 100%; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 13.5px; font-family: inherit; background: #fff;
    }
    textarea { min-height: 110px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
    .account-list { max-height: 175px; overflow-y: auto; border: 1px solid var(--border); border-radius: 10px; padding: 6px 10px; background: #fafbfc; }
    .account-item { display: flex; align-items: center; gap: 8px; padding: 5px 4px; border-bottom: 1px dashed #eef2f6; font-size: 13px; cursor: pointer; }
    .account-item:last-child { border-bottom: none; }
    .btn {
      border: 1px solid var(--border); background: #fff; color: var(--text);
      padding: 6px 11px; border-radius: 8px; font-size: 12.5px; font-weight: 600; cursor: pointer;
      display: inline-flex; align-items: center; gap: 4px; text-decoration: none; white-space: nowrap;
      transition: background 0.15s, border-color 0.15s;
    }
    .btn:hover { background: #f1f5f9; border-color: #94a3b8; }
    .btn-primary { background: var(--primary); border-color: var(--primary); color: #fff; }
    .btn-primary:hover { background: var(--primary-h); }
    .btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
    .btn-soft { background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }
    .btn-soft:hover { background: #dbeafe; }
    .btn-copied { background: #16a34a !important; border-color: #16a34a !important; color: #fff !important; }
    .btn-lg { padding: 9px 20px; font-size: 14px; }
    .actions-bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; margin-top: 8px; padding-top: 10px; border-top: 1px solid var(--border); }
    .log-box {
      background: #0f172a; color: #e2e8f0; border-radius: 10px; padding: 10px 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
      max-height: 150px; overflow-y: auto; white-space: pre-wrap; margin-top: 10px;
    }
    .toolbar {
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px;
      padding-bottom: 12px; margin-bottom: 14px; border-bottom: 1px solid var(--border);
    }
    .toolbar-group { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .search-input { width: 200px !important; padding: 6px 10px !important; font-size: 13px !important; }
    .date-select { width: auto !important; padding: 6px 10px !important; font-size: 13px !important; }
    .pill { padding: 5px 11px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid var(--border); background: var(--gray-bg); cursor: pointer; }
    .pill.active { outline: 2px solid var(--primary); background: #eff6ff; color: var(--primary); }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 16px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; display: flex; flex-direction: column; box-shadow: 0 1px 3px rgba(15,23,42,0.04); }
    .card.published { opacity: 0.56; border-style: dashed; }
    .cover-wrap { position: relative; background: #0f172a; height: 195px; display: flex; align-items: center; justify-content: center; overflow: hidden; }
    .cover-wrap img { width: 100%; height: 100%; object-fit: cover; }
    .badge-top { position: absolute; top: 10px; left: 10px; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; border: 1px solid transparent; }
    .badge-dur { position: absolute; bottom: 10px; right: 10px; background: rgba(15,23,42,0.8); color: #fff; font-size: 12px; padding: 2px 8px; border-radius: 6px; }
    .preview-fab { position: absolute; bottom: 10px; left: 10px; background: rgba(15,23,42,0.8); color: #fff; border: 1px solid rgba(255,255,255,0.25); padding: 3px 9px; border-radius: 6px; font-size: 12px; cursor: pointer; font-weight: 600; }
    .preview-fab:hover { background: rgba(37,99,235,0.9); }
    .status-green { background: var(--green-bg); color: var(--green-fg); border-color: var(--green-bd); }
    .status-yellow { background: var(--yellow-bg); color: var(--yellow-fg); border-color: var(--yellow-bd); }
    .status-red { background: var(--red-bg); color: var(--red-fg); border-color: var(--red-bd); }
    .status-manual, .status-off { background: var(--gray-bg); color: var(--gray-fg); border-color: var(--gray-bd); }
    .body { padding: 14px 15px 15px; display: flex; flex-direction: column; gap: 10px; flex: 1; }
    .wb-tag { font-weight: 700; color: #1e293b; background: #eff6ff; border: 1px solid #bfdbfe; padding: 2px 8px; border-radius: 6px; font-size: 12.5px; }
    .meta-row { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
    .file-bar {
      background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px;
      display: flex; flex-direction: column; gap: 7px;
    }
    .file-path-hint { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: #475569; word-break: break-all; }
    .file-btns { display: flex; flex-wrap: wrap; gap: 6px; }
    .field-label { display: flex; justify-content: space-between; align-items: center; font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 4px; }
    .text-box { background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px; font-size: 13px; word-break: break-word; white-space: pre-wrap; max-height: 82px; overflow-y: auto; }
    .dedup-box { background: #f8fafc; border: 1px dashed var(--border); border-radius: 8px; padding: 7px 10px; font-size: 12px; }
    .dedup-links { margin-top: 5px; display: flex; flex-wrap: wrap; gap: 6px; }
    .dedup-links a { color: var(--primary); text-decoration: none; font-size: 12px; background: #eff6ff; padding: 2px 7px; border-radius: 5px; }
    .footer-actions { margin-top: auto; padding-top: 9px; border-top: 1px solid var(--border); display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; }
    .pub-btn { border: 1px solid #cbd5e1; background: #fff; padding: 6px 12px; border-radius: 8px; font-size: 12.5px; font-weight: 700; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
    .pub-btn.done { background: #dcfce7; border-color: #86efac; color: #166534; }
    table.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
    table.tbl th, table.tbl td { padding: 10px 8px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
    table.tbl th { background: #f8fafc; color: #475569; font-size: 12px; font-weight: 700; }
    table.tbl tr.published { opacity: 0.52; }
    .thumb-sm { width: 72px; height: 46px; object-fit: cover; border-radius: 6px; background: #0f172a; display: block; }
    .modal-mask { position: fixed; inset: 0; background: rgba(15,23,42,0.72); display: none; align-items: center; justify-content: center; z-index: 1000; padding: 20px; }
    .modal-mask.open { display: flex; }
    .modal-box { background: #0f172a; border-radius: 14px; padding: 14px; max-width: 900px; width: 100%; color: #fff; }
    .modal-box video { width: 100%; max-height: 75vh; border-radius: 8px; background: #000; }
    .toast { position: fixed; bottom: 20px; right: 20px; background: #111827; color: #fff; padding: 9px 14px; border-radius: 8px; font-size: 13px; opacity: 0; pointer-events: none; transition: opacity 0.2s; z-index: 999; }
    .toast.show { opacity: 1; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>douyin2weibo 工作台</h1>
        <div class="sub" style="display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-top:4px;">
          <span>视频存放目录：<code id="output-root-path">加载中...</code></span>
          <button class="btn" style="padding:3px 8px; font-size:12px;" onclick="pickOutputDir()">📁 浏览选择</button>
          <button class="btn" style="padding:3px 8px; font-size:12px;" onclick="toggleEditOutputDir()">✏️ 改路径</button>
        </div>
        <div id="output-dir-editor" style="display:none; margin-top:6px; gap:6px; align-items:center; flex-wrap:wrap;">
          <input type="text" id="output-dir-input" placeholder="输入文件夹路径，例如 ~/Desktop/微博待发视频 或 D:\\微博视频" style="width:360px; padding:5px 8px; font-size:12.5px;" />
          <button class="btn btn-primary" style="padding:5px 10px; font-size:12px;" onclick="saveOutputDir()">💾 保存目录</button>
          <button class="btn" style="padding:5px 10px; font-size:12px;" onclick="toggleEditOutputDir(false)">取消</button>
        </div>
        <div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:7px; align-items:center; font-size:12.5px;">
          <span id="dy-login-badge" class="pill">抖音：检测中...</span>
          <button class="btn" onclick="startLogin('douyin')">🔑 扫码登录/换抖音号</button>
          <span id="wb-login-badge" class="pill">微博查重：检测中...</span>
          <button class="btn" onclick="startLogin('weibo')">🔑 扫码登录/换微博小号</button>
        </div>
      </div>
      <div style="display:flex; flex-wrap:wrap; gap:8px; align-items:center;">
        <button class="btn btn-soft" onclick="openOutputRoot()">📂 打开输出目录</button>
        <button class="btn" onclick="copyText('https://weibo.com/upload/channel', '微博上传页网址', this)">🔗 复制上传页网址</button>
        <button class="btn" onclick="copyText(document.getElementById('output-root-path').textContent, '输出总目录路径', this)">📋 复制目录路径</button>
        <button class="btn" id="btn-check-update" onclick="checkUpdate()">🔄 检查更新</button>
        <button class="btn" id="btn-toggle-runner" onclick="toggleRunnerPanel()">🔽 收起抓取面板</button>
      </div>
    </div>

    <div id="update-banner" class="panel" style="display:none; background:#eff6ff; border-color:#bfdbfe; padding:12px 16px;"></div>

    <div class="panel" id="runner-panel">
      <div class="tabs">
        <button class="tab active" id="tab-paste" onclick="switchMode('paste')">📋 粘贴视频链接（批量/单条）</button>
        <button class="tab" id="tab-batch" onclick="switchMode('batch')">📂 固定账号表（待发.txt）</button>
      </div>

      <div id="pane-paste">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; flex-wrap:wrap; gap:8px;">
          <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
            <span style="font-size:13px; color:#334155; font-weight:600;">把找好的抖音单条视频链接一条一行粘贴到这里（支持十几条一起下）：</span>
            <span id="paste-count-badge" class="pill status-green" style="cursor:default;">已识别 0 条链接</span>
          </div>
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <button class="btn btn-soft" onclick="appendFromClipboard()">📋 从剪贴板追加一条</button>
            <button class="btn" onclick="cleanPasteBox()">🧹 去重整理</button>
            <button class="btn" onclick="clearPasteBox()">🗑️ 清空</button>
          </div>
        </div>
        <textarea id="paste-links-box" style="min-height:150px;" oninput="updatePasteCount()" placeholder="直接粘贴抖音单条视频链接或分享口令，每行一条（也支持在链接后加空格和目标微博号名）：&#10;https://www.douyin.com/video/1234567890123456789&#10;https://v.douyin.com/xxxxxx/  示例微博号&#10;...可一次贴十几个一起下"></textarea>
        <div class="row" style="margin-top:8px; margin-bottom:4px;">
          <label class="field">
            统一微博号名（可选，留空则按每行指定或自动用抖音作者名）
            <input type="text" id="paste-default-weibo" placeholder="如果这一批都发同一个微博号，填这里即可" />
          </label>
          <label class="field">
            统一微博 UID（可选，仅查重时需要）
            <input type="text" id="paste-default-uid" placeholder="留空即可" />
          </label>
        </div>
      </div>

      <div id="pane-batch" style="display:none;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; flex-wrap:wrap; gap:8px;">
          <div style="font-size:13px; color:#334155; font-weight:600;">勾选要处理的账号（来自 待发.txt）：</div>
          <div style="display:flex; gap:6px;">
            <button class="btn" onclick="selectAllAccounts(true)">全选</button>
            <button class="btn" onclick="selectAllAccounts(false)">全不选</button>
            <button class="btn" onclick="toggleEditRaw()">✏️ 编辑待发.txt</button>
          </div>
        </div>
        <div class="account-list" id="account-checklist"></div>
        <div id="raw-editor" style="display:none; margin-top:8px;">
          <textarea id="raw-tasks-text" placeholder="每行贴一个：抖音链接 微博号名，或者直接粘贴表格 CSV 行"></textarea>
          <div style="margin-top:6px; display:flex; gap:8px;">
            <button class="btn btn-primary" onclick="saveRawTasks()">💾 保存并刷新列表</button>
            <button class="btn" onclick="toggleEditRaw(false)">取消</button>
          </div>
        </div>
      </div>

      <div class="row" style="margin-top:10px;">
        <label class="field">
          查重方式
          <select id="opt-dedup">
            <option value="own_only">⚡ 极速仅查自家号主页（推荐）</option>
            <option value="full">🔍 自家号 + 全站搜索</option>
            <option value="manual">👀 仅生成搜微博按钮</option>
            <option value="off">⏭️ 跳过查重直接下</option>
          </select>
        </label>
        <label class="field">
          查重时间范围
          <select id="opt-lookback">
            <option value="0">自动（从抖音发布日起）</option>
            <option value="7">最近 7 天</option>
            <option value="30" selected>最近 30 天</option>
            <option value="90">最近 90 天</option>
          </select>
        </label>
        <label class="field">
          抖音主页抓最新几条
          <select id="opt-latest">
            <option value="1" selected>最新 1 条</option>
            <option value="2">最新 2 条</option>
            <option value="3">最新 3 条</option>
          </select>
        </label>
        <label class="field">
          1080P 转码速度与画质
          <select id="opt-transcode-profile">
            <option value="medium_18" selected>⚡ 均衡高速（CRF 18 · medium，推荐）</option>
            <option value="fast_19">🚀 极速省时（CRF 19 · fast）</option>
            <option value="medium_20">💾 省体积高速（CRF 20 · medium）</option>
            <option value="slow_18">🎞️ 精压慢速（CRF 18 · slow）</option>
          </select>
        </label>
      </div>

      <div class="actions-bar">
        <div style="display:flex; gap:14px; flex-wrap:wrap; font-size:13px;">
          <label style="display:inline-flex; align-items:center; gap:5px; cursor:pointer;">
            <input type="checkbox" id="opt-copy-1080p" checked /> 源片已是 1080P H.264 时直接秒封装（免重编码）
          </label>
          <label style="display:inline-flex; align-items:center; gap:5px; cursor:pointer;">
            <input type="checkbox" id="opt-dedup-only" /> 仅查重不下载
          </label>
          <label style="display:inline-flex; align-items:center; gap:5px; cursor:pointer;">
            <input type="checkbox" id="opt-overwrite" /> 强制重新转码覆盖
          </label>
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <button class="btn btn-lg" id="btn-stop-dedup" style="display:none; background:#fff7ed; border-color:#fdba74; color:#c2410c;" onclick="stopDedup()">⏭️ 停止查重并直接处理</button>
          <button class="btn btn-primary btn-lg" id="btn-run" onclick="startRun()">🚀 开始处理</button>
        </div>
      </div>

      <div class="log-box" id="log-box">就绪。如果只需找已下载的视频去创作罐头上传，直接看下方列表即可。</div>
    </div>

    <div class="panel">
      <div class="toolbar">
        <div class="toolbar-group">
          <strong style="font-size:16px;">已打包视频 & 上传工作台</strong>
          <span class="sub" id="wb-summary"></span>
        </div>
        <div class="toolbar-group">
          <input type="text" class="search-input" id="search-box" placeholder="🔍 搜微博号 / 标题关键词..." oninput="renderCards()" />
          <select class="date-select" id="date-filter" onchange="onDateFilterChange()">
            <option value="all">全部日期</option>
          </select>
          <button class="btn" id="btn-view-card" onclick="setViewMode('card')">🃏 卡片视图</button>
          <button class="btn" id="btn-view-table" onclick="setViewMode('table')">📋 紧凑列表</button>
        </div>
      </div>

      <div class="toolbar-group" style="justify-content:space-between; margin-bottom:12px; gap:10px;">
        <div class="toolbar-group" id="filter-bar"></div>
        <div class="toolbar-group" style="background:#f8fafc; border:1px solid var(--border); padding:5px 10px; border-radius:9px; font-size:12.5px;">
          <label style="display:inline-flex; align-items:center; gap:5px; cursor:pointer; font-weight:600;">
            <input type="checkbox" id="opt-clean-tags" checked onchange="saveCleanOpts();renderCards();" />
            自动去抖音尾标
          </label>
          <label style="display:inline-flex; align-items:center; gap:5px; cursor:pointer; font-weight:600; color:#0369a1;">
            <input type="checkbox" id="opt-sports-tags" checked onchange="saveCleanOpts();renderCards();" />
            体育号换指定标签
          </label>
          <input type="text" id="opt-extra-tags" placeholder="附加微博话题（可选，如 #三角洲行动[超话]#）" style="width:240px; padding:4px 8px; font-size:12px;" oninput="saveCleanOpts();renderCards();" />
        </div>
      </div>
      <div id="failures-box" style="margin-bottom:12px;"></div>
      <div id="cards-container"></div>
    </div>
  </div>

  <div class="modal-mask" id="video-modal" onclick="if(event.target===this)closePreview()">
    <div class="modal-box">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
        <strong id="preview-title" style="font-size:14px;">视频预览</strong>
        <button class="btn" onclick="closePreview()">✕ 关闭</button>
      </div>
      <video id="preview-player" controls playsinline></video>
    </div>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    let mode = "paste";
    let viewMode = localStorage.getItem("d2w_view_mode") || "card";
    let runnerCollapsed = localStorage.getItem("d2w_runner_collapsed") === "1";
    let stripTagKeywords = ["抖音精选", "上抖音", "来抖音", "抖音小助手", "青年创作者成长计划", "氪金兽"];
    let sportsAccountKeywords = [
      "体坛", "体育", "足球", "篮球", "乒乓", "羽毛球", "网球", "排球",
      "台球", "斯诺克", "懂个球", "绿茵", "国足", "NBA", "CBA", "欧冠",
      "英超", "西甲", "德甲", "意甲", "法甲", "中超", "奥运", "亚运",
      "世锦赛", "田径", "游泳", "跳水", "体操", "冰雪", "冬奥", "短道",
      "花滑", "拳击", "格斗", "UFC", "F1", "赛车", "马拉松", "高尔夫",
      "棒球", "橄榄球", "冰球"
    ];
    let sportsReplaceTags = "#微博视频号迎新计划##一分钟精选视频扶持计划##V观体坛#";
    let sportsOverrides = {};
    let renderedCards = [];
    let parsedAccounts = [];
    let cardsData = [];
    let failuresData = [];
    let currentFilter = "all";
    let dateFilterTouched = false;
    let pollTimer = null;

    function loadCleanOpts() {
      const cleanOn = localStorage.getItem("d2w_clean_tags");
      document.getElementById("opt-clean-tags").checked = cleanOn === null ? true : cleanOn === "1";
      const sportsOn = localStorage.getItem("d2w_sports_tags");
      document.getElementById("opt-sports-tags").checked = sportsOn === null ? true : sportsOn === "1";
      document.getElementById("opt-extra-tags").value = localStorage.getItem("d2w_extra_tags") || "";
      try {
        sportsOverrides = JSON.parse(localStorage.getItem("d2w_sports_overrides") || "{}") || {};
      } catch {
        sportsOverrides = {};
      }
    }
    function saveCleanOpts() {
      localStorage.setItem("d2w_clean_tags", document.getElementById("opt-clean-tags").checked ? "1" : "0");
      localStorage.setItem("d2w_sports_tags", document.getElementById("opt-sports-tags").checked ? "1" : "0");
      localStorage.setItem("d2w_extra_tags", document.getElementById("opt-extra-tags").value || "");
    }
    function cardSportsKey(c) {
      return `${c.aweme_id || c.package_dir || c.title || ""}::${c.weibo_name || ""}`;
    }
    function sportsKeywordMatches(haystack, kw) {
      const cleanKw = String(kw || "").trim().toLowerCase();
      const hay = String(haystack || "").toLowerCase();
      if (!cleanKw || !hay) return false;
      if (/^[a-z0-9]+$/i.test(cleanKw)) {
        const re = new RegExp(`(?:^|[^a-z0-9])${cleanKw}(?:$|[^a-z0-9])`, "i");
        return re.test(hay);
      }
      return hay.includes(cleanKw);
    }
    function isSportsCard(c) {
      if (!c || typeof c !== "object") return false;
      const key = cardSportsKey(c);
      if (Object.prototype.hasOwnProperty.call(sportsOverrides, key)) {
        return Boolean(sportsOverrides[key]);
      }
      if (Array.isArray(sportsAccountKeywords) && sportsAccountKeywords.length) {
        const tags = (String(c.desc || "").match(/#[^\\s#]+#?/g) || []).join(" ");
        const haystack = [c.weibo_name || "", c.author || "", tags].join(" ").trim();
        if (sportsAccountKeywords.some(kw => sportsKeywordMatches(haystack, kw))) {
          return true;
        }
        return false;
      }
      return Boolean(c.is_sports);
    }
    function toggleCardSports(idx) {
      const c = renderedCards[idx];
      if (!c) return;
      const key = cardSportsKey(c);
      const nextVal = !isSportsCard(c);
      sportsOverrides[key] = nextVal;
      localStorage.setItem("d2w_sports_overrides", JSON.stringify(sportsOverrides));
      renderCards();
      showToast(nextVal ? "已切换为体育号：全部标签将替换为指定体育标签" : "已切换为普通号：仅按规则去抖音尾标");
    }
    function formatWeiboDesc(cardOrDesc) {
      const isObj = cardOrDesc && typeof cardOrDesc === "object";
      let text = String(isObj ? (cardOrDesc.desc || "") : (cardOrDesc || "")).trim();
      const doClean = document.getElementById("opt-clean-tags").checked;
      const doSports = document.getElementById("opt-sports-tags").checked;
      const extra = (document.getElementById("opt-extra-tags").value || "").trim();
      const sportsHit = isObj && doSports && isSportsCard(cardOrDesc);
      if (sportsHit) {
        const body = text.replace(/#[^\\s#]+#?/g, "").replace(/\\s+/g, " ").trim();
        const rep = String(sportsReplaceTags || "#微博视频号迎新计划##一分钟精选视频扶持计划##V观体坛#").trim();
        return [body, rep].filter(Boolean).join(" ").trim();
      } else if (doClean) {
        const tags = text.match(/#[^\\s#]+#?/g) || [];
        let body = text.replace(/#[^\\s#]+#?/g, "").replace(/\\s+/g, " ").trim();
        const kept = [];
        for (const t of tags) {
          const cleanName = t.replace(/^#+|#+$/g, "");
          if (!cleanName) continue;
          if (stripTagKeywords.some(kw => cleanName.includes(kw))) continue;
          kept.push("#" + cleanName + "#");
        }
        text = [body, kept.join("")].filter(Boolean).join(" ").trim();
      }
      if (extra) {
        text = text ? (text + " " + extra) : extra;
      }
      return text;
    }
    function copyCardDesc(idx, btnEl) {
      const c = renderedCards[idx];
      if (!c) return;
      const doSports = document.getElementById("opt-sports-tags").checked;
      const sportsHit = doSports && isSportsCard(c);
      copyText(formatWeiboDesc(c), sportsHit ? "体育号微博简介（已换指定标签）" : "微博简介", btnEl);
    }

    function showToast(msg) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.classList.add("show");
      clearTimeout(window.__tt);
      window.__tt = setTimeout(() => el.classList.remove("show"), 1800);
    }
    function esc(s) {
      return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }
    function applyRunnerCollapse() {
      document.getElementById("runner-panel").style.display = runnerCollapsed ? "none" : "";
      document.getElementById("btn-toggle-runner").textContent = runnerCollapsed ? "🔼 展开抓取面板" : "🔽 收起抓取面板";
    }
    function toggleRunnerPanel() {
      runnerCollapsed = !runnerCollapsed;
      localStorage.setItem("d2w_runner_collapsed", runnerCollapsed ? "1" : "0");
      applyRunnerCollapse();
    }
    function setViewMode(vm) {
      viewMode = vm;
      localStorage.setItem("d2w_view_mode", vm);
      document.getElementById("btn-view-card").classList.toggle("btn-primary", vm === "card");
      document.getElementById("btn-view-table").classList.toggle("btn-primary", vm === "table");
      renderCards();
    }
    function switchMode(m) {
      mode = m;
      document.getElementById("tab-paste").classList.toggle("active", m === "paste");
      document.getElementById("tab-batch").classList.toggle("active", m === "batch");
      document.getElementById("pane-paste").style.display = m === "paste" ? "" : "none";
      document.getElementById("pane-batch").style.display = m === "batch" ? "" : "none";
    }
    function extractDouyinUrlsFromText(txt) {
      const re = /https?:\\/\\/(?:v\\.douyin\\.com|www\\.douyin\\.com|douyin\\.com|m\\.douyin\\.com|www\\.iesdouyin\\.com)\\/[^\\s,，"'<>]+/g;
      const matches = String(txt || "").match(re) || [];
      const seen = new Set();
      const out = [];
      for (const raw of matches) {
        const u = raw.replace(/[.,;，。；"']+$/, "");
        if (u && !seen.has(u)) { seen.add(u); out.push(u); }
      }
      return out;
    }
    function updatePasteCount() {
      const box = document.getElementById("paste-links-box");
      const urls = extractDouyinUrlsFromText(box.value);
      const badge = document.getElementById("paste-count-badge");
      badge.textContent = "已识别 " + urls.length + " 条链接";
    }
    async function appendFromClipboard() {
      try {
        const clip = (await navigator.clipboard.readText() || "").trim();
        if (!clip) { showToast("剪贴板为空"); return; }
        const box = document.getElementById("paste-links-box");
        const cur = box.value.trim();
        box.value = cur ? (cur + "\\n" + clip) : clip;
        updatePasteCount();
        showToast("已追加到粘贴栏");
      } catch {
        showToast("请直接在下方文本框按 Ctrl+V / Cmd+V 粘贴");
      }
    }
    function cleanPasteBox() {
      const box = document.getElementById("paste-links-box");
      const urls = extractDouyinUrlsFromText(box.value);
      box.value = urls.join("\\n");
      updatePasteCount();
      showToast("已整理去重，共 " + urls.length + " 条链接");
    }
    function clearPasteBox() {
      document.getElementById("paste-links-box").value = "";
      updatePasteCount();
    }
    function toggleEditRaw(force) {
      const box = document.getElementById("raw-editor");
      box.style.display = (force !== undefined ? force : box.style.display === "none") ? "" : "none";
    }
    function selectAllAccounts(val) {
      document.querySelectorAll(".acc-chk").forEach(el => el.checked = val);
    }
    function renderAccounts() {
      const box = document.getElementById("account-checklist");
      if (!parsedAccounts.length) {
        box.innerHTML = `<div style="padding:8px; color:#6b7280; font-size:13px;">待发.txt 目前为空，请点右上角「✏️ 编辑待发.txt」粘贴账号或链接。</div>`;
        return;
      }
      box.innerHTML = parsedAccounts.map((a, i) => `
        <label class="account-item">
          <input type="checkbox" class="acc-chk" data-idx="${i}" checked />
          <span class="wb-tag">${esc(a.weibo_name || "未命名微博")}</span>
          <span style="color:#4b5563;">${esc(a.douyin_name || "")}</span>
          <span style="color:#6b7280; font-family:monospace; font-size:12px;">${esc(a.douyin_url)}</span>
        </label>
      `).join("");
    }
    async function saveRawTasks() {
      const text = document.getElementById("raw-tasks-text").value;
      const r = await fetch("/api/save-tasks", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ text })
      });
      const d = await r.json();
      parsedAccounts = d.accounts || [];
      renderAccounts();
      toggleEditRaw(false);
      showToast("已保存 待发.txt（共 " + parsedAccounts.length + " 条）");
    }
    async function copyText(text, label, btnEl) {
      try { await navigator.clipboard.writeText(text); }
      catch {
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); document.body.removeChild(ta);
      }
      if (btnEl) {
        const old = btnEl.textContent;
        btnEl.textContent = "✓ 已复制";
        btnEl.classList.add("btn-copied");
        setTimeout(() => { btnEl.textContent = old; btnEl.classList.remove("btn-copied"); }, 1400);
      }
      showToast(label + " 已复制");
    }
    async function openPath(packageDir, selectFile, awemeId) {
      try {
        const r = await fetch("/api/open-folder", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ package_dir: packageDir, select_file: selectFile || "", aweme_id: awemeId || "" })
        });
        const d = await r.json();
        if (!r.ok) {
          showToast("⚠️ " + (d.error || "目录不存在，请刷新页面"));
          return;
        }
        if (d.package_dir && d.package_dir !== packageDir) {
          const c = cardsData.find(x => (awemeId && x.aweme_id === awemeId) || x.package_dir === packageDir);
          if (c) {
            c.package_dir = d.package_dir;
            if (d.video_path !== undefined) c.video_path = d.video_path;
            if (d.cover_path !== undefined) c.cover_path = d.cover_path;
            renderCards();
          }
        }
        showToast("已在访达/文件夹中为你定位");
      } catch (e) {
        showToast("⚠️ 打开失败：" + String(e));
      }
    }
    function toggleEditOutputDir(force) {
      const box = document.getElementById("output-dir-editor");
      const show = force !== undefined ? force : box.style.display === "none";
      box.style.display = show ? "flex" : "none";
      if (show) {
        document.getElementById("output-dir-input").value = document.getElementById("output-root-path").textContent || "";
      }
    }
    async function saveOutputDir() {
      const val = document.getElementById("output-dir-input").value.trim();
      if (!val) { showToast("请输入有效的文件夹路径"); return; }
      const r = await fetch("/api/save-output-dir", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ output_dir: val })
      });
      const d = await r.json();
      if (!r.ok) { showToast(d.error || "保存失败"); return; }
      document.getElementById("output-root-path").textContent = d.output_dir;
      cardsData = d.cards || [];
      refreshDateOptions();
      renderCards();
      toggleEditOutputDir(false);
      showToast("存放目录已更新");
    }
    async function pickOutputDir() {
      const r = await fetch("/api/pick-output-dir", { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" });
      const d = await r.json();
      if (!r.ok) { showToast(d.error || "选目录失败"); return; }
      if (d.cancelled) return;
      document.getElementById("output-root-path").textContent = d.output_dir;
      cardsData = d.cards || [];
      refreshDateOptions();
      renderCards();
      showToast("存放目录已改为：" + d.output_dir);
    }
    async function openOutputRoot() {
      await fetch("/api/open-output-root", { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" });
      showToast("已打开输出总目录");
    }
    async function checkUpdate() {
      const btn = document.getElementById("btn-check-update");
      const banner = document.getElementById("update-banner");
      btn.disabled = true;
      btn.textContent = "⏳ 检查中...";
      try {
        const r = await fetch("/api/check-update", { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" });
        const d = await r.json();
        banner.style.display = "";
        if (!r.ok) {
          banner.style.background = "var(--red-bg)";
          banner.style.borderColor = "var(--red-bd)";
          banner.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;"><span>⚠️ 检查更新失败：${esc(d.error || "未知错误")}</span><button class="btn" onclick="document.getElementById('update-banner').style.display='none'">关闭</button></div>`;
          return;
        }
        if (d.up_to_date) {
          banner.style.background = "var(--green-bg)";
          banner.style.borderColor = "var(--green-bd)";
          banner.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;"><span>✅ 已是最新版本（当前版本 <code>${esc(d.local_commit)}</code> · ${esc(d.via || "直连")}）</span><button class="btn" onclick="document.getElementById('update-banner').style.display='none'">关闭</button></div>`;
        } else {
          banner.style.background = "#eff6ff";
          banner.style.borderColor = "#bfdbfe";
          const logs = (d.changelog || []).map(x => `<li><code>${esc(x)}</code></li>`).join("");
          banner.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
              <div>
                <strong>🆕 发现 ${d.behind_count} 个更新</strong>（当前 <code>${esc(d.local_commit)}</code> → 最新 <code>${esc(d.remote_commit)}</code> · ${esc(d.via || "直连")}）
                ${logs ? `<ul style="margin:6px 0 0 18px;padding:0;font-size:12.5px;">${logs}</ul>` : ""}
              </div>
              <div style="display:flex;gap:8px;align-items:center;">
                <button class="btn btn-primary" id="btn-apply-update" onclick="applyUpdate()">⬇️ 一键更新并重启</button>
                <button class="btn" onclick="document.getElementById('update-banner').style.display='none'">稍后</button>
              </div>
            </div>
          `;
        }
      } catch (e) {
        banner.style.display = "";
        banner.innerHTML = `⚠️ 检查更新出错：${esc(String(e))}`;
      } finally {
        btn.disabled = false;
        btn.textContent = "🔄 检查更新";
      }
    }
    async function applyUpdate() {
      const btn = document.getElementById("btn-apply-update");
      const banner = document.getElementById("update-banner");
      if (btn) { btn.disabled = true; btn.textContent = "⏳ 正在拉取更新..."; }
      const r = await fetch("/api/apply-update", { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" });
      const d = await r.json();
      if (!r.ok) {
        banner.style.background = "var(--red-bg)";
        banner.style.borderColor = "var(--red-bd)";
        banner.innerHTML = `⚠️ 更新失败：${esc(d.error || "请尝试双击 更新代码.bat")}`;
        return;
      }
      if (!d.restarting) {
        showToast(d.message || "已是最新版本");
        checkUpdate();
        return;
      }
      banner.innerHTML = `⏳ 代码已更新，控制台正在自动重启，稍后将自动刷新页面...`;
      const waitTimer = setInterval(async () => {
        try {
          const hr = await fetch("/api/health");
          if (hr.ok) { clearInterval(waitTimer); location.reload(); }
        } catch {}
      }, 900);
    }
    function openPreview(videoPath, title) {
      const modal = document.getElementById("video-modal");
      const player = document.getElementById("preview-player");
      document.getElementById("preview-title").textContent = title || "视频预览";
      player.src = `/files?path=${encodeURIComponent(videoPath)}`;
      modal.classList.add("open");
      player.play().catch(() => {});
    }
    function closePreview() {
      const modal = document.getElementById("video-modal");
      const player = document.getElementById("preview-player");
      player.pause();
      player.removeAttribute("src");
      modal.classList.remove("open");
    }
    async function togglePub(awemeId, weiboName, nextState) {
      await fetch("/api/toggle-published", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ aweme_id: awemeId, weibo_name: weiboName, published: nextState })
      });
      const c = cardsData.find(x => x.aweme_id === awemeId);
      if (c) c.published = nextState;
      refreshDateOptions();
      renderCards();
    }
    function fmtDur(sec) {
      const s = Math.round(Number(sec) || 0);
      return String(Math.floor(s/60)).padStart(2,"0") + ":" + String(s%60).padStart(2,"0");
    }
    function statusEmoji(st) {
      return st === "green" ? "🟢" : st === "yellow" ? "🟡" : st === "red" ? "🔴" : "⚪";
    }
    function getTodayStr() {
      const dt = new Date();
      const y = dt.getFullYear();
      const m = String(dt.getMonth() + 1).padStart(2, "0");
      const d = String(dt.getDate()).padStart(2, "0");
      return `${y}-${m}-${d}`;
    }
    function onDateFilterChange() {
      dateFilterTouched = true;
      renderCards();
    }
    function refreshDateOptions(preferToday = false) {
      const sel = document.getElementById("date-filter");
      const today = getTodayStr();
      const prev = sel.value || today;
      const statsByDay = {};
      for (const c of cardsData) {
        const d = c.date_str || "";
        if (!d) continue;
        if (!statsByDay[d]) statsByDay[d] = { total: 0, unpub: 0 };
        statsByDay[d].total++;
        if (!c.published) statsByDay[d].unpub++;
      }
      if (!statsByDay[today]) {
        statsByDay[today] = { total: 0, unpub: 0 };
      }
      const dates = Object.keys(statsByDay).sort().reverse();
      sel.innerHTML =
        dates.map(d => {
          const st = statsByDay[d];
          const tag = d === today ? "今天 · " : "";
          return `<option value="${esc(d)}">${esc(d)}（${tag}共 ${st.total} 条 · 待发 ${st.unpub}）</option>`;
        }).join("") +
        `<option value="all">全部日期（共 ${cardsData.length} 条）</option>`;
      if (preferToday || !dateFilterTouched) {
        sel.value = today;
      } else if (dates.includes(prev) || prev === "all") {
        sel.value = prev;
      } else {
        sel.value = today;
      }
    }
    function renderCards() {
      const kw = (document.getElementById("search-box").value || "").trim().toLowerCase();
      const dateSel = document.getElementById("date-filter").value || "all";

      const dateScoped = cardsData.filter(c => {
        if (dateSel !== "all" && c.date_str !== dateSel) return false;
        if (kw) {
          const hay = `${c.weibo_name} ${c.author} ${c.title} ${c.desc}`.toLowerCase();
          if (!hay.includes(kw)) return false;
        }
        return true;
      });

      const counts = { all: dateScoped.length, unpublished: 0, green: 0, yellow: 0, red: 0 };
      for (const c of dateScoped) {
        if (!c.published) counts.unpublished++;
        if (counts[c.dedup_status] !== undefined) counts[c.dedup_status]++;
      }
      document.getElementById("wb-summary").textContent = `当前筛选 ${counts.all} 条 · 待发 ${counts.unpublished} 条`;

      const filters = [
        ["all", "全部 " + counts.all],
        ["unpublished", "待发布 " + counts.unpublished],
        ["green", "🟢 未发现 " + counts.green],
        ["yellow", "🟡 疑似 " + counts.yellow],
        ["red", "🔴 已发过 " + counts.red],
      ];
      document.getElementById("filter-bar").innerHTML = filters.map(([k, lbl]) =>
        `<button class="pill ${currentFilter===k ? "active":""}" onclick="currentFilter='${k}';renderCards();">${lbl}</button>`
      ).join("");

      const visible = dateScoped.filter(c => {
        if (currentFilter === "all") return true;
        if (currentFilter === "unpublished") return !c.published;
        return c.dedup_status === currentFilter;
      });

      const container = document.getElementById("cards-container");
      renderedCards = visible;
      const doSports = document.getElementById("opt-sports-tags").checked;
      if (!visible.length) {
        const dateSelNow = document.getElementById("date-filter").value || "all";
        const isTodayEmpty = dateSelNow === getTodayStr() && cardsData.length > 0 && currentFilter === "all" && !document.getElementById("search-box").value.trim();
        container.innerHTML = isTodayEmpty
          ? `<div style="padding:36px;text-align:center;color:#64748b;">今天（${esc(dateSelNow)}）暂无视频记录，可直接点击顶部「全部日期」或选择历史日期查看往期 ${cardsData.length} 条视频。</div>`
          : `<div style="padding:36px;text-align:center;color:#64748b;">当前筛选条件下暂无视频记录。</div>`;
      } else if (viewMode === "table") {
        container.innerHTML = `
          <div style="overflow-x:auto;">
            <table class="tbl">
              <thead>
                <tr>
                  <th>封面</th>
                  <th>微博号 / 日期</th>
                  <th>标题与规格</th>
                  <th>查重</th>
                  <th>快速找文件（配合创作罐头）</th>
                  <th>一键复制文案</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                ${visible.map((c, i) => {
                  const coverUrl = c.cover_path ? `/files?path=${encodeURIComponent(c.cover_path)}` : "";
                  const sportsDetected = isSportsCard(c);
                  const sportsActive = doSports && sportsDetected;
                  return `
                    <tr class="${c.published ? "published" : ""}">
                      <td>${coverUrl ? `<img class="thumb-sm" src="${coverUrl}" />` : "-"}</td>
                      <td>
                        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;">
                          <span class="wb-tag">${esc(c.weibo_name)}</span>
                          <button type="button" class="pill ${sportsDetected ? "status-green" : ""}" style="font-size:11px;padding:1px 6px;" title="点击可手动切换体育号/普通号" onclick="toggleCardSports(${i})">${sportsDetected ? "🏅 体育号" : "普通号"}</button>
                        </div>
                        <div class="sub" style="margin-top:4px;">${esc(c.date_str)} · ${fmtDur(c.duration_sec)}</div>
                      </td>
                      <td style="max-width:280px;">
                        <div style="font-weight:600;margin-bottom:3px;">${esc(c.title)}</div>
                        <div class="sub">${esc(c.resolution_info)} · ${c.video_size_mb ? c.video_size_mb + " MB" : "未下载"} · <span style="color:${(c.title||"").length>30 ? "#dc2626" : "#64748b"};font-weight:600;">${(c.title||"").length}/30字</span></div>
                      </td>
                      <td>
                        <span class="pill status-${esc(c.dedup_status)}" style="cursor:default;">${statusEmoji(c.dedup_status)} ${esc(c.dedup_label)}</span>
                      </td>
                      <td>
                        <div style="display:flex;gap:5px;flex-wrap:wrap;">
                          ${c.video_path ? `<button class="btn btn-primary" onclick='openPath(${JSON.stringify(c.package_dir)}, ${JSON.stringify(c.video_path)}, ${JSON.stringify(c.aweme_id)})'>📁 定位视频</button>` : ""}
                          ${c.cover_path ? `<button class="btn" onclick='openPath(${JSON.stringify(c.package_dir)}, ${JSON.stringify(c.cover_path)}, ${JSON.stringify(c.aweme_id)})'>🖼 封面</button>` : ""}
                          ${c.video_path ? `<button class="btn btn-soft" onclick='copyText(${JSON.stringify(c.video_path)}, "视频路径", this)'>📋 视频路径</button>` : ""}
                          ${c.video_path ? `<button class="btn" onclick='openPreview(${JSON.stringify(c.video_path)}, ${JSON.stringify(c.title)})'>▶</button>` : ""}
                        </div>
                      </td>
                      <td>
                        <div style="display:flex;gap:5px;flex-wrap:wrap;">
                          <button class="btn btn-soft" onclick='copyText(${JSON.stringify(c.title)}, "标题", this)'>复制标题</button>
                          ${(c.title||"").length > 30 ? `<button class="btn" style="color:#dc2626;" onclick='copyText(${JSON.stringify((c.title||"").slice(0,30))}, "前30字标题", this)'>✂️ 30字</button>` : ""}
                          <button class="btn" style="${sportsActive ? "border-color:#86efac;background:#f0fdf4;color:#15803d;" : ""}" onclick="copyCardDesc(${i}, this)">${sportsActive ? "🏅 复制体育简介" : "复制简介"}</button>
                        </div>
                      </td>
                      <td>
                        <button class="pub-btn ${c.published ? "done" : ""}" onclick='togglePub(${JSON.stringify(c.aweme_id)}, ${JSON.stringify(c.weibo_name)}, ${!c.published})'>
                          ${c.published ? "☑ 已发布" : "☐ 标为已发"}
                        </button>
                      </td>
                    </tr>
                  `;
                }).join("")}
              </tbody>
            </table>
          </div>
        `;
      } else {
        container.innerHTML = `<div class="grid">${visible.map((c, i) => {
          const coverUrl = c.cover_path ? `/files?path=${encodeURIComponent(c.cover_path)}` : "";
          const sportsDetected = isSportsCard(c);
          const sportsActive = doSports && sportsDetected;
          const formattedPreview = formatWeiboDesc(c);
          const evHtml = (c.dedup_evidences || []).slice(0,2).map(ev =>
            `<div>• ${ev.is_own_account ? "[自家号]" : "[外部]"} <b>${esc(ev.author)}</b> (${Math.round(ev.similarity*100)}%) ${ev.post_url ? `<a href="${esc(ev.post_url)}" target="_blank">查看</a>` : ""}</div>`
          ).join("");
          const manualHtml = (c.manual_search_urls || []).slice(0,2).map(u =>
            `<a href="${esc(u.video_url)}" target="_blank">搜视频: ${esc(u.query)}</a><a href="${esc(u.all_url)}" target="_blank">搜综合</a>`
          ).join("");
          const shortFolder = c.package_dir ? c.package_dir.split(/[\\/]/).slice(-2).join(" / ") : "";
          return `
            <div class="card ${c.published ? "published" : ""}">
              <div class="cover-wrap">
                ${coverUrl ? `<img src="${coverUrl}" alt="cover"/>` : `<div style="color:#94a3b8;font-size:13px;">未下载（仅查重或已跳过）</div>`}
                <span class="badge-top status-${esc(c.dedup_status)}">${statusEmoji(c.dedup_status)} ${esc(c.dedup_label)}</span>
                ${c.video_path ? `<button class="preview-fab" onclick='openPreview(${JSON.stringify(c.video_path)}, ${JSON.stringify(c.title)})'>▶ 预览视频</button>` : ""}
                <span class="badge-dur">${fmtDur(c.duration_sec)}</span>
              </div>
              <div class="body">
                <div class="meta-row">
                  <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                    <span class="wb-tag">微博：${esc(c.weibo_name)}</span>
                    <button type="button" class="pill ${sportsDetected ? "status-green" : ""}" style="font-size:11px;padding:2px 7px;" title="点击可手动切换体育号/普通号" onclick="toggleCardSports(${i})">${sportsDetected ? "🏅 体育号·已换标签" : "普通号（点此切体育）"}</button>
                  </div>
                  <span>${esc(c.author)} · ${esc(c.publish_time)}</span>
                </div>
                <div class="meta-row">
                  <span>规格：${esc(c.resolution_info)}</span>
                  <span>${c.video_size_mb ? c.video_size_mb + " MB" : ""}</span>
                </div>

                ${c.package_dir ? `
                  <div class="file-bar">
                    <div class="file-path-hint">📂 ${esc(shortFolder)}</div>
                    <div class="file-btns">
                      ${c.video_path ? `<button class="btn btn-primary" onclick='openPath(${JSON.stringify(c.package_dir)}, ${JSON.stringify(c.video_path)}, ${JSON.stringify(c.aweme_id)})'>📁 定位视频</button>` : ""}
                      ${c.cover_path ? `<button class="btn" onclick='openPath(${JSON.stringify(c.package_dir)}, ${JSON.stringify(c.cover_path)}, ${JSON.stringify(c.aweme_id)})'>🖼 定位封面</button>` : ""}
                      ${c.video_path ? `<button class="btn btn-soft" onclick='copyText(${JSON.stringify(c.video_path)}, "视频路径", this)'>📋 复制视频路径</button>` : ""}
                      <button class="btn" onclick='copyText(${JSON.stringify(c.package_dir)}, "文件夹路径", this)'>📁 复制目录</button>
                    </div>
                  </div>
                ` : ""}

                <div class="dedup-box">
                  <div><b>查重：</b>${esc(c.dedup_summary)}</div>
                  ${evHtml}
                  ${manualHtml ? `<div class="dedup-links">${manualHtml}</div>` : ""}
                </div>

                <div>
                  <div class="field-label">
                    <span>标题 <span style="color:${(c.title||"").length>30 ? "#dc2626" : "#64748b"};">（${(c.title||"").length}/30字${(c.title||"").length>30 ? " ⚠️超长" : ""}）</span></span>
                    <div style="display:flex; gap:5px;">
                      ${(c.title||"").length > 30 ? `<button class="btn" style="color:#dc2626;" onclick='copyText(${JSON.stringify((c.title||"").slice(0,30))},"前30字标题",this)'>✂️ 复制前30字</button>` : ""}
                      <button class="btn btn-soft" onclick='copyText(${JSON.stringify(c.title)},"标题",this)'>📋 复制标题</button>
                    </div>
                  </div>
                  <div class="text-box">${esc(c.title)}</div>
                </div>
                <div>
                  <div class="field-label"><span>${sportsActive ? "简介（🏅 已识别体育号：复制时全替换标签）" : "简介（点击复制会自动按上方规则去抖音尾标）"}</span><button class="btn" style="${sportsActive ? "border-color:#86efac;background:#f0fdf4;color:#15803d;" : ""}" onclick="copyCardDesc(${i}, this)">📋 ${sportsActive ? "复制简介（体育标签）" : "复制简介"}</button></div>
                  <div class="text-box">${esc(sportsActive ? formattedPreview : c.desc)}</div>
                </div>

                <div class="footer-actions">
                  <a class="btn" href="${esc(c.video_page_url)}" target="_blank">原抖音链接</a>
                  <button class="pub-btn ${c.published ? "done" : ""}" onclick='togglePub(${JSON.stringify(c.aweme_id)}, ${JSON.stringify(c.weibo_name)}, ${!c.published})'>
                    ${c.published ? "☑ 已发布（点此撤销）" : "☐ 标为已发布"}
                  </button>
                </div>
              </div>
            </div>
          `;
        }).join("")}</div>`;
      }

      const fbox = document.getElementById("failures-box");
      fbox.innerHTML = failuresData.length ? `
        <div style="background:var(--red-bg);border:1px solid var(--red-bd);color:var(--red-fg);border-radius:10px;padding:10px 14px;font-size:13px;">
          <b>注意：有 ${failuresData.length} 条未成功：</b>
          <ul style="margin:6px 0 0 18px;padding:0;">${failuresData.map(f => `<li>${esc(f.weibo_name || f.url)}：${esc(f.error)}</li>`).join("")}</ul>
        </div>
      ` : "";
    }

    async function startRun() {
      const selectedLines = [];
      if (mode === "batch") {
        document.querySelectorAll(".acc-chk:checked").forEach(el => {
          const acc = parsedAccounts[Number(el.dataset.idx)];
          if (acc) selectedLines.push(acc.raw_line);
        });
      }
      const payload = {
        mode,
        paste_text: document.getElementById("paste-links-box").value,
        default_weibo: document.getElementById("paste-default-weibo").value.trim(),
        default_uid: document.getElementById("paste-default-uid").value.trim(),
        selected_lines: selectedLines,
        dedup_preset: document.getElementById("opt-dedup").value,
        lookback_days: Number(document.getElementById("opt-lookback").value),
        latest_per_profile: Number(document.getElementById("opt-latest").value),
        transcode_profile: document.getElementById("opt-transcode-profile").value,
        copy_if_1080p: document.getElementById("opt-copy-1080p").checked,
        dedup_only: document.getElementById("opt-dedup-only").checked,
        overwrite: document.getElementById("opt-overwrite").checked,
      };
      const r = await fetch("/api/run", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const d = await r.json();
      if (!r.ok) { showToast(d.error || "启动失败"); return; }
      document.getElementById("btn-run").disabled = true;
      document.getElementById("btn-run").textContent = "⏳ 正在处理...";
      const stopBtn = document.getElementById("btn-stop-dedup");
      stopBtn.disabled = false;
      stopBtn.textContent = "⏭️ 停止查重并直接处理";
      stopBtn.style.display = "inline-flex";
      startPolling();
    }

    async function stopDedup() {
      const stopBtn = document.getElementById("btn-stop-dedup");
      stopBtn.disabled = true;
      stopBtn.textContent = "⏳ 正在跳过查重...";
      const r = await fetch("/api/stop-dedup", { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" });
      if (r.ok) showToast("已停止查重，马上进入下载与 1080P 转码");
    }

    function startPolling() {
      clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        const r = await fetch("/api/status");
        const st = await r.json();
        const logEl = document.getElementById("log-box");
        if (st.logs && st.logs.length) {
          logEl.textContent = st.logs.join("\\n");
          logEl.scrollTop = logEl.scrollHeight;
        }
        if (st.cards && st.cards.length) {
          cardsData = st.cards;
          refreshDateOptions();
        }
        failuresData = st.failures || [];
        renderCards();
        const stopBtn = document.getElementById("btn-stop-dedup");
        const canStopDedup = st.running && (st.phase === "fetching" || st.phase === "dedup");
        stopBtn.style.display = canStopDedup ? "inline-flex" : "none";
        if (!st.running) {
          clearInterval(pollTimer);
          document.getElementById("btn-run").disabled = false;
          document.getElementById("btn-run").textContent = "🚀 开始处理";
          refreshHealth();
        }
      }, 700);
    }

    async function refreshHealth() {
      const r = await fetch("/api/health");
      const h = await r.json();
      const dyEl = document.getElementById("dy-login-badge");
      const wbEl = document.getElementById("wb-login-badge");
      dyEl.textContent = h.douyin_cookie_ok ? "抖音：✅ 已登录" : "抖音：❌ 未登录";
      dyEl.className = "pill " + (h.douyin_cookie_ok ? "status-green" : "status-red");
      wbEl.textContent = h.weibo_cookie_ok ? "微博查重：✅ 已登录" : "微博查重：⚠️ 未登录";
      wbEl.className = "pill " + (h.weibo_cookie_ok ? "status-green" : "status-yellow");
    }

    async function startLogin(platform) {
      const r = await fetch("/api/login", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ platform })
      });
      const d = await r.json();
      if (!r.ok) { showToast(d.error || "无法打开登录窗口"); return; }
      runnerCollapsed = false;
      applyRunnerCollapse();
      document.getElementById("btn-run").disabled = true;
      showToast("请在弹出的浏览器窗口里扫码登录");
      startPolling();
    }

    async function init() {
      applyRunnerCollapse();
      loadCleanOpts();
      refreshHealth();
      document.getElementById("btn-view-card").classList.toggle("btn-primary", viewMode === "card");
      document.getElementById("btn-view-table").classList.toggle("btn-primary", viewMode === "table");
      const r = await fetch("/api/init");
      const d = await r.json();
      if (Array.isArray(d.strip_tag_keywords) && d.strip_tag_keywords.length) {
        stripTagKeywords = d.strip_tag_keywords;
      }
      if (Array.isArray(d.sports_account_keywords) && d.sports_account_keywords.length) {
        sportsAccountKeywords = d.sports_account_keywords;
      }
      if (typeof d.sports_replace_tags === "string" && d.sports_replace_tags.trim()) {
        sportsReplaceTags = d.sports_replace_tags.trim();
      }
      document.getElementById("output-root-path").textContent = d.output_dir || "";
      document.getElementById("raw-tasks-text").value = d.raw_tasks || "";
      parsedAccounts = d.accounts || [];
      cardsData = d.cards || [];
      renderAccounts();
      refreshDateOptions(true);
      renderCards();
      if (d.running) startPolling();
    }
    init();
  </script>
</body>
</html>
"""


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(body.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            raw = _HTML_PAGE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if parsed.path == "/api/health":
            import shutil
            cfg = load_config()
            ffmpeg_ok = Path(cfg.ffmpeg_path).is_file() or bool(shutil.which(cfg.ffmpeg_path))
            ffprobe_ok = Path(cfg.ffprobe_path).is_file() or bool(shutil.which(cfg.ffprobe_path))
            commit = ""
            if (cfg.project_root / ".git").exists():
                try:
                    c_res = run_git(["rev-parse", "--short", "HEAD"], cfg.project_root, timeout=5)
                    if c_res.returncode == 0:
                        commit = c_res.stdout.strip()
                except Exception:
                    pass
            self._send_json({
                "ok": ffmpeg_ok and ffprobe_ok and cfg.douyin_cookie_path.is_file(),
                "platform": sys.platform,
                "python": sys.version.split()[0],
                "commit": commit,
                "ffmpeg_path": cfg.ffmpeg_path,
                "ffmpeg_ok": ffmpeg_ok,
                "ffprobe_ok": ffprobe_ok,
                "douyin_cookie_ok": cfg.douyin_cookie_path.is_file(),
                "weibo_cookie_ok": cfg.weibo_cookie_path.is_file(),
                "output_dir": str(cfg.output_dir),
                "log_path": str(cfg.project_root / "logs" / "app.log"),
            })
            return

        if parsed.path == "/api/init":
            cfg = load_config()
            tasks_file = cfg.project_root / "待发.txt"
            raw_tasks = tasks_file.read_text(encoding="utf-8-sig") if tasks_file.is_file() else ""
            accounts = []
            for ln in raw_tasks.splitlines():
                t = parse_line(ln)
                if t:
                    accounts.append({
                        "douyin_url": t.douyin_url,
                        "weibo_name": t.weibo_name,
                        "weibo_uid": t.weibo_uid,
                        "douyin_name": t.douyin_name,
                        "raw_line": t.raw_line,
                    })
            disk_cards = _load_existing_cards()
            with _STATE_LOCK:
                running = _APP_STATE["running"]
                session_cards = list(_APP_STATE.get("session_cards") or [])
                cards = _merge_cards(session_cards, disk_cards)
                _APP_STATE["cards"] = cards
            self._send_json({
                "output_dir": str(cfg.output_dir),
                "strip_tag_keywords": cfg.strip_tag_keywords,
                "sports_account_keywords": cfg.sports_account_keywords,
                "sports_replace_tags": cfg.sports_replace_tags,
                "raw_tasks": raw_tasks,
                "accounts": accounts,
                "cards": cards,
                "running": running,
            })
            return

        if parsed.path == "/api/status":
            with _STATE_LOCK:
                self._send_json(dict(_APP_STATE))
            return

        if parsed.path == "/files":
            qs = urllib.parse.parse_qs(parsed.query)
            req_path = Path((qs.get("path") or [""])[0]).resolve()
            cfg = load_config()
            try:
                req_path.relative_to(cfg.output_dir.resolve())
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not req_path.is_file():
                live_dir = _resolve_live_package_dir(cfg.output_dir, req_path.parent)
                if live_dir and (live_dir / req_path.name).is_file():
                    req_path = (live_dir / req_path.name).resolve()
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
            mime, _ = mimetypes.guess_type(str(req_path))
            file_size = req_path.stat().st_size
            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                spec = range_hdr.split("=", 1)[1].split(",")[0].strip()
                start_s, end_s = spec.split("-", 1)
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else file_size - 1
                end = min(end, file_size - 1)
                length = max(0, end - start + 1)
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                with req_path.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(262144, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with req_path.open("rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_json_body()

        if parsed.path == "/api/save-tasks":
            cfg = load_config()
            tasks_file = cfg.project_root / "待发.txt"
            text = str(body.get("text") or "")
            tasks_file.write_text(text, encoding="utf-8")
            accounts = []
            for ln in text.splitlines():
                t = parse_line(ln)
                if t:
                    accounts.append({
                        "douyin_url": t.douyin_url,
                        "weibo_name": t.weibo_name,
                        "weibo_uid": t.weibo_uid,
                        "douyin_name": t.douyin_name,
                        "raw_line": t.raw_line,
                    })
            self._send_json({"accounts": accounts})
            return

        if parsed.path == "/api/run":
            with _STATE_LOCK:
                if _APP_STATE["running"]:
                    self._send_json({"error": "当前已有任务在运行中"}, status=409)
                    return
                _STOP_DEDUP_EVENT.clear()
                _APP_STATE["running"] = True
                _APP_STATE["phase"] = "fetching"
                _APP_STATE["logs"] = ["已启动处理任务..."]
                _APP_STATE["failures"] = []
                _APP_STATE["summary"] = None
            th = threading.Thread(target=_run_worker, args=(body,), daemon=True)
            th.start()
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/stop-dedup":
            with _STATE_LOCK:
                running = _APP_STATE["running"]
            if not running:
                self._send_json({"ok": True, "message": "当前没有正在运行的任务"})
                return
            _STOP_DEDUP_EVENT.set()
            _append_log("收到指令：停止剩余微博查重，马上直接进入下载与转码...")
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/login":
            platform = str(body.get("platform") or "")
            if platform not in {"douyin", "weibo"}:
                self._send_json({"error": "不支持的平台"}, status=400)
                return
            with _STATE_LOCK:
                if _APP_STATE["running"]:
                    self._send_json({"error": "当前有任务正在运行，请稍后再登录"}, status=409)
                    return
                _APP_STATE["running"] = True
                _APP_STATE["logs"] = [f"正在打开可见浏览器窗口，请扫码登录 {platform}..."]
            th = threading.Thread(target=_login_worker, args=(platform,), daemon=True)
            th.start()
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/save-output-dir":
            new_dir = str(body.get("output_dir") or "").strip()
            try:
                resolved = save_output_dir_to_config(new_dir)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            with _STATE_LOCK:
                if not _APP_STATE["running"]:
                    _APP_STATE["cards"] = []
                    _APP_STATE["session_cards"] = []
            self._send_json({"ok": True, "output_dir": str(resolved), "cards": _load_existing_cards()})
            return

        if parsed.path == "/api/pick-output-dir":
            cfg = load_config()
            initial = cfg.output_dir if cfg.output_dir.is_dir() else Path.home()
            try:
                chosen = _pick_native_folder(initial)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            if not chosen:
                self._send_json({"ok": True, "cancelled": True})
                return
            resolved = save_output_dir_to_config(chosen)
            with _STATE_LOCK:
                if not _APP_STATE["running"]:
                    _APP_STATE["cards"] = []
                    _APP_STATE["session_cards"] = []
            self._send_json({"ok": True, "output_dir": str(resolved), "cards": _load_existing_cards()})
            return

        if parsed.path == "/api/check-update":
            cfg = load_config()
            try:
                info = check_for_updates(cfg.project_root, cfg.github_mirrors)
                self._send_json(info)
            except RuntimeError as exc:
                _get_file_logger().warning("POST /api/check-update failed: %s", exc)
                self._send_json({"error": str(exc)}, status=400)
            except Exception as exc:
                _get_file_logger().error("POST /api/check-update error: %s", exc)
                self._send_json({"error": str(exc)}, status=500)
            return

        if parsed.path == "/api/apply-update":
            with _STATE_LOCK:
                if _APP_STATE["running"]:
                    self._send_json({"error": "当前有下载或登录任务在运行，请等完成后再更新"}, status=409)
                    return
            cfg = load_config()
            try:
                res = apply_update(cfg.project_root, cfg.github_mirrors)
                _get_file_logger().info("POST /api/apply-update: %s", res.get("message"))
                self._send_json(res)
                if res.get("restarting"):
                    _schedule_self_restart()
            except RuntimeError as exc:
                _get_file_logger().warning("POST /api/apply-update failed: %s", exc)
                self._send_json({"error": str(exc)}, status=400)
            except Exception as exc:
                _get_file_logger().error("POST /api/apply-update error: %s", exc)
                self._send_json({"error": str(exc)}, status=500)
            return

        if parsed.path == "/api/open-output-root":
            cfg = load_config()
            cfg.output_dir.mkdir(parents=True, exist_ok=True)
            _open_native_path(cfg.output_dir)
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/open-folder":
            cfg = load_config()
            pkg_dir = Path(str(body.get("package_dir") or "")).resolve()
            sel_raw = str(body.get("select_file") or body.get("video_path") or "")
            sel_path = Path(sel_raw).resolve() if sel_raw else None
            aweme_id = str(body.get("aweme_id") or "")
            try:
                pkg_dir.relative_to(cfg.output_dir.resolve())
            except ValueError:
                self._send_json({"error": "非法路径"}, status=403)
                return
            live_dir = _resolve_live_package_dir(cfg.output_dir, pkg_dir, aweme_id=aweme_id)
            if not live_dir or not live_dir.is_dir():
                msg = f"目录不存在（可能已被移动或删除）：{pkg_dir.name}，请刷新页面"
                _get_file_logger().warning("POST /api/open-folder 404: req=%s aweme_id=%s", pkg_dir, aweme_id)
                self._send_json({"error": msg}, status=404)
                return
            live_sel = (live_dir / sel_path.name) if sel_path else None
            _get_file_logger().info("POST /api/open-folder ok: dir=%s select=%s", live_dir, live_sel)
            _open_native_path(live_dir, live_sel)
            live_video = live_dir / "video_1080p.mp4"
            live_cover = live_dir / "cover.jpg"
            self._send_json({
                "ok": True,
                "package_dir": str(live_dir),
                "video_path": str(live_video) if live_video.is_file() else "",
                "cover_path": str(live_cover) if live_cover.is_file() else "",
            })
            return

        if parsed.path == "/api/toggle-published":
            cfg = load_config()
            aweme_id = str(body.get("aweme_id") or "")
            wb_name = str(body.get("weibo_name") or "")
            pub = bool(body.get("published"))
            ledger = _load_ledger(cfg.ledger_path)
            entry = ledger.setdefault("videos", {}).setdefault(aweme_id, {"aweme_id": aweme_id, "weibo_name": wb_name})
            entry["published"] = pub
            entry["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _save_ledger(cfg.ledger_path, ledger)
            self._send_json({"ok": True})
            return

        self.send_error(HTTPStatus.NOT_FOUND)


def _find_free_port(start: int = 8765) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def main() -> None:
    parser = argparse.ArgumentParser(description="douyin2weibo 本地可视化控制台")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    port = args.port or _find_free_port(8765)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)
    _SERVER_REF["server"] = server
    _SERVER_REF["port"] = port
    url = f"http://127.0.0.1:{port}/"
    try:
        _get_file_logger().info("ui_server started on %s (platform=%s, python=%s)", url, sys.platform, sys.version.split()[0])
    except Exception:
        pass
    print(f"douyin2weibo 可视化控制台已启动：{url}")
    print("保持此窗口开启即可在浏览器中操作；关闭本窗口即退出服务。")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出控制台。")


if __name__ == "__main__":
    main()
