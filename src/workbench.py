"""Generate self-contained static 工作台.html for manual upload in 创作罐头."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _rel_or_uri(target: Path | None, base_dir: Path) -> str:
    if not target:
        return ""
    try:
        return target.resolve().relative_to(base_dir.resolve()).as_posix()
    except Exception:
        return target.resolve().as_uri()


def render_workbench_html(
    cards: list[dict[str, Any]],
    failures: list[dict[str, str]],
    output_html_path: Path,
    generated_at: str,
) -> Path:
    """Write static 工作台.html with embedded JSON data."""
    base_dir = output_html_path.parent.resolve()
    normalized_cards: list[dict[str, Any]] = []
    for c in cards:
        pkg_dir = Path(c["package_dir"]) if c.get("package_dir") else None
        cover_path = Path(c["cover_path"]) if c.get("cover_path") else None
        video_path = Path(c["video_path"]) if c.get("video_path") else None
        normalized_cards.append({
            **c,
            "cover_rel": _rel_or_uri(cover_path, base_dir),
            "video_rel": _rel_or_uri(video_path, base_dir),
            "folder_uri": pkg_dir.resolve().as_uri() if pkg_dir else "",
            "folder_native": str(pkg_dir.resolve()) if pkg_dir else "",
        })

    payload_json = json.dumps(
        {
            "generated_at": generated_at,
            "cards": normalized_cards,
            "failures": failures,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>douyin2weibo 上传工作台</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --card: #ffffff;
      --text: #161823;
      --muted: #667085;
      --border: #e4e7ec;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --green-bg: #ecfdf3; --green-fg: #027a48; --green-bd: #abefc6;
      --yellow-bg: #fffaeb; --yellow-fg: #b54708; --yellow-bd: #fedf89;
      --red-bg: #fef3f2; --red-fg: #b42318; --red-bd: #fecdca;
      --gray-bg: #f2f4f7; --gray-fg: #344054; --gray-bd: #d0d5dd;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .container {{ max-width: 1240px; margin: 0 auto; padding: 20px 18px 60px; }}
    header {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px 20px;
      margin-bottom: 18px;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
    }}
    h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
    .sub {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
    .stats {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .pill {{
      padding: 5px 11px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--border);
      background: var(--gray-bg);
      color: var(--gray-fg);
      cursor: pointer;
    }}
    .pill.active {{ outline: 2px solid var(--primary); }}
    .pill.green {{ background: var(--green-bg); color: var(--green-fg); border-color: var(--green-bd); }}
    .pill.yellow {{ background: var(--yellow-bg); color: var(--yellow-fg); border-color: var(--yellow-bd); }}
    .pill.red {{ background: var(--red-bg); color: var(--red-fg); border-color: var(--red-bd); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      transition: opacity 0.2s, box-shadow 0.2s;
    }}
    .card:hover {{ box-shadow: 0 8px 24px rgba(16, 24, 40, 0.06); }}
    .card.published {{ opacity: 0.62; }}
    .cover-wrap {{
      position: relative;
      background: #0f172a;
      height: 200px;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }}
    .cover-wrap img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
    }}
    .cover-placeholder {{
      color: #94a3b8;
      font-size: 13px;
      padding: 12px;
      text-align: center;
    }}
    .badge-top {{
      position: absolute;
      top: 10px;
      left: 10px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid transparent;
    }}
    .badge-dur {{
      position: absolute;
      bottom: 10px;
      right: 10px;
      background: rgba(15, 23, 42, 0.78);
      color: #fff;
      font-size: 12px;
      padding: 2px 8px;
      border-radius: 6px;
    }}
    .status-green {{ background: var(--green-bg); color: var(--green-fg); border-color: var(--green-bd); }}
    .status-yellow {{ background: var(--yellow-bg); color: var(--yellow-fg); border-color: var(--yellow-bd); }}
    .status-red {{ background: var(--red-bg); color: var(--red-fg); border-color: var(--red-bd); }}
    .status-manual, .status-off {{ background: var(--gray-bg); color: var(--gray-fg); border-color: var(--gray-bd); }}
    .body {{ padding: 14px 16px 16px; display: flex; flex-direction: column; gap: 10px; flex: 1; }}
    .meta-row {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--muted);
    }}
    .wb-tag {{
      font-weight: 700;
      color: #1e293b;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      padding: 2px 8px;
      border-radius: 6px;
      font-size: 12px;
    }}
    .field-label {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
      font-weight: 600;
      color: var(--muted);
      margin-bottom: 4px;
    }}
    .text-box {{
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 13px;
      word-break: break-word;
      white-space: pre-wrap;
      max-height: 92px;
      overflow-y: auto;
    }}
    .btn {{
      border: 1px solid var(--border);
      background: #fff;
      color: var(--text);
      padding: 4px 10px;
      border-radius: 7px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}
    .btn:hover {{ background: #f1f5f9; }}
    .btn-primary {{
      background: var(--primary);
      border-color: var(--primary);
      color: #fff;
    }}
    .btn-primary:hover {{ background: var(--primary-hover); }}
    .dedup-box {{
      background: #f8fafc;
      border: 1px dashed var(--border);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 12px;
      color: #334155;
    }}
    .dedup-links {{ margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }}
    .dedup-links a {{
      color: var(--primary);
      text-decoration: none;
      font-size: 12px;
      background: #eff6ff;
      padding: 2px 7px;
      border-radius: 5px;
    }}
    .dedup-links a:hover {{ text-decoration: underline; }}
    .footer-actions {{
      margin-top: auto;
      padding-top: 10px;
      border-top: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }}
    .pub-toggle {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      user-select: none;
    }}
    .pub-toggle input {{ width: 16px; height: 16px; cursor: pointer; }}
    .failures {{
      margin-top: 22px;
      background: var(--red-bg);
      border: 1px solid var(--red-bd);
      border-radius: 12px;
      padding: 14px 16px;
      font-size: 13px;
      color: var(--red-fg);
    }}
    .toast {{
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: #111827;
      color: #fff;
      padding: 9px 14px;
      border-radius: 8px;
      font-size: 13px;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s;
      z-index: 999;
    }}
    .toast.show {{ opacity: 1; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>douyin2weibo 上传工作台</h1>
        <div class="sub" id="meta-sub">生成时间：{html.escape(generated_at)}</div>
      </div>
      <div class="stats" id="filter-bar"></div>
    </header>
    <div class="grid" id="cards-grid"></div>
    <div id="failures-box"></div>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    const DATA = {payload_json};
    const STORAGE_KEY = "douyin2weibo_published_v1";
    let currentFilter = "all";

    function loadPublished() {{
      try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{{}}"); }}
      catch {{ return {{}}; }}
    }}
    function savePublished(map) {{
      localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
    }}
    function showToast(msg) {{
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.classList.add("show");
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
    }}
    async function copyText(text, label) {{
      try {{
        await navigator.clipboard.writeText(text);
        showToast(label + " 已复制到剪贴板");
      }} catch {{
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        showToast(label + " 已复制到剪贴板");
      }}
    }}
    function fmtDuration(sec) {{
      const s = Math.round(Number(sec) || 0);
      const m = Math.floor(s / 60);
      const r = s % 60;
      return String(m).padStart(2, "0") + ":" + String(r).padStart(2, "0");
    }}
    function esc(str) {{
      return String(str || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}
    function statusEmoji(st) {{
      if (st === "green") return "🟢";
      if (st === "yellow") return "🟡";
      if (st === "red") return "🔴";
      return "⚪";
    }}

    function render() {{
      const pubMap = loadPublished();
      const cards = DATA.cards || [];
      const counts = {{ all: cards.length, unpublished: 0, green: 0, yellow: 0, red: 0 }};
      for (const c of cards) {{
        const key = (c.weibo_name || "") + "::" + c.aweme_id;
        if (!pubMap[key]) counts.unpublished++;
        if (counts[c.dedup_status] !== undefined) counts[c.dedup_status]++;
      }}

      const filters = [
        ["all", "全部 " + counts.all, ""],
        ["unpublished", "未发布 " + counts.unpublished, ""],
        ["green", "🟢 未发现 " + counts.green, "green"],
        ["yellow", "🟡 疑似 " + counts.yellow, "yellow"],
        ["red", "🔴 已发过 " + counts.red, "red"],
      ];
      document.getElementById("filter-bar").innerHTML = filters.map(([k, lbl, cls]) =>
        `<button class="pill ${{cls}} ${{currentFilter === k ? "active" : ""}}" onclick="setFilter('${{k}}')">${{lbl}}</button>`
      ).join("");

      const visible = cards.filter(c => {{
        const key = (c.weibo_name || "") + "::" + c.aweme_id;
        if (currentFilter === "all") return true;
        if (currentFilter === "unpublished") return !pubMap[key];
        return c.dedup_status === currentFilter;
      }});

      const grid = document.getElementById("cards-grid");
      grid.innerHTML = visible.map((c, i) => {{
        const key = (c.weibo_name || "") + "::" + c.aweme_id;
        const isPub = !!pubMap[key];
        const statusCls = "status-" + (c.dedup_status || "off");
        const evHtml = (c.dedup_evidences || []).slice(0, 2).map(ev =>
          `<div>• ${{ev.is_own_account ? "[自家号]" : "[外部]"}} <b>${{esc(ev.author)}}</b> (${{Math.round(ev.similarity * 100)}}%) ${{ev.post_url ? `<a href="${{esc(ev.post_url)}}" target="_blank">查看原微博</a>` : ""}}</div>`
        ).join("");
        const manualHtml = (c.manual_search_urls || []).slice(0, 2).map(u =>
          `<a href="${{esc(u.video_url)}}" target="_blank">搜视频: ${{esc(u.query)}}</a><a href="${{esc(u.all_url)}}" target="_blank">搜综合</a>`
        ).join("");

        return `
          <div class="card ${{isPub ? "published" : ""}}" data-idx="${{i}}">
            <div class="cover-wrap">
              ${{c.cover_rel ? `<img src="${{esc(c.cover_rel)}}" alt="cover" />` : `<div class="cover-placeholder">未下载文件（已跳过或仅查重模式）</div>`}}
              <span class="badge-top ${{statusCls}}">${{statusEmoji(c.dedup_status)}} ${{esc(c.dedup_label)}}</span>
              <span class="badge-dur">${{fmtDuration(c.duration_sec)}}</span>
            </div>
            <div class="body">
              <div class="meta-row">
                <span class="wb-tag">微博：${{esc(c.weibo_name || "未指定")}}</span>
                <span>抖音：${{esc(c.author)}} · ${{esc(c.publish_time)}}</span>
              </div>
              <div class="meta-row">
                <span>分辨率：${{esc(c.resolution_info || "-")}}</span>
                <span>${{c.video_size_mb ? c.video_size_mb + " MB" : ""}}</span>
              </div>

              <div class="dedup-box">
                <div><b>查重：</b>${{esc(c.dedup_summary)}}</div>
                ${{evHtml}}
                ${{manualHtml ? `<div class="dedup-links">${{manualHtml}}</div>` : ""}}
              </div>

              <div>
                <div class="field-label">
                  <span>标题（已去话题，可直接填标题栏）</span>
                  <button class="btn btn-primary" onclick='copyCardField(${{JSON.stringify(c.title)}}, "标题")'>复制标题</button>
                </div>
                <div class="text-box">${{esc(c.title)}}</div>
              </div>

              <div>
                <div class="field-label">
                  <span>简介 / 正文（含原话题）</span>
                  <button class="btn" onclick='copyCardField(${{JSON.stringify(c.desc)}}, "简介")'>复制简介</button>
                </div>
                <div class="text-box">${{esc(c.desc)}}</div>
              </div>

              <div class="footer-actions">
                <div style="display:flex; gap:6px; flex-wrap:wrap;">
                  ${{c.folder_uri ? `<a class="btn" href="${{esc(c.folder_uri)}}" target="_blank">打开文件夹</a>` : ""}}
                  ${{c.folder_native ? `<button class="btn" onclick='copyCardField(${{JSON.stringify(c.folder_native)}}, "文件夹路径")'>复制路径</button>` : ""}}
                  <a class="btn" href="${{esc(c.video_page_url)}}" target="_blank">原抖音</a>
                </div>
                <label class="pub-toggle">
                  <input type="checkbox" ${{isPub ? "checked" : ""}} onchange='togglePub(${{JSON.stringify(key)}}, this.checked)' />
                  <span>已发布</span>
                </label>
              </div>
            </div>
          </div>
        `;
      }}).join("");

      const fails = DATA.failures || [];
      document.getElementById("failures-box").innerHTML = fails.length ? `
        <div class="failures">
          <b>处理失败（${{fails.length}} 条，未中断整批）：</b>
          <ul>${{fails.map(f => `<li><code>${{esc(f.url)}}</code>（${{esc(f.weibo_name || "-")}}）：${{esc(f.error)}}</li>`).join("")}}</ul>
        </div>
      ` : "";
    }}

    window.setFilter = function(k) {{ currentFilter = k; render(); }};
    window.copyCardField = function(text, label) {{ copyText(text, label); }};
    window.togglePub = function(key, checked) {{
      const map = loadPublished();
      if (checked) map[key] = new Date().toISOString();
      else delete map[key];
      savePublished(map);
      render();
    }};

    render();
  </script>
</body>
</html>
"""
    output_html_path.write_text(html_doc, encoding="utf-8")
    return output_html_path
