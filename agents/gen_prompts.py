#!/usr/bin/env python3
"""
gen_prompts.py — B-roll Factory Phase 1
讀取 plan/scene-manifest.json，用 style.prompt_template
把每個場景展開成完整的影像生成 prompt。

輸出：
  build/prompts/scene-<id>.txt   每個場景一個 prompt 文字檔
  build/prompts.json             所有場景的彙整 JSON
"""

import json
import sys
from pathlib import Path

MANIFEST_PATH = Path("plan/scene-manifest.json")
OUTPUT_DIR    = Path("build/prompts")
OUTPUT_JSON   = Path("build/prompts.json")

REQUIRED_KEYS = {"subject", "details", "lighting", "mood", "camera_desc"}


def load_manifest(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"[ERROR] 找不到 manifest：{path}\n"
            f"  請確認 plan/scene-manifest.json 存在於 repo 根目錄。"
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_subs(style: dict, scene_prompt: dict) -> dict:
    """把全域 style 和場景 prompt 欄位合併成一個替換字典。"""
    return {
        "aesthetic":     style.get("aesthetic", ""),
        "era_reference": style.get("era_reference", ""),
        "palette":       ", ".join(style.get("palette", [])),
        "avoid":         ", ".join(style.get("avoid", [])),
        **scene_prompt,          # subject / details / lighting / mood / camera_desc
    }


def expand(template: str, scene: dict, style: dict) -> str:
    sp = scene.get("prompt", {})
    missing = REQUIRED_KEYS - sp.keys()
    if missing:
        raise ValueError(f"缺少欄位：{missing}")
    return template.format(**build_subs(style, sp))


def main():
    manifest = load_manifest(MANIFEST_PATH)
    style    = manifest["style"]
    template = style["prompt_template"]
    scenes   = manifest["scenes"]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    errors  = 0

    for scene in scenes:
        sid   = scene["id"]
        title = scene.get("title", sid)
        try:
            prompt = expand(template, scene, style)
        except (KeyError, ValueError) as e:
            print(f"[WARN] 跳過 {sid}：{e}", file=sys.stderr)
            errors += 1
            continue

        out = OUTPUT_DIR / f"scene-{sid}.txt"
        out.write_text(prompt, encoding="utf-8")

        results.append({
            "id":            sid,
            "section":       scene.get("section", ""),
            "title":         title,
            "duration_sec":  scene.get("duration_sec"),
            "motion":        scene.get("motion"),
            "audio_segment": scene.get("audio_segment"),
            "prompt":        prompt,
            "file":          str(out),
        })
        print(f"  ✓  {sid:<6} {title}")

    OUTPUT_JSON.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    total = len(scenes)
    ok    = len(results)
    print(f"\n{'[OK]' if not errors else '[WARN]'} {ok}/{total} 個場景展開完成")
    print(f"  prompts → {OUTPUT_DIR}/")
    print(f"  彙整    → {OUTPUT_JSON}")
    if errors:
        print(f"  警告：{errors} 個場景因欄位缺失而略過，請檢查 manifest。")


if __name__ == "__main__":
    main()
