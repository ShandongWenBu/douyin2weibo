"""CLI entrypoint for batch & single-link douyin2weibo processing."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .config import (
    AppConfig,
    format_weibo_description,
    is_sports_account,
    load_config,
    sanitize_filename,
)
from .douyin_fetcher import (
    CaptchaRequiredError,
    DouyinVideo,
    download_first_working_url,
    fetch_douyin_batch,
)
from .input_parser import TaskInput, load_tasks_from_file, parse_line
from .media_processor import process_cover_to_jpg, transcode_to_1080p
from .weibo_dedup import DedupResult, run_weibo_dedup
from .workbench import render_workbench_html


def _load_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"videos": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("videos", {})
            return data
    except Exception:
        pass
    return {"videos": {}}


def _save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_or_create_date_dir(output_root: Path, date_str: str) -> Path:
    """Use a stable immutable YYYY-MM-DD directory (never renamed dynamically)."""
    target = output_root / date_str
    target.mkdir(parents=True, exist_ok=True)
    return target


def _make_package_dir(date_dir: Path, weibo_name: str, video: DouyinVideo) -> Path:
    account_part = sanitize_filename(weibo_name or video.author or "未指定账号", max_len=24)
    title_part = sanitize_filename(video.title[:10] if video.title else video.aweme_id, max_len=20)
    base_name = f"{account_part}_{title_part}"
    candidate = date_dir / base_name
    if candidate.exists():
        meta_file = candidate / "meta.json"
        if meta_file.is_file():
            try:
                old_meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if old_meta.get("aweme_id") == video.aweme_id:
                    return candidate
            except Exception:
                pass
        candidate = date_dir / f"{base_name}_{video.aweme_id[-4:]}"
    return candidate


def _collect_interactive_tasks(default_file: Path) -> list[TaskInput]:
    print("\n未检测到命令行参数，请选择运行方式：")
    print(f"  [1] 批量处理（读取 {default_file.name}）")
    print("  [2] 单独处理（粘贴 1 条抖音链接或分享文本）")
    choice = input("请输入选项 [1/2，默认 1]: ").strip() or "1"
    if choice == "2":
        raw_url = input("请粘贴抖音作品或主页链接：").strip()
        wb_name = input("对应微博号名（可留空回车）：").strip()
        wb_uid = input("对应微博 UID（可选，用于查重，可留空回车）：").strip()
        parsed = parse_line(f"{raw_url}\t{wb_name}\t{wb_uid}".strip())
        if not parsed:
            raise ValueError("未能从输入中识别到有效的抖音链接。")
        return [parsed]
    return load_tasks_from_file(default_file)


async def execute_pipeline(
    cfg: AppConfig,
    tasks: list[TaskInput],
    *,
    dedup_only: bool = False,
    force_download: bool = False,
    overwrite: bool = False,
    log_fn: Callable[[str], None] = print,
    stop_dedup_event: threading.Event | None = None,
    phase_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run Douyin fetch -> Weibo dedup -> download & 1080P transcode -> 工作台.html."""
    if not tasks:
        raise ValueError("没有找到需要处理的任务。")

    if phase_fn:
        phase_fn("fetching")
    log_fn(f"共读取到 {len(tasks)} 条输入任务，开始解析抖音作品信息...")

    unique_urls = list(dict.fromkeys(t.douyin_url for t in tasks))
    fetched = await fetch_douyin_batch(
        unique_urls,
        cookie_path=cfg.douyin_cookie_path,
        latest_per_profile=cfg.latest_per_profile,
        headless=cfg.dedup.headless,
    )

    expanded_items: list[tuple[TaskInput, DouyinVideo]] = []
    failures: list[dict[str, str]] = []

    for task in tasks:
        videos, err = fetched.get(task.douyin_url, ([], "未返回结果"))
        if err or not videos:
            failures.append({
                "url": task.douyin_url,
                "weibo_name": task.weibo_name,
                "error": err or "未获取到作品",
            })
            log_fn(f"[失败] {task.douyin_url} ({task.weibo_name or '-'}): {err}")
            continue
        for vid in videos:
            log_fn(
                f"  [解析成功] {task.weibo_name or vid.author} | {vid.aweme_id} | "
                f"{vid.width}x{vid.height} | {vid.duration_sec:.1f}s | {vid.title[:35]}"
            )
            expanded_items.append((task, vid))

    if not expanded_items:
        return {
            "success_count": 0,
            "skipped_red_count": 0,
            "cards": [],
            "failures": failures,
            "workbench_path": str(cfg.project_root / "工作台.html"),
        }

    if phase_fn:
        phase_fn("dedup")
    log_fn(f"\n开始微博查重（模式：{cfg.dedup.mode}，全站搜：{'开' if cfg.dedup.enable_global_search else '关'}，每日上限：{cfg.dedup.daily_search_limit}）...")
    all_own_uids = {t.weibo_uid for t in tasks if t.weibo_uid}
    dedup_inputs = [(vid, task.weibo_name, task.weibo_uid) for task, vid in expanded_items]
    dedup_map = await run_weibo_dedup(
        dedup_inputs,
        all_known_own_uids=all_own_uids,
        weibo_cookie_path=cfg.weibo_cookie_path,
        ledger_path=cfg.ledger_path,
        state_path=cfg.dedup_state_path,
        cfg=cfg.dedup,
        stop_event=stop_dedup_event,
        log_fn=log_fn,
    )

    if phase_fn:
        phase_fn("downloading")

    for task, vid in expanded_items:
        dr = dedup_map.get(vid.aweme_id)
        if dr:
            log_fn(f"  [查重] {task.weibo_name or vid.author} | {dr.label} - {dr.summary}")

    today_str = datetime.date.today().isoformat()
    date_dir = _get_or_create_date_dir(cfg.output_dir, today_str)
    ledger = _load_ledger(cfg.ledger_path)
    cards: list[dict[str, Any]] = []
    success_count = 0
    skipped_red_count = 0

    for idx, (task, vid) in enumerate(expanded_items, start=1):
        dr = dedup_map.get(vid.aweme_id) or DedupResult(status="off", label="未查重", summary="")
        wb_name = task.weibo_name or vid.author
        sports_flag = is_sports_account(wb_name, vid.author, vid.desc, cfg.sports_account_keywords)
        existing_ledger = (ledger.get("videos") or {}).get(vid.aweme_id) or {}
        is_pub = bool(existing_ledger.get("published", False))

        if dedup_only or (dr.status == "red" and not force_download):
            if dr.status == "red":
                skipped_red_count += 1
            cards.append({
                "aweme_id": vid.aweme_id,
                "weibo_name": wb_name,
                "weibo_uid": task.weibo_uid,
                "author": vid.author,
                "publish_time": vid.publish_time_str,
                "duration_sec": vid.duration_sec,
                "resolution_info": f"{vid.width}x{vid.height}（未下载）",
                "video_size_mb": None,
                "title": vid.title,
                "desc": vid.desc,
                "is_sports": sports_flag,
                "video_page_url": vid.video_page_url,
                "dedup_status": dr.status,
                "dedup_label": dr.label,
                "dedup_summary": dr.summary,
                "dedup_evidences": [asdict(e) for e in dr.evidences],
                "manual_search_urls": dr.manual_search_urls,
                "package_dir": "",
                "cover_path": "",
                "video_path": "",
                "published": is_pub,
            })
            continue

        log_fn(f"\n[{idx}/{len(expanded_items)}] 正在处理：{wb_name} - {vid.title[:28]}")
        pkg_dir = _make_package_dir(date_dir, wb_name, vid)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        raw_video_path = pkg_dir / "_raw_source.mp4"
        final_video_path = pkg_dir / "video_1080p.mp4"
        raw_cover_path = pkg_dir / "_raw_cover.img"
        final_cover_path = pkg_dir / "cover.jpg"
        meta_path = pkg_dir / "meta.json"

        try:
            reused = False
            if not overwrite and final_video_path.is_file() and final_cover_path.is_file() and meta_path.is_file():
                try:
                    old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if old_meta.get("aweme_id") == vid.aweme_id:
                        reused = True
                        size_mb = float(old_meta.get("video_size_mb") or round(final_video_path.stat().st_size / (1024 * 1024), 2))
                        res_info = f"{old_meta.get('source_resolution', '')} -> {old_meta.get('output_resolution', '')}".strip(" ->")
                        log_fn(f"  [复用已有上传包] {pkg_dir.name} ({res_info}, {size_mb} MB)")
                except Exception:
                    reused = False

            if not reused:
                log_fn("  -> 下载无水印最高画质原画...")
                download_first_working_url(vid.video_urls, raw_video_path)

                log_fn("  -> 下载原封面并处理为 <=5MB JPG...")
                download_first_working_url(vid.cover_urls, raw_cover_path)
                process_cover_to_jpg(raw_cover_path, final_cover_path, cfg.transcode.cover_max_mb)

                log_fn("  -> ffmpeg 转码 1080P...")
                out_probe = transcode_to_1080p(
                    cfg.ffmpeg_path,
                    cfg.ffprobe_path,
                    raw_video_path,
                    final_video_path,
                    cfg.transcode,
                )

                if raw_video_path.exists():
                    raw_video_path.unlink()
                if raw_cover_path.exists():
                    raw_cover_path.unlink()

                formatted_desc = format_weibo_description(
                    vid.desc,
                    cfg.strip_tag_keywords,
                    is_sports=sports_flag,
                    sports_replace_tags=cfg.sports_replace_tags,
                )
                (pkg_dir / "标题.txt").write_text(vid.title, encoding="utf-8")
                (pkg_dir / "简介.txt").write_text(formatted_desc, encoding="utf-8")

                size_mb = round(final_video_path.stat().st_size / (1024 * 1024), 2)
                res_info = f"{vid.width}x{vid.height} -> {out_probe.width}x{out_probe.height}"
                meta = {
                    "aweme_id": vid.aweme_id,
                    "weibo_name": wb_name,
                    "weibo_uid": task.weibo_uid,
                    "author": vid.author,
                    "is_sports": sports_flag,
                    "share_url": vid.share_url,
                    "video_page_url": vid.video_page_url,
                    "publish_time": vid.publish_time_str,
                    "duration_sec": vid.duration_sec,
                    "source_resolution": f"{vid.width}x{vid.height}",
                    "output_resolution": f"{out_probe.width}x{out_probe.height}",
                    "video_size_mb": size_mb,
                    "title": vid.title,
                    "desc": vid.desc,
                    "hashtags": vid.hashtags,
                    "dedup": asdict(dr),
                    "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                meta_path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            ledger.setdefault("videos", {})[vid.aweme_id] = {
                "aweme_id": vid.aweme_id,
                "weibo_name": wb_name,
                "title": vid.title,
                "package_dir": str(pkg_dir),
                "published": is_pub,
                "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            _save_ledger(cfg.ledger_path, ledger)

            success_count += 1
            log_fn(f"  [完成] {pkg_dir.name} ({res_info}, {size_mb} MB)")
            cards.append({
                "aweme_id": vid.aweme_id,
                "weibo_name": wb_name,
                "weibo_uid": task.weibo_uid,
                "author": vid.author,
                "publish_time": vid.publish_time_str,
                "duration_sec": vid.duration_sec,
                "resolution_info": res_info,
                "video_size_mb": size_mb,
                "title": vid.title,
                "desc": vid.desc,
                "is_sports": sports_flag,
                "video_page_url": vid.video_page_url,
                "dedup_status": dr.status,
                "dedup_label": dr.label,
                "dedup_summary": dr.summary,
                "dedup_evidences": [asdict(e) for e in dr.evidences],
                "manual_search_urls": dr.manual_search_urls,
                "package_dir": str(pkg_dir),
                "cover_path": str(final_cover_path),
                "video_path": str(final_video_path),
                "published": is_pub,
            })
        except Exception as exc:
            failures.append({
                "url": task.douyin_url,
                "weibo_name": wb_name,
                "error": str(exc),
            })
            log_fn(f"  [失败] {wb_name} ({vid.aweme_id}): {exc}")

    for c in cards:
        c.setdefault("date_str", today_str)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    workbench_path = cfg.project_root / "工作台.html"
    render_workbench_html(cards, failures, workbench_path, now_str)

    return {
        "success_count": success_count,
        "skipped_red_count": skipped_red_count,
        "cards": cards,
        "failures": failures,
        "workbench_path": str(workbench_path),
    }


async def run_pipeline(args: argparse.Namespace) -> int:
    cfg: AppConfig = load_config(Path(args.config) if args.config else None)
    if args.dedup:
        cfg.dedup.mode = args.dedup
    if args.no_global_search:
        cfg.dedup.enable_global_search = False
    if args.lookback_days is not None:
        cfg.dedup.lookback_days = max(0, args.lookback_days)
    if args.since:
        cfg.dedup.min_date = args.since
    if args.latest is not None:
        cfg.latest_per_profile = max(1, args.latest)
    if args.output_dir:
        cfg.output_dir = Path(args.output_dir).expanduser().resolve()

    tasks: list[TaskInput] = []
    if args.url:
        parsed = parse_line(f"{args.url}\t{args.weibo or ''}\t{args.weibo_uid or ''}".strip())
        if not parsed:
            print(f"[错误] 无法识别的抖音链接：{args.url}")
            return 1
        tasks = [parsed]
    else:
        input_file = Path(args.file) if args.file else (cfg.project_root / "待发.txt")
        if args.interactive and not input_file.is_file():
            tasks = _collect_interactive_tasks(input_file)
        elif args.interactive and sys.stdin.isatty():
            tasks = _collect_interactive_tasks(input_file)
        else:
            tasks = load_tasks_from_file(input_file)

    if not tasks:
        print("没有找到需要处理的任务。请在 待发.txt 中填写链接，或使用 --url 传入单条链接。")
        return 1

    try:
        res = await execute_pipeline(
            cfg,
            tasks,
            dedup_only=args.dedup_only,
            force_download=args.force_download,
            overwrite=args.overwrite,
            log_fn=print,
        )
    except CaptchaRequiredError as exc:
        print(f"\n[已停止] {exc}")
        return 2

    if not res["cards"] and res["failures"]:
        print("全部任务解析失败，未生成上传包。")
        return 1

    print("\n================ 处理汇总 ================")
    print(f"打包成功：{res['success_count']} 条")
    print(f"查重跳过（已发过）：{res['skipped_red_count']} 条")
    print(f"处理失败：{len(res['failures'])} 条")
    print(f"工作台：{res['workbench_path']}")

    workbench_path = Path(res["workbench_path"])
    if not args.no_open and workbench_path.is_file():
        webbrowser.open(workbench_path.resolve().as_uri())

    return 0 if not res["failures"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="douyin2weibo：抖音查重、无水印下载、1080P 转码与工作台生成")
    p.add_argument("--config", help="配置文件路径（默认 config.yaml）")
    p.add_argument("--output-dir", help="输出视频存放目录覆盖")
    p.add_argument("--file", help="批量输入文件路径（默认 待发.txt）")
    p.add_argument("--url", help="单条抖音作品或主页链接")
    p.add_argument("--weibo", help="单条模式对应的微博号名")
    p.add_argument("--weibo-uid", help="单条模式对应的微博 UID（可选，用于查重）")
    p.add_argument("--latest", type=int, help="当链接为抖音主页时，抓最新几条非置顶作品")
    p.add_argument("--dedup", choices=["auto", "manual", "off"], help="查重模式覆盖")
    p.add_argument("--no-global-search", action="store_true", help="关闭 s.weibo.com 全站搜索，仅查本地台账与目标微博主页")
    p.add_argument("--lookback-days", type=int, help="查重时间阈值（仅查最近 N 天内，0 表示仅按抖音发布日起算）")
    p.add_argument("--since", help="查重固定起始日期（YYYY-MM-DD）")
    p.add_argument("--dedup-only", action="store_true", help="仅解析和查重，不下载转码")
    p.add_argument("--force-download", action="store_true", help="即使查重为红灯也强制下载打包")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的同作品上传包重新下载转码")
    p.add_argument("--interactive", action="store_true", help="交互选择批量或单条链接")
    p.add_argument("--no-open", action="store_true", help="处理完不自动打开浏览器")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    code = asyncio.run(run_pipeline(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
