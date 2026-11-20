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
| 主流程编排、桌面日期归档（固定 `YYYY-MM-DD` 目录） | `src/main.py` | `execute_pipeline()`, `_get_or_create_date_dir()` |

## 3. 跨平台与安全红线（修改必守 — 推送前默认执行 Windows 三件套自检）
1. **Windows 三件套推送前自检（每次改代码必查）**：
   - **① 路径与命令引号**：一律用 `pathlib.Path`，生成文件名必须经过 `sanitize_filename()`；调用 Windows 原生命令（如 `explorer /select,"<路径>"`）时必须注意含空格/中文/反斜杠的引号转义，不得用 `list2cmdline` 把 `/select,` 包进引号。
   - **② 文件/目录独占锁**：Windows 下禁止对正在被资源管理器或上传软件占用的活目录做动态改名（日期目录固定 `YYYY-MM-DD`，永不动态改名），删除/替换文件须做好异常兜底。
   - **③ UTF-8 编码**：Windows 默认 GBK/CP936，所有文本读写和 `subprocess.run(..., text=True)` 必须显式传入 `encoding="utf-8"`（子进程输出建议加 `errors="replace"`；PowerShell 输出中文前加 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;`）。
2. **安全与风控**：
   - 遇到抖音或微博验证码/安全验证/登录跳转，必须立即抛出 `CaptchaRequiredError` 停止，严禁尝试自动绕过。
   - 搜索查重严禁去掉 `daily_search_limit` 和 `min_delay_sec` / `max_delay_sec` 随机延时保护。
3. **Git 同步隔离**：
   - `config.yaml`、`待发.txt`、`cookies/`、`ledger.json`、`.dedup_state.json`、`output/`、`logs/` 均在 `.gitignore` 中，**严禁强行提交含账号、手机号或 Cookie 的文件**。
   - 若新增配置项，请同步更新 `config.example.yaml`。

## 4. 自动落盘日志与自检排障（供本地 Agent 快速生成 Issue）
- **滚动日志文件**：`logs/app.log`（UTF-8，单文件 2 MB，最多保留 3 份，由 `src/ui_server.py` 自动写入启动信息、处理日志、接口警告及完整异常堆栈，不记录 Cookie/密钥）。
- **环境自检接口**：`GET http://127.0.0.1:8765/api/health` 返回 `platform`、`python`、`commit`、`ffmpeg_ok`、`ffprobe_ok`、`douyin_cookie_ok`、`weibo_cookie_ok`、`output_dir` 与 `log_path`。
- **语法与转义检查（Mac / Windows 通用）**：
  ```bash
  # Mac:
  .venv/bin/python -W error -m py_compile src/*.py
  # Windows:
  .venv\Scripts\python.exe -W error -m py_compile src/*.py
  ```
- **Windows 排查约定**：生产机（Windows）本地 Agent 排查问题时优先读取 `logs/app.log` 和 `/api/health`，总结结构化 Issue 交由主开发机修改并推送，尽量避免直接在 Windows 本地改 `src/` 造成 `git pull` 冲突。
