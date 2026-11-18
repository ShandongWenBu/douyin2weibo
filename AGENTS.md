# AGENTS.md — 本地 Agent 维护与二次开发指南

本文件供本地 AI Agent（Antigravity / WorkBuddy / Cursor / Claude Code 等）在 Mac 或 Windows 上快速定位模块、安全修改与验证代码时直接读取。

## 1. 项目定位与边界
- **用途**：抖音作品/主页抓取 → 微博查重 → 无水印原画下载 → `ffmpeg` 转 1080P H.264 MP4 + 封面 JPG（≤ 5MB）→ 本地 Web 控制台 & 静态 `工作台.html`（供用户在「创作罐头」手动发布）。
- **明确不做**：不自动登录或操控「创作罐头」、不自动发微博、不绕过任何验证码/滑块/风控。

## 2. 模块地图（改什么看哪里，无需全库通读）
| 需求 / 问题 | 对应文件 | 核心函数 / 说明 |
| --- | --- | --- |
| 控制台网页 UI、按钮、30字提醒、去抖音尾标、本地 HTTP API | `src/ui_server.py` | `_HTML_PAGE`（前端）与 `ConsoleHandler`（`/api/health`, `/api/init`, `/api/login`, `/api/run`, `/api/status`, `/api/open-folder`, `/api/toggle-published`） |
| 可见浏览器扫码登录并保存 Cookie | `src/cookie_login.py` | `login_and_save_cookie()`（支持在界面点按钮或 CLI 扫码换号） |
| 离线静态 `工作台.html` 模板 | `src/workbench.py` | `render_workbench_html()` |
| 输入格式解析（`待发.txt`、CSV 行、分享口令、单链接） | `src/input_parser.py` | `parse_line()`, `load_tasks_from_file()` |
| 抖音接口拦截、无水印最高画质流选择、封面下载 | `src/douyin_fetcher.py` | `classify_douyin_url()`, `_pick_best_streams()`, `fetch_douyin_batch()` |
| 微博查重（台账 / 自家主页 `mymblog` / `s.weibo.com` 时间窗） | `src/weibo_dedup.py` | `compute_time_window()`, `build_search_queries()`, `run_weibo_dedup()` |
| 视频 1080P 转码与封面压缩 | `src/media_processor.py` | `probe_video()`, `transcode_to_1080p()`, `process_cover_to_jpg()` |
| 配置读取、Windows 文件名合法化 | `src/config.py` | `load_config()`, `sanitize_filename()`, `extract_title_and_hashtags()` |
| 主流程编排、桌面日期归档（`YYYY-MM-DD_共N个`） | `src/main.py` | `execute_pipeline()`, `_get_or_create_date_dir()`, `_finalize_date_dir_count()` |

## 3. 跨平台与安全红线（修改必守）
1. **Mac / Windows 双端兼容**：
   - 文件路径一律用 `pathlib.Path`，文本读写一律显式 `encoding="utf-8"`（读取用户输入文件可用 `utf-8-sig`）。
   - 生成文件夹或文件名必须经过 `sanitize_filename()`（过滤 `<>:"/\|?*` 和 Windows 保留名）。
   - 不得引入仅 Mac 可用的命令（原生打开目录已在 `src/ui_server.py` 的 `_open_native_path()` 中区分 `darwin` / `win`）。
2. **安全与风控**：
   - 遇到抖音或微博验证码/安全验证/登录跳转，必须立即抛出 `CaptchaRequiredError` 停止，严禁尝试自动绕过。
   - 搜索查重严禁去掉 `daily_search_limit` 和 `min_delay_sec` / `max_delay_sec` 随机延时保护。
3. **Git 同步隔离**：
   - `config.yaml`、`待发.txt`、`cookies/`、`ledger.json`、`.dedup_state.json`、`output/` 均在 `.gitignore` 中，**严禁强行提交含账号、手机号或 Cookie 的文件**。
   - 若新增配置项，请同步更新 `config.example.yaml`。

## 4. 修改后的最小验证步骤
1. 语法与转义检查（Mac / Windows 通用）：
   ```bash
   # Mac:
   .venv/bin/python -W error -m py_compile src/*.py
   # Windows:
   .venv\Scripts\python.exe -W error -m py_compile src/*.py
   ```
2. 环境自检接口（启动控制台后可访问）：
   - `GET http://127.0.0.1:8765/api/health` 返回 `ffmpeg_ok`、`ffprobe_ok`、`douyin_cookie_ok`、`weibo_cookie_ok` 与当前 `output_dir`。
3. 改完 `src/ui_server.py` 后，需关闭并重新双击 `启动控制台.bat`（或 `启动控制台.command`），然后刷新浏览器页面生效。
