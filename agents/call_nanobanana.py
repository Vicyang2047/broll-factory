#!/usr/bin/env python3
"""call_nanobanana.py — 小兵 #2：把 prompt 變成畫格圖。

讀 build/prompts.json，逐場呼叫 Gemini image model 生圖，輸出到
assets/frames/scene-<id>.png。沒有 GEMINI_API_KEY 時自動退到 mock 模式，
用 Pillow 畫灰色佔位圖，讓整條管線在離線環境也跑得起來。

成本控制：已存在的 frame 跳過、失敗最多重試 3 次、每次動作間 sleep 2 秒。

不會 commit .env、不會 commit assets/frames、不會把 API key 印出來。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

# 路徑都以 repo 根目錄為基準（不受 cwd 影響）。
REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_JSON = REPO_ROOT / "build" / "prompts.json"
GEN_PROMPTS = REPO_ROOT / "agents" / "gen_prompts.py"
FRAMES_DIR = REPO_ROOT / "assets" / "frames"

DEFAULT_MODEL = "gemini-3.1-flash-image-preview"
DEFAULT_SCENES = ["1-1", "3-1"]  # 不加 --all 時只跑這兩場做煙霧測試。

# 速率/重試控制。
MAX_RETRIES = 3
SLEEP_SEC = 2

# Mock 佔位圖尺寸（16:9）。
MOCK_SIZE = (1280, 720)


# --------------------------------------------------------------------------- #
# 載入資料
# --------------------------------------------------------------------------- #
def load_api_key() -> str | None:
    """從 .env（若有 python-dotenv）或環境變數讀 GEMINI_API_KEY。"""
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass  # 沒裝 python-dotenv 就只看環境變數。

    import os

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    return key or None


def load_prompts() -> list[dict[str, Any]]:
    if not PROMPTS_JSON.exists():
        print(f"[nanobanana] 找不到 {PROMPTS_JSON}，先跑 gen_prompts.py …")
        subprocess.run([sys.executable, str(GEN_PROMPTS)], check=True)

    with PROMPTS_JSON.open(encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Mock 模式
# --------------------------------------------------------------------------- #
def _load_font(size: int):
    """盡量找一個可用字型；找不到就用 Pillow 內建點陣字。"""
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]

    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue

    return ImageFont.load_default()


def render_mock(scene: dict[str, Any], out_path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", MOCK_SIZE, (90, 90, 96))
    draw = ImageDraw.Draw(img)

    # 邊框，讓佔位圖一眼看出是 mock。
    draw.rectangle(
        [8, 8, MOCK_SIZE[0] - 8, MOCK_SIZE[1] - 8],
        outline=(160, 160, 168),
        width=4,
    )

    big = _load_font(72)
    mid = _load_font(40)
    small = _load_font(28)

    sid = scene.get("id", "?")
    title = scene.get("title", "")
    section = scene.get("section", "")

    lines = [
        (f"scene-{sid}", big, (235, 235, 240)),
        (title, mid, (210, 210, 218)),
        (f"[{section}]", small, (180, 180, 190)),
        ("MOCK PLACEHOLDER", small, (150, 150, 160)),
    ]

    # 垂直置中堆疊。
    total_h = 0
    measured = []

    for text, font, color in lines:
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        measured.append((text, font, color, w, h))
        total_h += h + 20

    y = (MOCK_SIZE[1] - total_h) // 2

    for text, font, color, w, h in measured:
        x = (MOCK_SIZE[0] - w) // 2
        draw.text((x, y), text, font=font, fill=color)
        y += h + 20

    img.save(out_path, "PNG")


# --------------------------------------------------------------------------- #
# Real API 模式
# --------------------------------------------------------------------------- #
def make_client(api_key: str) -> Any:
    from google import genai  # type: ignore

    return genai.Client(api_key=api_key)


def render_real(client: Any, model: str, scene: dict[str, Any], out_path: Path) -> None:
    from google.genai import types  # type: ignore
    from PIL import Image

    prompt = scene["prompt"]

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["Image"]),
    )

    image_bytes = _extract_image_bytes(response)

    if image_bytes is None:
        raise RuntimeError("回應中找不到影像資料")

    Image.open(BytesIO(image_bytes)).save(out_path, "PNG")


def _extract_image_bytes(response: Any) -> bytes | None:
    """從 google-genai 回應抽出第一張圖的 bytes。"""
    candidates = getattr(response, "candidates", None) or []

    for cand in candidates:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []

        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data

    return None


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def select_scenes(prompts: list[dict[str, Any]], do_all: bool) -> list[dict[str, Any]]:
    if do_all:
        return prompts

    wanted = set(DEFAULT_SCENES)
    return [scene for scene in prompts if scene.get("id") in wanted]


def process_scene(
    scene: dict[str, Any],
    out_path: Path,
    use_mock: bool,
    client: Any,
    model: str,
) -> bool:
    """生成單一場景，含重試。回傳是否成功。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if use_mock:
                render_mock(scene, out_path)
            else:
                render_real(client, model, scene, out_path)

            return True

        except Exception as exc:  # noqa: BLE001 — 任何失敗都重試
            print(
                f"[nanobanana] scene-{scene.get('id')} "
                f"第 {attempt}/{MAX_RETRIES} 次失敗：{exc}"
            )

            if attempt < MAX_RETRIES:
                time.sleep(SLEEP_SEC)

    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--all",
        action="store_true",
        help="處理全部 15 個場景（預設只跑 1-1 和 3-1）",
    )

    parser.add_argument(
        "--mock",
        action="store_true",
        help="強制 mock 模式，不呼叫 API",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="覆蓋已存在的圖片",
    )

    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini image model（預設 {DEFAULT_MODEL}）",
    )

    args = parser.parse_args()

    prompts = load_prompts()
    scenes = select_scenes(prompts, args.all)

    if not scenes:
        sys.exit("[nanobanana] 沒有選到任何場景")

    api_key = None if args.mock else load_api_key()
    use_mock = args.mock or api_key is None

    client = None

    if not use_mock:
        try:
            client = make_client(api_key)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            print(f"[nanobanana] 建立 client 失敗，改用 mock：{exc}")
            use_mock = True

    mode = "MOCK" if use_mock else f"REAL ({args.model})"
    print(f"[nanobanana] 模式：{mode}｜場景數：{len(scenes)}")

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    done = 0
    skipped = 0
    failed = 0

    for scene in scenes:
        sid = scene.get("id")
        out_path = FRAMES_DIR / f"scene-{sid}.png"

        if out_path.exists() and not args.force:
            print(f"[nanobanana] scene-{sid} 已存在，跳過")
            skipped += 1
            continue

        ok = process_scene(scene, out_path, use_mock, client, args.model)

        if ok:
            print(f"[nanobanana] scene-{sid} -> {out_path.relative_to(REPO_ROOT)}")
            done += 1
            time.sleep(SLEEP_SEC)  # 成功後也歇一下，避免速率限制。
        else:
            failed += 1

    print(f"[nanobanana] 完成：生成 {done}｜跳過 {skipped}｜失敗 {failed}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()