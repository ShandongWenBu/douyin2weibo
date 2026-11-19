"""FFmpeg 1080P transcoding and cover JPG compression (<= 5MB)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .config import TranscodeConfig


@dataclass
class ProbeInfo:
    width: int
    height: int
    duration_sec: float
    video_codec: str
    audio_codec: str | None


def probe_video(ffprobe_path: str, video_path: Path) -> ProbeInfo:
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height:format=duration",
        "-of", "json",
        str(video_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    v_stream = None
    a_stream = None
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and v_stream is None:
            v_stream = s
        elif s.get("codec_type") == "audio" and a_stream is None:
            a_stream = s
    if not v_stream:
        raise RuntimeError(f"未检测到视频流：{video_path}")
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    return ProbeInfo(
        width=int(v_stream.get("width") or 0),
        height=int(v_stream.get("height") or 0),
        duration_sec=dur,
        video_codec=str(v_stream.get("codec_name") or ""),
        audio_codec=str(a_stream.get("codec_name")) if a_stream else None,
    )


def transcode_to_1080p(
    ffmpeg_path: str,
    ffprobe_path: str,
    src_video: Path,
    dst_video: Path,
    cfg: TranscodeConfig,
) -> ProbeInfo:
    """Transcode source video to crisp 1080P H.264 MP4 (or fast-remux if already 1080P H.264)."""
    info = probe_video(ffprobe_path, src_video)
    is_portrait = info.height > info.width
    short_side = min(info.width, info.height)

    dst_video.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst_video.with_suffix(".tmp.mp4")

    can_fast_copy = (
        cfg.copy_if_1080p
        and short_side == 1080
        and info.video_codec.lower() == "h264"
    )
    if can_fast_copy:
        cmd = [
            ffmpeg_path,
            "-y",
            "-i", str(src_video),
            "-c:v", "copy",
            "-movflags", "+faststart",
        ]
    else:
        scale_expr = "scale=1080:-2:flags=lanczos" if is_portrait else "scale=-2:1080:flags=lanczos"
        sharpen = cfg.upscale_sharpen if short_side < 1080 else cfg.downscale_sharpen
        vf = f"{scale_expr},{sharpen}" if sharpen else scale_expr
        cmd = [
            ffmpeg_path,
            "-y",
            "-i", str(src_video),
            "-vf", vf,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", str(cfg.crf),
            "-preset", cfg.preset,
            "-movflags", "+faststart",
        ]

    if info.audio_codec:
        if info.audio_codec.lower() in {"aac", "mp3"}:
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.extend(["-c:a", "aac", "-b:a", cfg.audio_bitrate])
    cmd.append(str(tmp_dst))

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if tmp_dst.exists():
            tmp_dst.unlink()
        raise RuntimeError(f"ffmpeg 转码失败：{proc.stderr[-600:]}")

    tmp_dst.replace(dst_video)
    return probe_video(ffprobe_path, dst_video)


def process_cover_to_jpg(src_cover: Path, dst_cover: Path, max_mb: float = 4.8) -> int:
    """Convert cover image to RGB JPG and ensure file size stays within max_mb."""
    dst_cover.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(max_mb * 1024 * 1024)

    with Image.open(src_cover) as img:
        rgb = img.convert("RGB")
        for quality in (95, 90, 85, 78, 70):
            rgb.save(dst_cover, format="JPEG", quality=quality, optimize=True)
            size = dst_cover.stat().st_size
            if size <= max_bytes:
                return size
        # If still oversized (unlikely), resize proportionally
        w, h = rgb.size
        scale = 0.8
        while dst_cover.stat().st_size > max_bytes and scale >= 0.4:
            resized = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
            resized.save(dst_cover, format="JPEG", quality=85, optimize=True)
            scale -= 0.15
    return dst_cover.stat().st_size
