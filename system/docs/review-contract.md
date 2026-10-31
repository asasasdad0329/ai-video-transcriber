# 複核與畫面轉錄契約

本文件是 `video-transcribe` 的內容交付介面。命令在專案根目錄執行；`<run_dir>` 指 `process` 回傳的 `工作資料/任務/轉錄/<hash>/`。所有 JSON 使用 UTF-8，ID 從該 run 產物取得，不以檔名或時間自行猜造。

## 小檔先讀與狀態界線

先用精簡 `status` 定位狀態，疑點只取局部上下文與相關證據。正常交付採 [一次成稿流程](final-review.md)：局部校字、畫面、排版與正規化先暫存並通過 seal-draft，封存候選全文只讀一次，剩餘問題以一個 changes 批次隨最後提交處理。模型不讀 `transcript.raw.json`、完整語音片段陣列或完整索引歷史；原始資料與覆蓋檢查留在本機。`review-bundle` 等舊介面保留相容用途，不是小修字的必要步驟。

自動分類漏掉疑點仍有可能。`keep`、更正成功或待複核清空，只描述已經做過的處理，不能據此宣稱全片每個字都聽音核對過。保留「本機 ASR 原稿」「有證據的更正」「待確認」「畫面轉錄」之間的可追溯關係。

## 更正 JSON

以下是程式生成正式 Patch 的資料契約。正常流程由 `finish-review` 補齊；模型在封存前使用 `edit-draft` 的短指令或最小更正清單，唯一全文閱讀後則提交一份含 review_issues 的完整批次，不手寫整份正式 Patch 契約。

頂層格式：

```json
{
  "run_id": "<run 中的實際 run_id>",
  "patches": [
    {
      "segment_id": "s000001",
      "old_text": "目前該段的精確文字",
      "new_text": "更正後文字",
      "decision": "correct",
      "evidence_ids": ["<bundle 中的證據 ID>"],
      "reason": "指出哪項證據支持哪個更正，以及仍存在的限制。"
    }
  ]
}
```

`decision` 的使用：

| 值 | 何時使用 | `new_text` |
| --- | --- | --- |
| `correct` | 證據足以支持一項最小更正 | 僅修改有依據的文字 |
| `keep` | 已檢查該疑點，有理由保留原文 | 與 `old_text` 相同 |
| `needs_review` | 語音、文字或畫面仍有歧義，現有證據無法解決 | 與 `old_text` 相同 |

每個 patch 的 `old_text` 必須精確對應套用前的文字，包括標點。每份 patch 檔對同一 segment 最多交付一次決定。先前已有更正時，從目前狀態取文字，不拿過期 bundle 的文字強制覆蓋；被拒絕的 patch 要重新讀相關狀態後修正。

`evidence_ids` 可引用該 run 已提供的 frame／音訊／詞彙證據識別碼，以及 `text:<segment_id>`（目前正文的上下文證據）。只引用 segment 本身不足以證明其中的 ASR 文字正確；`reason` 應說明同音關係、前後文語義、重複術語、同段圖表等如何支持該更正。文字推論是有效的校字依據，不必假稱已聽音；多個合理候選無法排除時才保留 needs_review。

### 最小決議與唯讀預檢

`decision-bundle <run_dir> --segment-id s000001` 回傳當前 `run_id`、完整正文的 `transcript_sha256`、所選段落與合法證據 ID；只供舊任務相容或無模型的本機除錯。model session 一律拒絕此命令，不能靠反覆選段重建全文；正常流程用 focused-review 與任務累計十二段上限的 draft-excerpt。這是版本快照，不會自動記錄已閱讀／已聽音。證據 ID 清單只表示可以引用，不能代替實際讀取證據。

模型可只產生下列最小決議，省略容易抄錯的 `old_text`：

```json
{
  "run_id": "<run_id>",
  "transcript_sha256": "<decision-bundle 的值>",
  "decisions": [
    {
      "segment_id": "s000001",
      "decision": "correct",
      "new_text": "有證據支持的更正",
      "evidence_ids": ["text:s000002"],
      "reason": "具體證據與更正理由"
    }
  ]
}
```

`keep`／`needs_review` 可省略 `new_text`，程式保留原文。`assemble-patch <run_dir> <decisions.json> --output <new-patch.json>` 依當前正文補齊欄位，累積回報格式、未知 ID、空更正與版本問題；有錯不輸出部分修正檔。輸出須為不存在的新檔，不覆寫既有資料。產物保留 `transcript_sha256`，後续 `apply` 會再檢查，正文若已變動就拒絕套用。既有無此欄位的 patch 仍按原契約驗證精確原文。

`preflight <run_dir> <patch.json> --layout <reading_layout.json>` 是相容介面的唯讀檢查，正常流程由 `finish-review` 內部執行一次，不另跑此命令。檢查包含原文、版本、證據 ID、重複決議、只加標點、正文按序恰一次與畫面卡恰一次覆蓋。格式通過不能證明校字內容正確；不得因此自動填上已閱讀或已聽音。機械組裝不決定語意斷句與標點。

保持逐字稿的口語內容與原意，不把語病全面潤飾、不補上講者未說的話、不因領域常識與講者不同就改寫原話。繁簡或標點轉換若由既定管線處理，沿用其設定；不在人工複核中任意改變整份文本的文體。

舊介面的獨立套用（正常成稿不再接此步驟）：

```powershell
.\run.ps1 apply "<run_dir>" "<run_dir>\patch.json"
```

## 證據與能力聲明

| 實際操作 | 可以聲稱 | 不能據此聲稱 |
| --- | --- | --- |
| 讀 ASR 文字及相鄰上下文 | 已做文字複核 | 已聽取原音 |
| 讀 OCR 輸出 | OCR 結果包含某字串 | 已確認投影片畫面正確 |
| 開啟並檢查 frame 圖片 | 已檢查該幀可見內容 | 已看完整段影片 |
| 執行局部 `recheck` | 局部 ASR 產生另一候選 | 已由人耳或聽覺模型確認 |
| 實際透過支援工具聆聽音訊 | 已聆聽該段音訊 | 未聽到的其他片段也已驗證 |

OCR 可能讀錯；投影片也可能與口述不同。分清楚哪項證據支援術語拼寫、哪項反映講者說法，不讓看似合理的補全掩蓋衝突。需要檢查時，打開 bundle 指向的實際圖片，不以縮圖檔名或 OCR 信心值代替。

目前的 `recheck` 用同一個本機模型、beam 8 重辨識指定片段。它不是另一個模型的獨立票，也沒有把 OCR 或術語表當作中文 hotwords 傳入 ASR。所用 Windows whisper.cpp 執行檔沒有 prompt-file 介面，窄字元 argv 不能安全傳遞中文 prompt，因此中文術語與 OCR 只交給內容複核使用；不要為重查自行拼接中文 prompt 命令或強制下載新模型。


## 畫面转錄 JSON

從 CLI status 取得預期 visuals_sha256，或依 visuals.json 計算 canonical JSON SHA-256（UTF-8、ensure_ascii=False、sort_keys=True、separators=(',', ':')）。只在真正看過來源畫面後標 reviewed:true。

```json
{
  "run_id": "<run 的實際 ID>",
  "visuals_sha256": "<當前 visuals.json 的 canonical SHA-256>",
  "cards": [
    {
      "id": "screen-001",
      "kind": "table",
      "title": "畫面原局",
      "frame_ids": ["f000001"],
      "verbatim_text": "畫面實際文字，保持原字；不清楚處明寫〔不清〕。",
      "structure": "| 畫面列 | 左一 | 左二 | 左三 | 左四 |\n| --- | --- | --- | --- | --- |\n| 天干 | 丙 | 辛 | 辛 | 癸 |",
      "uncertainties": [],
      "reviewed": true
    }
  ]
}
```

kind 可為 text、paragraph、slide、table、chart、diagram、scene、caption、other。每卡至少有 verbatim_text 或 structure；每個抽樣 frame ID 必須恰好被分派到一張卡，不能漏幀或用未知 ID。多幀共用一張卡時，只能合併同一主要圖卡，並在 structure 明列有變動的文字；變動的擷取時間和來源 ID 寫在以「擷取註記：」開頭的行，主稿會略過這些行。畫面本身出現的時間（如排程表的 09:00）是內容，照原樣寫在其他行。

structure 的一般說明行也可使用 `00:05:40 新增箭頭`：主稿只刪除行首的完整 `HH:MM:SS`（兩位時、分、秒，分秒為 00–59），後面須接空白或行尾。允許行首 0–3 個空格；不刪句中時間、短格式 `MM:SS`、表格列、反引號／波浪號程式碼圍欄或縮排程式碼。不再比對 frame_times。真正的時間若恰好位於一般說明行首且符合此格式，也會被移除；可見原文的時間應放 verbatim_text 或表格，這兩者不套用此清除規則。

主圖完整轉錄：原局的全部可見欄位、主星、干支、藏干、表格全部列欄、圖表單位圖例、PPT註記、流程圖節點與箭頭都要記錄。原本顯示的 X 保留為 X，不能當成 OCR 缺字而補成命理常識。沒有標示年/月/日/時，不得只憑位置宣稱畫面有該標籤，可用左一至左四。

verbatim_text 保留原字；畫面中的路徑、網址、指令、程式碼、檔名等字面內容用反引號（`）包住。主稿轉繁體與台灣用詞時，反引號內文字、網址及 Windows 路徑一律保留原字。structure 用 Markdown 表格、Mermaid 或純文字重建關係；這是轉錄而非摘要、推論或講義。旁白沒說但畫面有寫的內容，放在畫面區塊，不能塞進逐字正文。

字幕可依抽樣時間另列；若只依 OCR 未逐字看圖核對，明寫此限制。模糊／遮擋文字列 uncertainties 並保留截圖。reviewed:true 代表實際查看卡片所依據的主要畫面，不代表已聽音、所有字幕都校過或影片逐幀看完。

若原圖動畫沒顯示完全，先在相關時間區間補幀；若無法取得完整狀態，保留 uncertainty，不能用常識補全。覆蓋所有抽樣幀與覆蓋全片所有畫面是不同事情。

```powershell
.\run.ps1 finalize-visuals "<run_dir>" "<run_dir>\visuals.reviewed.json"
```

主交付檔是 `工作資料/輸出/逐字稿/<影片原檔名去掉副檔名>.md`（status 的 `primary_transcript`），同名碰撞才附加 run ID：依範例整理成自然段落與小節標題，段落旁直接寫出畫面文字、表格與關係，不含路徑、超連結、圖片引用、逐句時間碼或片段 ID。單一檔案就能完整閱讀與轉交。raw／corrected 中間稿與時間對齊在 run/exports/transcripts；原始證據留在 run。講義與摘要不在此流程內。

## 舊版全文上下文校字介面

此節只記錄相容格式；正常流程已改成封存候選只讀一次。不要呼叫以下入口重讀原始正文，也不要為小修字重建 plan、版本欄位或整份 ID 清單。`finish-review` 只在模型明確表示完成唯一候選閱讀，並交出單一剩餘問題批次後，保存實際閱讀方式及版本。

`context-bundle <run_dir>` 會建立含全部精簡正文及圖卡的舊版檔案；只供舊任務相容或本機除錯。模型不得呼叫此入口或讀取產物，CLI 在偵測到 model session 時也會拒絕；正常流程唯一一次全文閱讀由 `read-draft` 提供。

完整檢查正文和圖卡後，將下列欄位附在更正 JSON 頂層，再執行既有 `apply`。值取自当前 context_bundle；無需為每句產生 keep patch，patches 只列更正與待確認。空 patches 也可記錄已完成檢查。

```json
{
  "run_id": "<run_id>",
  "patches": [],
  "context_review": {
    "transcript_sha256": "<讀取時正文雜湊>",
    "visual_review_sha256": "<讀取時畫面轉錄雜湊>",
    "reviewed_segment_ids": ["<依序列出所有已讀 segment ID>"],
    "reviewed_card_ids": ["<依序列出所有已讀 card ID>"]
  }
}
```

覆蓋紀錄必須反映實際閱讀，不能只複製 ID 就宣稱完成。程式檢查版本與覆蓋，並記錄套用更正後的正文雜湊；它不判斷推論是否正確，也不代表已聽音。正文或畫面轉錄改變會使紀錄過期，狀態轉為 `awaiting_context_review`；已有有效紀錄則沿用。口述校正透過 patch，畫面誤讀透過 `finalize-visuals`；推論出的不可見內容不寫成画面原文。

## 閱讀版排版契約

### 舊版修正與排版組裝

以下介面保留給既有實驗與指定的範圍式 plan。正常流程使用 `start-draft`／`edit-draft` 累積前置工作、`seal-draft` 封存，不必先寫 plan。

`review-plan-bundle <run_dir>` 會取得當前完整正文、圖卡與版本，只供既有實驗或本機除錯。model session 的 CLI 會拒絕此入口；正常轉錄任務改用 `start-draft`／`edit-draft`／`seal-draft`，由本機程式保留完整正文與 ID。

```json
{
  "run_id": "<bundle run_id>",
  "transcript_sha256": "<bundle transcript_sha256>",
  "visual_review_sha256": "<bundle visual_review_sha256，尚無圖卡時為 null>",
  "decisions": [],
  "layout": {
    "title": "影片名稱",
    "protected_terms": ["丑"],
    "sections": [
      {
        "title": "本節主題",
        "blocks": [
          {"start_segment_id": "s000001", "end_segment_id": "s000003", "punctuation": {"s000001": "，", "s000003": "。"}},
          {"card_id": "screen-001"}
        ]
      }
    ]
  }
}
```

`decisions` 採前述最小決議契約，只列必要更正／待確認。範圍含首尾兩段，以實際正文順序展開，不按 ID 數字補造；所有正文仍須按序恰一次、每張圖卡恰一次。模型決定標點、段落與圖卡位置，程式不猜語意。圖卡只能引用，不能透過 plan 改寫；`include_structure` 若提供必須是布林值，省略即保留結構。原有 `needs_review` 會保留到預覽，除非該段有新的明確決議。

```powershell
.\run.ps1 assemble-review "<run_dir>" "<plan.json>" --output-dir "工作資料/歸檔/開發紀錄/<new-candidate>"
```

候選目錄必須不存在且位於轉錄 run 之外，產生 `patch.json`、`reading_layout.json`、`preview.md`、`reading_map.json`、`check.json`，最後才寫有完整檔案雜湊的 `manifest.json`。有格式／版本問題不產生候選。命令不執行 `apply`／`resume`、不覆寫來源或正式主稿，也不宣稱全文語意／音訊已通過檢核。

候選用於內容比對；差異本身不是錯誤。正式交付改用一次成稿流程；既有組裝產物不自動代表已完成全文閱讀。既有成品對照實驗只輸出差異，未要求修正時不要自動改回。

### 展開後的既有格式

在 run 內建立 `reading_layout.json`，只決定版面，不另寫一份改述口述。所有 segment ID 必須按原順序恰好出現一次，每張畫面卡也恰出現一次。正文由程式從更正稿取得，layout 只允許繁體轉換與標點整理；發現錯字先交付 patch，不能以「排版不得改字」為由略過校正。小節標題可以由整理者命名。

```json
{
  "title": "影片名稱",
  "protected_terms": ["丑"],
  "sections": [
    {
      "title": "本節主題",
      "blocks": [
        {
          "segment_ids": ["s000001", "s000002"],
          "punctuation": {"s000001": "，", "s000002": "。"}
        },
        {"card_id": "screen-001"}
      ]
    }
  ]
}
```

`protected_terms` 只填已確認需保留原字的歧義字／名稱，例如命理地支「丑」不能轉成「醜」。`punctuation` 為指定片段後要加的標點，留空表示直接連接；不要每個短片段都機械加逗號。不同語意段落用不同 block。

畫面卡預設保留可見文字與結構。只有結構欄純屬「封面樣式、取樣時間、字體位置」等擷取註記，而且所有畫面內容已在可見文字中時，才可在卡片 block 設 `include_structure: false`；表格、箭頭、數值及補充文字必須保留。重複同幀 OCR 字幕表留在證據檔，不放主閱讀稿。已知疑點自動以〔？〕標在原句旁，不插入整段稽核說明。

`resume <run_dir>` 僅用於明確要求的重匯出，不重做 ASR／OCR；`finish-review` 已完成輸出，不再接 `resume`。`reading_map.json` 保存逐段對應，供程式驗證無遺漏／重排。不要把這些對應欄位放回主 MD。
# 本機索引補充契約

畫面索引、補充證據登記、分頁取證與省略決議的擴充見 [local-evidence.md](local-evidence.md)。原始 visuals_sha256 契約不變；新增畫面採獨立命名空間及來源雜湊驗證。省略原抽樣畫面須有模型決議與已看代表幀依據，不能將本機分類當作已核圖。
