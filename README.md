# broll-factory

B-roll 生成管線 — 和風妖怪傳說主題

## 架構：將軍 / 小兵

| 角色 | 檔案 | 職責 |
|------|------|------|
| 將軍 | `plan/scene-manifest.json` | 場景規格、風格設定（你來改） |
| 小兵 1 | `agents/gen_prompts.py` | 讀 manifest → 展開 prompt |
| 小兵 2 | `agents/call_nanobanana.py` | 打 Gemini API → 出圖（Phase 2） |
| 小兵 3 | `agents/assemble.py` | 圖 + 音軌 → mp4（Phase 3） |

## Phase 1 — 展開 Prompt（現在可用）

```bash
pip install --break-system-packages google-genai pillow imageio-ffmpeg
python3 agents/gen_prompts.py
```

輸出在 `build/prompts/`，打開任一 `.txt` 確認 prompt 正確。

## Phase 2/3 — 出圖 + 組裝（即將加入）

需要在根目錄建立 `.env`（不要 commit）：

```
GEMINI_API_KEY=你的金鑰
```

## 注意事項

- 改場景設定 → 只改 `plan/scene-manifest.json`，腳本自動跟著更新
- `build/` 和 `.env` 已加入 `.gitignore`，不會被 commit
