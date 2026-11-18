"""Foolproof local Web GUI server for douyin2weibo (zero CLI commands needed)."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import load_config, save_output_dir_to_config
from .cookie_login import login_and_save_cookie
from .douyin_fetcher import CaptchaRequiredError
from .input_parser import TaskInput, parse_line
from .main import _load_ledger, _save_ledger, execute_pipeline

_STATE_LOCK = threading.Lock()
_SERVER_REF: dict[str, Any] = {"server": None, "port": 8765}
_APP_STATE: dict[str, Any] = {
    "running": False,
    "logs": [],
    "cards": [],
    "failures": [],
    "summary": None,
}


def _load_existing_cards() -> list[dict[str, Any]]:
    cfg = load_config()
    ledger = _load_ledger(cfg.ledger_path)
    videos_ledger = ledger.get("videos") or {}
    cards: list[dict[str, Any]] = []
    if not cfg.output_dir.is_dir():
        return cards
    meta_files = sorted(cfg.output_dir.glob("*/*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for mf in meta_files[:120]:
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        pkg_dir = mf.parent
        date_dir = pkg_dir.parent.name
        cover_path = pkg_dir / "cover.jpg"
        video_path = pkg_dir / "video_1080p.mp4"
        aweme_id = str(meta.get("aweme_id") or "")
        dr = meta.get("dedup") or {}
        src_res = meta.get("source_resolution") or ""
        out_res = meta.get("output_resolution") or ""
        res_info = f"{src_res} -> {out_res}".strip(" ->")
        is_pub = bool((videos_ledger.get(aweme_id) or {}).get("published", False))
        cards.append({
            "aweme_id": aweme_id,
            "date_str": date_dir,
            "weibo_name": meta.get("weibo_name") or meta.get("author") or "",
            "weibo_uid": meta.get("weibo_uid") or "",
            "author": meta.get("author") or "",
            "publish_time": meta.get("publish_time") or "",
            "duration_sec": meta.get("duration_sec") or 0,
            "resolution_info": res_info,
            "video_size_mb": meta.get("video_size_mb"),
            "title": meta.get("title") or "",
            "desc": meta.get("desc") or "",
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


def _append_log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with _STATE_LOCK:
        _APP_STATE["logs"].append(f"[{ts}] {msg}")
        if len(_APP_STATE["logs"]) > 400:
            _APP_STATE["logs"] = _APP_STATE["logs"][-400:]


def _open_native_path(folder_path: Path, select_file: Path | None = None) -> None:
    if sys.platform == "darwin":
        if select_file and select_file.is_file():
            subprocess.Popen(["open", "-R", str(select_file)])
        else:
            subprocess.Popen(["open", str(folder_path)])
    elif sys.platform.startswith("win"):
        if select_file and select_file.is_file():
            subprocess.Popen(["explorer", f"/select,{select_file}"])
        else:
            subprocess.Popen(["explorer", str(folder_path)])
    else:
        subprocess.Popen(["xdg-open", str(folder_path)])


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
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
        out = (res.stdout or "").strip().rstrip("/")
        return out or None
    if sys.platform.startswith("win"):
        escaped = str(initial_dir).replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description = '请选择解析视频存放目录';"
            f"$d.SelectedPath = '{escaped}';"
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
        )
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120)
        out = (res.stdout or "").strip()
        return out or None
    raise RuntimeError("当前系统不支持弹窗选目录，请直接点「改路径」输入文件夹路径。")


def _run_git(args: list[str], cwd: Path, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


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
        cfg.transcode.crf = int(payload.get("crf", 18))

        tasks: list[TaskInput] = []
        if payload.get("mode") == "single":
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

        res = asyncio.run(
            execute_pipeline(
                cfg,
                tasks,
                dedup_only=bool(payload.get("dedup_only", False)),
                force_download=bool(payload.get("force_download", False)),
                overwrite=bool(payload.get("overwrite", False)),
                log_fn=_append_log,
            )
        )
        today_str = datetime.date.today().isoformat()
        for c in res["cards"]:
            c.setdefault("date_str", today_str)
        with _STATE_LOCK:
            _APP_STATE["cards"] = res["cards"]
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
        _append_log(f"[已暂停] {exc}")
        with _STATE_LOCK:
            _APP_STATE["failures"] = [{"url": "-", "weibo_name": "风控/验证码", "error": str(exc)}]
    except Exception as exc:
        _append_log(f"[出错] {exc}")
        with _STATE_LOCK:
            _APP_STATE["failures"] = [{"url": "-", "weibo_name": "运行错误", "error": str(exc)}]
    finally:
        with _STATE_LOCK:
            _APP_STATE["running"] = False


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
        <button class="tab active" id="tab-batch" onclick="switchMode('batch')">批量账号列表</button>
        <button class="tab" id="tab-single" onclick="switchMode('single')">单条链接搬运</button>
      </div>

      <div id="pane-batch">
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

      <div id="pane-single" style="display:none;">
        <div class="row">
          <label class="field" style="grid-column: span 2;">
            抖音链接（作品链接、主页链接或分享口令）
            <input type="text" id="single-url" placeholder="例如：https://v.douyin.com/heFr3knB71o/" />
          </label>
          <label class="field">
            对应微博号名（可选）
            <input type="text" id="single-weibo" placeholder="例如：洛鸢三角洲行动" />
          </label>
          <label class="field">
            微博 UID（可选，用于查重）
            <input type="text" id="single-uid" placeholder="例如：8310157614" />
          </label>
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
          1080P 画质
          <select id="opt-crf">
            <option value="18" selected>超清原画级（CRF 18）</option>
            <option value="20">均衡省体积（CRF 20）</option>
          </select>
        </label>
      </div>

      <div class="actions-bar">
        <div style="display:flex; gap:14px; flex-wrap:wrap; font-size:13px;">
          <label style="display:inline-flex; align-items:center; gap:5px; cursor:pointer;">
            <input type="checkbox" id="opt-dedup-only" /> 仅查重不下载
          </label>
          <label style="display:inline-flex; align-items:center; gap:5px; cursor:pointer;">
            <input type="checkbox" id="opt-overwrite" /> 强制重新转码覆盖
          </label>
        </div>
        <button class="btn btn-primary btn-lg" id="btn-run" onclick="startRun()">🚀 开始处理</button>
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
          <select class="date-select" id="date-filter" onchange="renderCards()">
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
            <input type="checkbox" id="opt-clean-tags" checked onchange="saveCleanOpts()" />
            自动去抖音尾标
          </label>
          <input type="text" id="opt-extra-tags" placeholder="附加微博话题（可选，如 #三角洲行动[超话]#）" style="width:255px; padding:4px 8px; font-size:12px;" oninput="saveCleanOpts()" />
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
    let mode = "batch";
    let viewMode = localStorage.getItem("d2w_view_mode") || "card";
    let runnerCollapsed = localStorage.getItem("d2w_runner_collapsed") === "1";
    let stripTagKeywords = ["抖音精选", "上抖音", "来抖音", "抖音小助手", "青年创作者成长计划", "氪金兽"];
    let parsedAccounts = [];
    let cardsData = [];
    let failuresData = [];
    let currentFilter = "all";
    let pollTimer = null;

    function loadCleanOpts() {
      const cleanOn = localStorage.getItem("d2w_clean_tags");
      document.getElementById("opt-clean-tags").checked = cleanOn === null ? true : cleanOn === "1";
      document.getElementById("opt-extra-tags").value = localStorage.getItem("d2w_extra_tags") || "";
    }
    function saveCleanOpts() {
      localStorage.setItem("d2w_clean_tags", document.getElementById("opt-clean-tags").checked ? "1" : "0");
      localStorage.setItem("d2w_extra_tags", document.getElementById("opt-extra-tags").value || "");
    }
    function formatWeiboDesc(rawDesc) {
      let text = String(rawDesc || "").trim();
      const doClean = document.getElementById("opt-clean-tags").checked;
      const extra = (document.getElementById("opt-extra-tags").value || "").trim();
      if (doClean) {
        const tags = text.match(/#[^\\s#]+/g) || [];
        let body = text.replace(/#[^\\s#]+/g, "").replace(/\\s+/g, " ").trim();
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
      document.getElementById("tab-batch").classList.toggle("active", m === "batch");
      document.getElementById("tab-single").classList.toggle("active", m === "single");
      document.getElementById("pane-batch").style.display = m === "batch" ? "" : "none";
      document.getElementById("pane-single").style.display = m === "single" ? "" : "none";
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
    async function openPath(packageDir, selectFile) {
      await fetch("/api/open-folder", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ package_dir: packageDir, select_file: selectFile || "" })
      });
      showToast("已在访达/文件夹中为你定位");
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
          banner.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;"><span>✅ 已是最新版本（当前版本 <code>${esc(d.local_commit)}</code>）</span><button class="btn" onclick="document.getElementById('update-banner').style.display='none'">关闭</button></div>`;
        } else {
          banner.style.background = "#eff6ff";
          banner.style.borderColor = "#bfdbfe";
          const logs = (d.changelog || []).map(x => `<li><code>${esc(x)}</code></li>`).join("");
          banner.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
              <div>
                <strong>🆕 发现 ${d.behind_count} 个更新</strong>（当前 <code>${esc(d.local_commit)}</code> → 最新 <code>${esc(d.remote_commit)}</code>）
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
      renderCards();
    }
    function fmtDur(sec) {
      const s = Math.round(Number(sec) || 0);
      return String(Math.floor(s/60)).padStart(2,"0") + ":" + String(s%60).padStart(2,"0");
    }
    function statusEmoji(st) {
      return st === "green" ? "🟢" : st === "yellow" ? "🟡" : st === "red" ? "🔴" : "⚪";
    }
    function refreshDateOptions() {
      const sel = document.getElementById("date-filter");
      const prev = sel.value || "all";
      const dates = Array.from(new Set(cardsData.map(c => c.date_str).filter(Boolean))).sort().reverse();
      sel.innerHTML = `<option value="all">全部日期（${cardsData.length}）</option>` +
        dates.map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join("");
      if (dates.includes(prev) || prev === "all") sel.value = prev;
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
      if (!visible.length) {
        container.innerHTML = `<div style="padding:36px;text-align:center;color:#64748b;">当前筛选条件下暂无视频记录。</div>`;
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
                ${visible.map(c => {
                  const coverUrl = c.cover_path ? `/files?path=${encodeURIComponent(c.cover_path)}` : "";
                  return `
                    <tr class="${c.published ? "published" : ""}">
                      <td>${coverUrl ? `<img class="thumb-sm" src="${coverUrl}" />` : "-"}</td>
                      <td>
                        <div><span class="wb-tag">${esc(c.weibo_name)}</span></div>
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
                          ${c.video_path ? `<button class="btn btn-primary" onclick='openPath(${JSON.stringify(c.package_dir)}, ${JSON.stringify(c.video_path)})'>📁 定位视频</button>` : ""}
                          ${c.cover_path ? `<button class="btn" onclick='openPath(${JSON.stringify(c.package_dir)}, ${JSON.stringify(c.cover_path)})'>🖼 封面</button>` : ""}
                          ${c.video_path ? `<button class="btn btn-soft" onclick='copyText(${JSON.stringify(c.video_path)}, "视频路径", this)'>📋 视频路径</button>` : ""}
                          ${c.video_path ? `<button class="btn" onclick='openPreview(${JSON.stringify(c.video_path)}, ${JSON.stringify(c.title)})'>▶</button>` : ""}
                        </div>
                      </td>
                      <td>
                        <div style="display:flex;gap:5px;flex-wrap:wrap;">
                          <button class="btn btn-soft" onclick='copyText(${JSON.stringify(c.title)}, "标题", this)'>复制标题</button>
                          ${(c.title||"").length > 30 ? `<button class="btn" style="color:#dc2626;" onclick='copyText(${JSON.stringify((c.title||"").slice(0,30))}, "前30字标题", this)'>✂️ 30字</button>` : ""}
                          <button class="btn" onclick='copyText(formatWeiboDesc(${JSON.stringify(c.desc)}), "微博简介", this)'>复制简介</button>
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
        container.innerHTML = `<div class="grid">${visible.map(c => {
          const coverUrl = c.cover_path ? `/files?path=${encodeURIComponent(c.cover_path)}` : "";
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
                  <span class="wb-tag">微博：${esc(c.weibo_name)}</span>
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
                      ${c.video_path ? `<button class="btn btn-primary" onclick='openPath(${JSON.stringify(c.package_dir)}, ${JSON.stringify(c.video_path)})'>📁 定位视频</button>` : ""}
                      ${c.cover_path ? `<button class="btn" onclick='openPath(${JSON.stringify(c.package_dir)}, ${JSON.stringify(c.cover_path)})'>🖼 定位封面</button>` : ""}
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
                  <div class="field-label"><span>简介（点击复制会自动按上方规则去抖音尾标）</span><button class="btn" onclick='copyText(formatWeiboDesc(${JSON.stringify(c.desc)}),"微博简介",this)'>📋 复制简介</button></div>
                  <div class="text-box">${esc(c.desc)}</div>
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
        selected_lines: selectedLines,
        single_url: document.getElementById("single-url").value.trim(),
        single_weibo: document.getElementById("single-weibo").value.trim(),
        single_uid: document.getElementById("single-uid").value.trim(),
        dedup_preset: document.getElementById("opt-dedup").value,
        lookback_days: Number(document.getElementById("opt-lookback").value),
        latest_per_profile: Number(document.getElementById("opt-latest").value),
        crf: Number(document.getElementById("opt-crf").value),
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
      startPolling();
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
      document.getElementById("output-root-path").textContent = d.output_dir || "";
      document.getElementById("raw-tasks-text").value = d.raw_tasks || "";
      parsedAccounts = d.accounts || [];
      cardsData = d.cards || [];
      renderAccounts();
      refreshDateOptions();
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
            self._send_json({
                "ok": ffmpeg_ok and ffprobe_ok and cfg.douyin_cookie_path.is_file(),
                "platform": sys.platform,
                "ffmpeg_path": cfg.ffmpeg_path,
                "ffmpeg_ok": ffmpeg_ok,
                "ffprobe_ok": ffprobe_ok,
                "douyin_cookie_ok": cfg.douyin_cookie_path.is_file(),
                "weibo_cookie_ok": cfg.weibo_cookie_path.is_file(),
                "output_dir": str(cfg.output_dir),
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
            with _STATE_LOCK:
                running = _APP_STATE["running"]
                cards = _APP_STATE["cards"] or _load_existing_cards()
            self._send_json({
                "output_dir": str(cfg.output_dir),
                "strip_tag_keywords": cfg.strip_tag_keywords,
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
                _APP_STATE["running"] = True
                _APP_STATE["logs"] = ["已启动处理任务..."]
                _APP_STATE["failures"] = []
                _APP_STATE["summary"] = None
            th = threading.Thread(target=_run_worker, args=(body,), daemon=True)
            th.start()
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
            self._send_json({"ok": True, "output_dir": str(resolved), "cards": _load_existing_cards()})
            return

        if parsed.path == "/api/check-update":
            cfg = load_config()
            root = cfg.project_root
            if not (root / ".git").exists():
                self._send_json({"error": "当前目录不是 Git 仓库（请用 git clone 拉取项目）"}, status=400)
                return
            try:
                fetch_res = _run_git(["fetch", "origin"], root, timeout=20)
                if fetch_res.returncode != 0:
                    err_msg = (fetch_res.stderr or fetch_res.stdout or "git fetch 失败").strip()
                    self._send_json({"error": err_msg}, status=400)
                    return
                local_c = _run_git(["rev-parse", "--short", "HEAD"], root).stdout.strip()
                upstream_ref = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
                remote_branch = upstream_ref.stdout.strip() if upstream_ref.returncode == 0 else "origin/main"
                remote_c = _run_git(["rev-parse", "--short", remote_branch], root).stdout.strip()
                cnt_raw = _run_git(["rev-list", "--count", f"HEAD..{remote_branch}"], root).stdout.strip()
                behind = int(cnt_raw or "0")
                changelog: list[str] = []
                if behind > 0:
                    log_raw = _run_git(["log", "--format=%h %s", f"HEAD..{remote_branch}", "-n", "6"], root).stdout
                    changelog = [ln.strip() for ln in log_raw.splitlines() if ln.strip()]
                self._send_json({
                    "ok": True,
                    "up_to_date": behind == 0,
                    "local_commit": local_c,
                    "remote_commit": remote_c,
                    "behind_count": behind,
                    "changelog": changelog,
                })
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return

        if parsed.path == "/api/apply-update":
            with _STATE_LOCK:
                if _APP_STATE["running"]:
                    self._send_json({"error": "当前有下载或登录任务在运行，请等完成后再更新"}, status=409)
                    return
            cfg = load_config()
            root = cfg.project_root
            try:
                old_req = (root / "requirements.txt").read_text(encoding="utf-8") if (root / "requirements.txt").is_file() else ""
                pull_res = _run_git(["pull", "--ff-only"], root, timeout=30)
                if pull_res.returncode != 0:
                    err_msg = (pull_res.stderr or pull_res.stdout or "git pull 失败").strip()
                    self._send_json({"error": err_msg}, status=400)
                    return
                out_txt = (pull_res.stdout or "").strip()
                if "Already up to date" in out_txt or "已经是最新的" in out_txt:
                    self._send_json({"ok": True, "restarting": False, "message": "当前已是最新版本"})
                    return
                new_req = (root / "requirements.txt").read_text(encoding="utf-8") if (root / "requirements.txt").is_file() else ""
                if new_req != old_req:
                    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(root / "requirements.txt"), "--quiet"], check=False)
                self._send_json({"ok": True, "restarting": True, "message": "更新拉取成功，正在重启控制台..."})
                _schedule_self_restart()
            except Exception as exc:
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
            try:
                pkg_dir.relative_to(cfg.output_dir.resolve())
            except ValueError:
                self._send_json({"error": "非法路径"}, status=403)
                return
            if pkg_dir.is_dir():
                _open_native_path(pkg_dir, sel_path)
            self._send_json({"ok": True})
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
