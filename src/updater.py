"""Git update checker and applier with automatic GitHub mirror fallback."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import DEFAULT_GITHUB_MIRRORS, load_config

_GITHUB_HTTPS_RE = re.compile(r"https://github\.com/(.+)$")


def run_git(
    args: list[str],
    cwd: Path,
    timeout: int = 20,
    url_rewrite: tuple[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git non-interactively with explicit UTF-8 decoding."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    cmd = ["git"]
    if url_rewrite:
        new_prefix, old_prefix = url_rewrite
        cmd.extend(["-c", f"url.{new_prefix}.insteadOf={old_prefix}"])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _build_attempts(origin_url: str, mirrors: list[str]) -> list[tuple[str, tuple[str, str] | None]]:
    """Build ordered (label, url_rewrite) attempts: direct first, then mirrors."""
    attempts: list[tuple[str, tuple[str, str] | None]] = [("直连", None)]
    clean_origin = (origin_url or "").strip()
    if "https://github.com/" not in clean_origin:
        return attempts

    if clean_origin.startswith("https://github.com/"):
        for m in mirrors:
            base = m.strip().rstrip("/")
            if base:
                attempts.append((f"镜像 {base}", (f"{base}/https://github.com/", "https://github.com/")))
    else:
        # Origin was already set to a mirror URL like https://gh-proxy.com/https://github.com/...
        prefix = clean_origin.split("https://github.com/")[0].rstrip("/") + "/"
        attempts.append(("直连 GitHub", ("https://github.com/", f"{prefix}https://github.com/")))
        for m in mirrors:
            base = m.strip().rstrip("/")
            if base and f"{base}/" != prefix:
                attempts.append((f"镜像 {base}", (f"{base}/https://github.com/", f"{prefix}https://github.com/")))
    return attempts


def _run_with_mirror_fallback(
    git_args: list[str],
    root: Path,
    mirrors: list[str],
    timeout: int = 20,
) -> tuple[subprocess.CompletedProcess[str], str]:
    origin_res = run_git(["remote", "get-url", "origin"], root, timeout=5)
    origin_url = origin_res.stdout.strip() if origin_res.returncode == 0 else ""
    attempts = _build_attempts(origin_url, mirrors)

    last_res: subprocess.CompletedProcess[str] | None = None
    last_label = "直连"
    for idx, (label, rewrite) in enumerate(attempts):
        # Use shorter timeout on first direct probe when mirrors are available
        step_timeout = 12 if (idx == 0 and len(attempts) > 1) else timeout
        try:
            res = run_git(git_args, root, timeout=step_timeout, url_rewrite=rewrite)
        except subprocess.TimeoutExpired:
            res = subprocess.CompletedProcess(
                args=["git", *git_args],
                returncode=124,
                stdout="",
                stderr=f"连接超时（{label}）",
            )
        last_res = res
        last_label = label
        if res.returncode == 0:
            return res, label
        err_lower = (res.stderr or res.stdout or "").lower()
        # Do not retry mirrors on local git conflicts / dirty worktree errors
        if any(k in err_lower for k in ("local changes", "overwritten by merge", "not possible to fast-forward")):
            break
    assert last_res is not None
    return last_res, last_label


def check_for_updates(root: Path, mirrors: list[str] | None = None) -> dict[str, Any]:
    """Check remote updates with automatic GitHub mirror fallback."""
    if not (root / ".git").exists():
        raise RuntimeError("当前目录不是 Git 仓库（请用 git clone 拉取项目）")

    mirror_list = mirrors if mirrors is not None else list(DEFAULT_GITHUB_MIRRORS)
    fetch_res, via = _run_with_mirror_fallback(["fetch", "origin"], root, mirror_list, timeout=20)
    if fetch_res.returncode != 0:
        err_msg = (fetch_res.stderr or fetch_res.stdout or "git fetch 失败").strip()
        raise RuntimeError(err_msg)

    local_c = run_git(["rev-parse", "--short", "HEAD"], root, timeout=5).stdout.strip()
    upstream_ref = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root, timeout=5)
    remote_branch = upstream_ref.stdout.strip() if upstream_ref.returncode == 0 else "origin/main"
    remote_c = run_git(["rev-parse", "--short", remote_branch], root, timeout=5).stdout.strip()
    cnt_raw = run_git(["rev-list", "--count", f"HEAD..{remote_branch}"], root, timeout=5).stdout.strip()
    behind = int(cnt_raw or "0")

    changelog: list[str] = []
    if behind > 0:
        log_raw = run_git(["log", "--format=%h %s", f"HEAD..{remote_branch}", "-n", "6"], root, timeout=5).stdout
        changelog = [ln.strip() for ln in log_raw.splitlines() if ln.strip()]

    return {
        "ok": True,
        "up_to_date": behind == 0,
        "local_commit": local_c,
        "remote_commit": remote_c,
        "behind_count": behind,
        "changelog": changelog,
        "via": via,
    }


def apply_update(root: Path, mirrors: list[str] | None = None) -> dict[str, Any]:
    """Pull fast-forward updates with automatic mirror fallback and sync requirements if changed."""
    if not (root / ".git").exists():
        raise RuntimeError("当前目录不是 Git 仓库")

    mirror_list = mirrors if mirrors is not None else list(DEFAULT_GITHUB_MIRRORS)
    req_file = root / "requirements.txt"
    old_req = req_file.read_text(encoding="utf-8") if req_file.is_file() else ""

    pull_res, via = _run_with_mirror_fallback(["pull", "--ff-only"], root, mirror_list, timeout=30)
    if pull_res.returncode != 0:
        err_msg = (pull_res.stderr or pull_res.stdout or "git pull 失败").strip()
        raise RuntimeError(err_msg)

    out_txt = (pull_res.stdout or "").strip()
    if "Already up to date" in out_txt or "已经是最新的" in out_txt:
        return {"ok": True, "restarting": False, "via": via, "message": f"当前已是最新版本（{via}）"}

    new_req = req_file.read_text(encoding="utf-8") if req_file.is_file() else ""
    if new_req != old_req:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    return {"ok": True, "restarting": True, "via": via, "message": f"更新拉取成功（{via}），正在重启控制台..."}


def main() -> None:
    cfg = load_config()
    try:
        info = check_for_updates(cfg.project_root, cfg.github_mirrors)
        if info["up_to_date"]:
            print(f"当前已是最新版本：{info['local_commit']}（通过 {info['via']}）")
            return
        print(f"发现 {info['behind_count']} 个更新（{info['local_commit']} -> {info['remote_commit']}）：")
        for line in info["changelog"]:
            print(f"  - {line}")
        res = apply_update(cfg.project_root, cfg.github_mirrors)
        print(res["message"].replace("，正在重启控制台...", ""))
    except Exception as exc:
        print(f"[更新失败] {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
