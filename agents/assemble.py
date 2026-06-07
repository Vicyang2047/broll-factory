#!/usr/bin/env python3
"""assemble.py — 小兵 #3：把畫格 + 運鏡 + 音軌組成 mp4。

讀 build/prompts.json 取得場景順序、duration_sec、motion、fps、resolution，
對每張 assets/frames/scene-<id>.png 套用對應運鏡（ken_burns / pan / shake …），
逐格算出 crop 視窗後縮放輸出，串成一支影片；若 assets/audio/ 有對應
audio_segment 的音檔就對齊混音，沒有就輸出純畫面（silent）。

輸出：build/broll.mp4

依賴：pillow、imageio-ffmpeg（提供 ffmpeg 執行檔）。不呼叫任何雲端 API。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_JSON = REPO_ROOT / "build" / "prompts.json"
GEN_PROMPTS = REPO_ROOT / "agents" / "gen_prompts.py"
FRAMES_DIR = REPO_ROOT / "assets" / "frames"
AUDIO_DIR = REPO_ROOT / "assets" / "audio"
DEFAULT_OUTPUT = REPO_ROOT / "build" / "broll.mp4"

# crop 視窗在 base 畫布內可平移的邊界（base = target × 此係數）。
PAN_MARGIN = 1.25
# Ken Burns 類最大放大倍率對應的最小 crop。
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac")


# --------------------------------------------------------------------------- #
# 緩動 / 運鏡參數
# --------------------------------------------------------------------------- #
def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


# 每種 motion -> (z0, z1, px0, py0, px1, py1, jitter_px, ease)
# z 為放大倍率（>1 = 放大）；p* 為 crop 在可平移範圍內的比例 [0,1]；
# jitter_px 為每格隨機抖動幅度（像素）；ease 為 "smooth" 或 "linear"。
MOTION_TABLE: dict[str, dict[str, Any]] = {
    "ken_burns_in":         dict(z0=1.00, z1=1.22, px0=0.5, py0=0.5, px1=0.5, py1=0.5, jitter=0, ease="smooth"),
    "ken_burns_out":        dict(z0=1.22, z1=1.00, px0=0.5, py0=0.5, px1=0.5, py1=0.5, jitter=0, ease="smooth"),
    "ken_burns_up":         dict(z0=1.10, z1=1.20, px0=0.5, py0=1.0, px1=0.5, py1=0.0, jitter=0, ease="smooth"),
    "static_slow_drift":    dict(z0=1.06, z1=1.08, px0=0.4, py0=0.5, px1=0.6, py1=0.5, jitter=0, ease="linear"),
    "horizontal_pan":       dict(z0=1.06, z1=1.06, px0=0.0, py0=0.5, px1=1.0, py1=0.5, jitter=0, ease="smooth"),
    "handheld_shake":       dict(z0=1.10, z1=1.10, px0=0.5, py0=0.5, px1=0.5, py1=0.5, jitter=7, ease="linear"),
    "slow_orbit":           dict(z0=1.10, z1=1.12, px0=0.35, py0=0.45, px1=0.65, py1=0.55, jitter=0, ease="smooth"),
    "rapid_zoom_in":        dict(z0=1.00, z1=1.30, px0=0.5, py0=0.5, px1=0.5, py1=0.5, jitter=2, ease="smooth"),
    "slow_motion_drift":    dict(z0=1.10, z1=1.16, px0=0.5, py0=0.6, px1=0.5, py1=0.4, jitter=0, ease="linear"),
    "fast_horizontal_swipe":dict(z0=1.10, z1=1.10, px0=0.0, py0=0.5, px1=1.0, py1=0.5, jitter=0, ease="linear"),
    "camera_shake":         dict(z0=1.14, z1=1.14, px0=0.5, py0=0.5, px1=0.5, py1=0.5, jitter=14, ease="linear"),
    "whip_pan":             dict(z0=1.10, z1=1.10, px0=0.0, py0=0.5, px1=1.0, py1=0.5, jitter=0, ease="linear"),
    "freeze_with_jitter":   dict(z0=1.15, z1=1.15, px0=0.5, py0=0.5, px1=0.5, py1=0.5, jitter=4, ease="linear"),
    "follow_fall":          dict(z0=1.10, z1=1.10, px0=0.5, py0=0.0, px1=0.5, py1=1.0, jitter=0, ease="linear"),
    "static_ripple":        dict(z0=1.00, z1=1.03, px0=0.5, py0=0.5, px1=0.5, py1=0.5, jitter=0, ease="smooth"),
}
# 未列出的 motion 退回此預設（緩慢推近）。
DEFAULT_MOTION = dict(z0=1.0, z1=1.08, px0=0.5, py0=0.5, px1=0.5, py1=0.5, jitter=0, ease="smooth")


# --------------------------------------------------------------------------- #
# 載入 / 工具
# --------------------------------------------------------------------------- #
def load_scenes() -> list[dict[str, Any]]:
    if not PROMPTS_JSON.exists():
        print(f"[assemble] 找不到 {PROMPTS_JSON}，先跑 gen_prompts.py …")
        subprocess.run([sys.executable, str(GEN_PROMPTS)], check=True)
    with PROMPTS_JSON.open(encoding="utf-8") as fh:
        return json.load(fh)


def parse_resolution(value: str | None) -> tuple[int, int]:
    if value and "x" in value:
        w, h = value.lower().split("x", 1)
        return int(w), int(h)
    return 1920, 1080


def ffmpeg_exe() -> str:
    import imageio_ffmpeg  # type: ignore

    return imageio_ffmpeg.get_ffmpeg_exe()


def cover_resize(img: Any, w: int, h: int) -> Any:
    """縮放到剛好覆蓋 w×h（保持比例），再置中裁切。"""
    from PIL import Image

    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w, new_h = max(w, round(src_w * scale)), max(h, round(src_h * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))


# --------------------------------------------------------------------------- #
# 逐場算格
# --------------------------------------------------------------------------- #
def render_scene_frames(
    scene: dict[str, Any],
    target: tuple[int, int],
    fps: int,
    out_dir: Path,
    start_index: int,
    rng: Any,
) -> int:
    """把單一場景展開成多張畫格，寫入 out_dir（六位數連號）。回傳寫了幾格。"""
    from PIL import Image

    target_w, target_h = target
    sid = scene.get("id")
    duration = float(scene.get("duration_sec", 1) or 1)
    n_frames = max(1, round(duration * fps))
    motion = MOTION_TABLE.get(scene.get("motion", ""), DEFAULT_MOTION)

    src_path = FRAMES_DIR / f"scene-{sid}.png"
    if not src_path.exists():
        raise FileNotFoundError(f"缺少畫格 {src_path}（先跑 call_nanobanana.py）")

    base_w = round(target_w * PAN_MARGIN)
    base_h = round(target_h * PAN_MARGIN)
    with Image.open(src_path) as raw:
        base = cover_resize(raw.convert("RGB"), base_w, base_h)

    ease = smoothstep if motion["ease"] == "smooth" else (lambda t: t)
    jitter = motion["jitter"]

    for i in range(n_frames):
        t = 0.0 if n_frames == 1 else i / (n_frames - 1)
        te = ease(t)

        z = lerp(motion["z0"], motion["z1"], te)
        crop_w = min(base_w, round(target_w / z))
        crop_h = min(base_h, round(target_h / z))

        px = lerp(motion["px0"], motion["px1"], te)
        py = lerp(motion["py0"], motion["py1"], te)
        max_ox = base_w - crop_w
        max_oy = base_h - crop_h
        ox = px * max_ox
        oy = py * max_oy

        if jitter:
            ox += rng.uniform(-jitter, jitter)
            oy += rng.uniform(-jitter, jitter)

        ox = int(min(max(ox, 0), max_ox))
        oy = int(min(max(oy, 0), max_oy))

        frame = base.crop((ox, oy, ox + crop_w, oy + crop_h))
        if (crop_w, crop_h) != (target_w, target_h):
            frame = frame.resize((target_w, target_h), Image.LANCZOS)
        frame.save(out_dir / f"{start_index + i:06d}.png")

    return n_frames


# --------------------------------------------------------------------------- #
# 音軌
# --------------------------------------------------------------------------- #
def find_audio(segment: str) -> Path | None:
    for ext in AUDIO_EXTS:
        candidate = AUDIO_DIR / f"{segment}{ext}"
        if candidate.exists():
            return candidate
    return None


def build_audio(scenes: list[dict[str, Any]], work: Path, ffmpeg: str) -> Path | None:
    """依各場景 audio_segment 串出與影片等長的音軌；任何段缺檔則該段補靜音。
    若完全沒有任何音檔則回傳 None（輸出純畫面）。"""
    available = {s.get("audio_segment") for s in scenes if find_audio(s.get("audio_segment", ""))}
    if not available:
        return None

    parts: list[Path] = []
    for idx, scene in enumerate(scenes):
        seg = scene.get("audio_segment", "")
        dur = float(scene.get("duration_sec", 1) or 1)
        part = work / f"a{idx:03d}.wav"
        src = find_audio(seg)
        if src is None:
            # 該段沒音檔 -> 補等長靜音。
            cmd = [
                ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{dur:.3f}", "-c:a", "pcm_s16le", str(part),
            ]
        else:
            # 取該段音樂，循環後裁切到場景長度。
            cmd = [
                ffmpeg, "-y", "-stream_loop", "-1", "-i", str(src),
                "-t", f"{dur:.3f}", "-ar", "44100", "-ac", "2",
                "-c:a", "pcm_s16le", str(part),
            ]
        subprocess.run(cmd, check=True, capture_output=True)
        parts.append(part)

    concat_list = work / "audio_concat.txt"
    concat_list.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    audio_out = work / "audio.wav"
    subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", str(audio_out)],
        check=True, capture_output=True,
    )
    return audio_out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> None:
    import random

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="輸出 mp4 路徑")
    parser.add_argument("--fps", type=int, default=0, help="覆寫 fps（預設取 manifest）")
    parser.add_argument("--resolution", default="", help="覆寫解析度，如 1280x720")
    parser.add_argument("--preview", action="store_true", help="預覽模式：960x540 + 半 fps，快速試跑")
    parser.add_argument("--scenes", default="", help="只組指定場景，逗號分隔，如 1-1,3-1")
    parser.add_argument("--no-audio", action="store_true", help="不混音，輸出純畫面")
    parser.add_argument("--seed", type=int, default=42, help="抖動亂數種子（可重現）")
    args = parser.parse_args()

    scenes = load_scenes()
    if args.scenes:
        wanted = {s.strip() for s in args.scenes.split(",") if s.strip()}
        scenes = [s for s in scenes if s.get("id") in wanted]
    if not scenes:
        sys.exit("[assemble] 沒有選到任何場景")

    fps = args.fps or int(scenes[0].get("fps", 24) or 24)
    resolution = args.resolution or scenes[0].get("resolution", "1920x1080")
    target = parse_resolution(resolution)
    if args.preview:
        fps = max(1, fps // 2)
        target = (960, 540)

    ffmpeg = ffmpeg_exe()
    rng = random.Random(args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="assemble_"))
    frames_dir = work / "frames"
    frames_dir.mkdir()

    try:
        total = 0
        for scene in scenes:
            n = render_scene_frames(scene, target, fps, frames_dir, total, rng)
            total += n
            print(f"[assemble] scene-{scene.get('id')} [{scene.get('motion')}] -> {n} 格")
        print(f"[assemble] 共 {total} 格 @ {fps}fps {target[0]}x{target[1]}（約 {total / fps:.1f} 秒）")

        silent_video = work / "video.mp4"
        subprocess.run(
            [ffmpeg, "-y", "-framerate", str(fps), "-i", str(frames_dir / "%06d.png"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), str(silent_video)],
            check=True, capture_output=True,
        )

        audio = None if args.no_audio else build_audio(scenes, work, ffmpeg)
        if audio is None:
            shutil.move(str(silent_video), str(args.output))
            note = "（純畫面，無音軌）" if args.no_audio else "（未找到 assets/audio/，輸出純畫面）"
        else:
            subprocess.run(
                [ffmpeg, "-y", "-i", str(silent_video), "-i", str(audio),
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(args.output)],
                check=True, capture_output=True,
            )
            note = "（含音軌）"

        print(f"[assemble] 完成 -> {args.output} {note}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()