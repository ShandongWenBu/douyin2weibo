---
name: douyin2weibo
description: Parse Douyin video or profile links, run rate-limited Weibo deduplication, download watermark-free source videos, transcode to 1080P H.264 MP4 with compressed JPG covers, and launch a local Web console + static 工作台.html for manual publishing in 创作罐头.
---

# douyin2weibo 使用说明

> 若需要修改源码或排查本机环境，请同时阅读 [AGENTS.md](AGENTS.md)。

## 目录结构
- `config.yaml` / `config.example.yaml`：配置抖音/微博 cookie 路径、ffmpeg 路径、桌面输出目录（默认 `~/Desktop/微博待发视频`）、文案去尾标规则与查重阈值
- `待发.txt` / `待发.example.txt`：批量任务文件（支持抖音作品链接、抖音主页链接、CSV 账号表行）
- `启动控制台.command` / `启动控制台.bat`：Mac / Windows 双击启动本地可视化控制台（`src/ui_server.py`）
- `更新代码.command` / `更新代码.bat`：Mac / Windows 双击从 Git 拉取最新代码
- `安装环境_Windows.bat`：Windows 首次一键初始化 `.venv`、模板文件与 Chromium 内核
- `src/main.py`：核心流水线与 CLI 入口
- `工作台.html`：每次批处理后同步生成的离线静态工作台备份

## 常用命令
1. **启动可视化控制台（日常推荐）**：
   - Mac：`.venv/bin/python -m src.ui_server`
   - Windows：`.venv\Scripts\python.exe -m src.ui_server`
2. **批量处理 `待发.txt`**：
   ```bash
   .venv/bin/python -m src.main --file 待发.txt
   ```
3. **单独处理一条链接**：
   ```bash
   .venv/bin/python -m src.main --url "https://v.douyin.com/xxxx/" --weibo "微博号名"
   ```
4. **仅查重 / 指定查重模式与时间范围**：
   - `--dedup auto|manual|off`、`--no-global-search`（仅查台账与自家微博主页）
   - `--lookback-days 30`、`--since YYYY-MM-DD`
   - `--dedup-only`（只查重不下载转码）

## 风控与安全边界
- 全站搜索查重默认开启每日上限（40 次）、随机间隔 8~15 秒，并带 `timescope` 时间窗与同日缓存。
- 遇到验证码或安全验证会立即停止并提示人工处理，严禁自动绕过。
