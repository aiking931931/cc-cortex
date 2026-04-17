---
name: wiredo
description: Use when the user wants to toggle WIREDO checklist enforcement on or off, check WIREDO status, or toggle per-asset-type validation. Triggers on keywords like "wiredo", "WIREDO".
user-invocable: true
disable-model-invocation: true
---

> I don't ship unverified work. Code, image, video, audio, document — every deliverable earns its checkmark or doesn't leave my hands.

# /wiredo — WIREDO 泛化交付標準

根據參數 `$ARGUMENTS` 控制 WIREDO 清單。

## 參數

| 參數 | 動作 |
|------|------|
| `on` | 全開（所有資產類型） |
| `off` | 全關 |
| `status` | 顯示開關狀態（含各資產類型） |
| `on code` | 只開 code 驗證 |
| `off image` | 只關 image 驗證 |
| `on video audio` | 開多個類型 |
| （無參數） | 顯示完整清單模板（所有類型） |

**資產類型**：`code` · `image` · `video` · `audio` · `document` · `media`

## WIREDO 六維 × 五類型

### CODE（代碼）

```text
□ W — Wired：grep 確認有人呼叫/import，刪掉會報錯
□ I — Inherited & Aligned：使用統一模板/基類，放在架構正確位置
□ R — Responsive & Performant：無 O(n²)/N+1/不必要阻塞
□ E — Extensible：可配置值在頂部常數或 config，不硬編碼
□ D — Defended & Verified：lint 零錯誤 + 有測試 + 有驗證證據
□ O — Observable：有 stats/log/metrics（非 SaaS → N/A）
```

### IMAGE（圖片）

```text
□ W — Wired：存入素材庫，不在 tmp/ 孤立
□ I — Inherited & Aligned：命名規則 + 正確資料夾
□ R — Responsive & Performant：≥800px, sRGB, 非黑圖/損壞
□ E — Extensible：元資料完整，生成參數有記錄
□ D — Defended & Verified：檔案存在 + 非空 + 視覺確認
□ O — Observable：N/A（獨立資產）
```

### VIDEO（影片）

```text
□ W — Wired：存入 media/，不在 tmp/
□ I — Inherited & Aligned：命名規則 + 正確資料夾
□ R — Responsive & Performant：H.264/H.265, ≤2Mbps, ≥720p
□ E — Extensible：容器元資料完整，壓縮參數有記錄
□ D — Defended & Verified：ffprobe 通過 + 視覺確認 + 時長正確
□ O — Observable：N/A（獨立資產）
```

### AUDIO（音訊）

```text
□ W — Wired：存入管理目錄，被系統引用
□ I — Inherited & Aligned：命名規則 + 正確資料夾
□ R — Responsive & Performant：44.1/48kHz, -16 LUFS, ≤-1 dBTP
□ E — Extensible：參數為頂部常數，不硬編碼
□ D — Defended & Verified：可播放 + 時長正確 + 無削波
□ O — Observable：N/A（獨立資產）
```

### DOCUMENT（文件）

```text
□ W — Wired：被引用/連結，不孤立
□ I — Inherited & Aligned：統一模板 + 標題結構正確
□ R — Responsive & Performant：合理大小，無斷鏈
□ E — Extensible：日期/版本參數化，不硬編碼
□ D — Defended & Verified：結構有效 + 內容完整
□ O — Observable：N/A（獨立文件）
```

## 級聯驗證（Cascade）

**You MUST** 遵守級聯規則：

| 堆疊 | 底層驗證者 | 上層行為 |
|------|-----------|---------|
| psyche → infinite-agent | infinite-agent | psyche 繼承結果，只驗自身層 |
| aegis → infinite-agent | infinite-agent | aegis 繼承結果，只驗自身層 |

設定在 `cc_config.json` → `wiredo.project_stack`。

## 工作流程

1. **任務開始**：偵測資產類型 → 注入對應 WIREDO 清單
2. **開發過程**：按 WIREDO 方向做（W→I→R→E→D→O）
3. **完成後**：逐項驗證（自動+手動）
4. **最終報告**：必含對應類型的完整 WIREDO 表格

## 執行

1. 讀取 `.claude/hooks/cc_config.json`
2. 解析參數：全開/全關/單類型開關
3. 更新 `wiredo` 區段（含 `enabled` + `asset_types`）
4. 寫回 + 一行確認
