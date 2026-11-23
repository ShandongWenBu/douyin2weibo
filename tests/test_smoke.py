"""Offline regression smoke tests for douyin2weibo (Mac & Windows)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from src.config import (
    DEFAULT_SPORTS_REPLACE_TAGS,
    DedupConfig,
    format_weibo_description,
    is_sports_account,
    load_config,
    sanitize_filename,
    save_output_dir_to_config,
)
from src.douyin_fetcher import DouyinVideo, classify_douyin_url
from src.input_parser import parse_text_block
from src.main import _get_or_create_date_dir
from src.ui_server import (
    ConsoleHandler,
    _append_log,
    _find_free_port,
    _load_existing_cards,
    _merge_cards,
    _normalize_day,
    _open_native_path,
    _resolve_live_package_dir,
)
from src.updater import _build_attempts, _run_with_mirror_fallback
from src.weibo_dedup import _sleep_interruptible, compute_time_window, run_weibo_dedup


class RegressionSmokeTests(unittest.TestCase):
    def test_windows_three_piece_subprocess_utf8_audit(self) -> None:
        src_dir = Path(__file__).resolve().parent.parent / "src"
        for pyf in src_dir.glob("*.py"):
            txt = pyf.read_text(encoding="utf-8")
            for idx, line in enumerate(txt.splitlines(), start=1):
                if "subprocess.run(" in line and "text=True" in line:
                    self.assertIn(
                        'encoding="utf-8"',
                        line,
                        f"{pyf}:{idx} subprocess.run(text=True) missing encoding='utf-8'",
                    )

    def test_sanitize_filename_and_config_output_dir(self) -> None:
        self.assertEqual(sanitize_filename('a<b>c:d/e\\f|g?h*i.'), "a_b_c_d_e_f_g_h_i")
        self.assertEqual(sanitize_filename("CON"), "_CON")

        with tempfile.TemporaryDirectory() as td:
            tmp_cfg = Path(td) / "config.yaml"
            tmp_cfg.write_text('# keep comment\noutput_dir: "~/Desktop/old"\n', encoding="utf-8")
            target = str(Path(td) / "custom_out")
            res = save_output_dir_to_config(target, config_path=tmp_cfg)
            self.assertEqual(res.resolve(), Path(target).resolve())
            self.assertIn("# keep comment", tmp_cfg.read_text(encoding="utf-8"))
            self.assertEqual(load_config(tmp_cfg).output_dir.resolve(), Path(target).resolve())

    def test_url_classification_and_share_text_parsing(self) -> None:
        self.assertEqual(
            classify_douyin_url("https://www.douyin.com/user/MS4wLjABAAAAxxx?modal_id=7709074754874969386"),
            ("video", "7709074754874969386"),
        )
        self.assertEqual(
            classify_douyin_url("https://www.douyin.com/video/7709074754874969386"),
            ("video", "7709074754874969386"),
        )
        self.assertEqual(
            classify_douyin_url("https://www.douyin.com/user/MS4wLjABAAAAxxx"),
            ("user", "MS4wLjABAAAAxxx"),
        )

        sample = """
        https://www.douyin.com/video/7709074754874969386
        4.82 复制打开抖音，看看【某某作品】 https://v.douyin.com/abc123/ 03/15 b@A.nQ WzL:/
        https://www.douyin.com/video/1111111111111111111  示例微博号
        https://www.douyin.com/video/2222222222222222222 https://www.douyin.com/video/7709074754874969386
        https://weibo.com/u/8310157614,13303932842.0,微博A,抖音A,https://v.douyin.com/xyz123/
        """
        tasks = parse_text_block(sample, default_weibo_name="默认号")
        self.assertEqual(len(tasks), 5)
        self.assertEqual(tasks[0].weibo_name, "默认号")
        self.assertEqual(tasks[1].douyin_url, "https://v.douyin.com/abc123/")
        self.assertEqual(tasks[1].weibo_name, "默认号")
        self.assertEqual(tasks[2].weibo_name, "示例微博号")
        self.assertEqual(tasks[4].weibo_uid, "8310157614")

    def test_windows_explorer_quoting_for_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "绿茵列传_劳资风云 姆巴佩 与"
            pkg.mkdir()
            vid = pkg / "video_1080p.mp4"
            vid.write_bytes(b"0")

            calls = []
            with patch.object(sys, "platform", "win32"), patch("subprocess.Popen", side_effect=lambda cmd: calls.append(cmd)):
                _open_native_path(pkg, vid)
                _open_native_path(pkg, pkg / "missing.mp4")

            self.assertEqual(calls[0], f'explorer /select,"{vid}"')
            self.assertEqual(calls[1], f'explorer "{pkg}"')

    def test_stable_date_dir_and_stale_folder_resolution_and_404(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "2026-11-19_共37个" / "绿茵列传_劳资风云 姆巴佩 与"
            legacy.mkdir(parents=True)
            vid = legacy / "video_1080p.mp4"
            vid.write_bytes(b"mp4")
            (legacy / "meta.json").write_text(json.dumps({"aweme_id": "770999"}), encoding="utf-8")

            d20 = _get_or_create_date_dir(root, "2026-11-20")
            self.assertEqual(d20.name, "2026-11-20")

            stale_pkg = root / "2026-11-19_共12个" / "绿茵列传_劳资风云 姆巴佩 与"
            self.assertEqual(_resolve_live_package_dir(root, stale_pkg), legacy.resolve())
            self.assertIsNone(_resolve_live_package_dir(root, root / "2026-11-19_共12个" / "deleted_pkg"))

            cfg = load_config()
            cfg.output_dir = root
            opened = []
            port = _find_free_port(8792)
            srv = ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                with patch("src.ui_server.load_config", return_value=cfg), patch(
                    "src.ui_server._open_native_path", side_effect=lambda d, f=None: opened.append((d, f))
                ):
                    cards = _load_existing_cards()
                    self.assertEqual(cards[0]["date_str"], "2026-11-19")

                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/open-folder",
                        data=json.dumps({"package_dir": str(stale_pkg), "select_file": str(stale_pkg / "video_1080p.mp4"), "aweme_id": "770999"}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        d = json.loads(r.read().decode("utf-8"))
                        self.assertEqual(Path(d["package_dir"]).resolve(), legacy.resolve())
                        self.assertEqual(opened[-1], (legacy.resolve(), vid.resolve()))

                    req404 = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/open-folder",
                        data=json.dumps({"package_dir": str(root / "2026-11-19_共12个" / "deleted_pkg")}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(req404, timeout=5)
                    self.assertEqual(ctx.exception.code, 404)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_merge_cards_and_interruptible_dedup(self) -> None:
        self.assertEqual(_normalize_day("2026-11-19_共3个"), "2026-11-19")
        merged = _merge_cards(
            [{"aweme_id": "4", "date_str": "2026-11-19", "published": False}],
            [
                {"aweme_id": "2", "date_str": "2026-11-19", "published": True},
                {"aweme_id": "1", "date_str": "2026-11-19", "published": False},
            ],
        )
        self.assertEqual([c["aweme_id"] for c in merged], ["1", "4", "2"])

        async def _check() -> None:
            ev = threading.Event()
            threading.Timer(0.1, ev.set).start()
            t0 = time.time()
            stopped = await _sleep_interruptible(5.0, ev)
            self.assertTrue(stopped)
            self.assertLess(time.time() - t0, 1.0)

            v = DouyinVideo(
                aweme_id="999", share_url="", video_page_url="", author="a", sec_uid="",
                desc="测试", title="测试", hashtags=[], create_time=int(time.time()),
                publish_time_str="", duration_sec=60, width=1920, height=1080,
            )
            sd, ed, _ = compute_time_window(v, DedupConfig(lookback_days=30))
            self.assertLessEqual(sd, ed)
            res = await run_weibo_dedup(
                [(v, "测试号", "")],
                all_known_own_uids=set(),
                weibo_cookie_path=Path("nonexistent.json"),
                ledger_path=Path(tempfile.gettempdir()) / "nonexistent_ledger.json",
                state_path=Path(tempfile.gettempdir()) / "nonexistent_state.json",
                cfg=DedupConfig(mode="auto"),
                stop_event=ev,
            )
            self.assertEqual(res["999"].label, "已跳过查重")

        asyncio.run(_check())

    def test_updater_mirror_fallback_and_file_log(self) -> None:
        attempts = _build_attempts(
            "https://github.com/ShandongWenBu/douyin2weibo.git",
            ["https://gh-proxy.com/", "https://ghfast.top/"],
        )
        self.assertEqual(attempts[0], ("直连", None))
        self.assertEqual(attempts[1][0], "镜像 https://gh-proxy.com")

        seen_rewrites = []

        def fake_run_git(args, cwd, timeout=20, url_rewrite=None):
            if args[:2] == ["remote", "get-url"]:
                return subprocess.CompletedProcess(["git"], 0, stdout="https://github.com/ShandongWenBu/douyin2weibo.git\n", stderr="")
            seen_rewrites.append(url_rewrite)
            if url_rewrite is None:
                return subprocess.CompletedProcess(["git"], 128, stdout="", stderr="Failed to connect to github.com")
            return subprocess.CompletedProcess(["git"], 0, stdout="ok", stderr="")

        with patch("src.updater.run_git", side_effect=fake_run_git):
            res, via = _run_with_mirror_fallback(["fetch", "origin"], Path("."), ["https://gh-proxy.com/"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(via, "镜像 https://gh-proxy.com")
        self.assertEqual(len(seen_rewrites), 2)

        _append_log("回归测试：logs/app.log 写入验证")
        log_path = Path(__file__).resolve().parent.parent / "logs" / "app.log"
        self.assertTrue(log_path.is_file())
        self.assertIn("回归测试：logs/app.log 写入验证", log_path.read_text(encoding="utf-8"))

    def test_sports_account_detection_and_tag_replacement(self) -> None:
        # 1. Real sports account names match
        for wb in ("体坛视角1", "奶酪懂个球", "闪耀体坛", "足球有故事", "星河乒乓", "绿茵列传"):
            self.assertTrue(is_sports_account(weibo_name=wb), f"{wb} should be detected as sports")

        # 2. Tag/author fallback matches, while gaming/random/prose-only stay non-sports
        self.assertTrue(is_sports_account(weibo_name="普通号", author="普通作者", desc="绝杀！#NBA #篮球"))
        self.assertFalse(is_sports_account(weibo_name="洛鸢三角洲行动", author="洛鸢", desc="撤离成功 #三角洲行动# #抖音精选#"))
        self.assertFalse(is_sports_account(weibo_name="小蛙三角洲行动", author="userF10", desc="今天聊个打篮球段子 #搞笑日常#"))

        # 3. Sports account replaces all hashtags cleanly with zero stray '#'
        raw_sports = "劳资风云：姆巴佩与俱乐部争议不断 #足球# #姆巴佩 #抖音精选# #青年创作者成长计划#"
        replaced = format_weibo_description(
            raw_sports,
            ["抖音精选", "青年创作者成长计划"],
            is_sports=True,
        )
        self.assertEqual(
            replaced,
            f"劳资风云：姆巴佩与俱乐部争议不断 {DEFAULT_SPORTS_REPLACE_TAGS}",
        )
        self.assertEqual(
            DEFAULT_SPORTS_REPLACE_TAGS,
            "#微博视频号迎新计划##一分钟精选视频扶持计划##V观体坛#",
        )

        # 4. Non-sports account strips only strip_tag_keywords and leaves no stray '#'
        raw_game = "猛攻撤离实录 #三角洲行动# #抖音精选# #青年创作者成长计划#"
        cleaned_game = format_weibo_description(
            raw_game,
            ["抖音精选", "青年创作者成长计划"],
            is_sports=False,
        )
        self.assertEqual(cleaned_game, "猛攻撤离实录 #三角洲行动#")

        # 5. Legacy Windows config.yaml omitting sports keys still receives full defaults
        with tempfile.TemporaryDirectory() as td:
            legacy_cfg = Path(td) / "config.yaml"
            legacy_cfg.write_text('weibo_clean:\n  strip_tag_keywords:\n    - "抖音精选"\n', encoding="utf-8")
            loaded = load_config(legacy_cfg)
            self.assertEqual(loaded.sports_replace_tags, DEFAULT_SPORTS_REPLACE_TAGS)
            self.assertIn("体坛", loaded.sports_account_keywords)


if __name__ == "__main__":
    unittest.main()
