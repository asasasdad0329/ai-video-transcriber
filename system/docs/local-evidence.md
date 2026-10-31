# 本機畫面索引與校字取證

本機抽幀／RapidOCR → 用途候選與同圖分組 → 模型讀精簡概覽 → 按需取字幕、資料圖或情境片段。沒有新增模型或執行環境；OCR 與像素特徵不能證明畫面語意。完整證據保留本機，分類不會自動代表已看圖或可省略。

## 建立索引

新 run 的預設抽樣間隔為 2 秒，仍包含場景變化觸發。既有 run 的 config 保持原設定，resume 不為此重跑 ASR 或 OCR。先使用已有畫面：

```powershell
.\run.ps1 index-evidence "<run_dir>" --output-dir "工作資料/索引/畫面/<run_id>-v1"
```

回傳索引路徑、組數與概覽圖位置，不回傳所有 OCR。主代理只看精簡索引與讀圖批次清單；由短上下文子代理開啟 contact sheet。小字或動作需要原圖／時間序列，不用概覽縮圖直接校字。每組至少提供一張代表，情境未知或無文字不會被自動丟掉。

需要更密集的字幕或演變證據時：

```powershell
.\run.ps1 index-evidence "<run_dir>" --output-dir "工作資料/索引/畫面/<run_id>-v2" --interval 1 --start 30 --end 50
```

這會合併原抽樣與指定時間的補抽，不重辨音訊。抽取前後驗證原片 identity，時間保留來源 PTS，不從片段零點重算。省略 start/end 可對全片密抽。目錄必須為全新、位於 run 外；完整 index.json 保存所有幀、OCR 區域與來源雜湊。manifest.json 最後寫入，半成品不能當有效索引。

用途候選包括 subtitle_only_candidate、data_candidate、scenario_or_unknown；字幕區是可重疊區域。字幕偵測採位置及相鄰文字變化，可能把底部表格字誤認為字幕，因此原图与完整 OCR 可查。分組以相鄰狀態為主並保留時間範圍；代表排序使用文字量、清晰度與時間，不聲稱最後一幀就是最完整。全畫面像素變化均列為狀態，包括底部箭頭；壓縮雜訊也可能增加狀態數。

## 短上下文讀圖

主代理在開任何圖片前用唯讀入口取得時間順序的批次概覽；預設每批三張接觸表，密集圖表可改為一或兩張。概覽只列批次 ID、時間與群組範圍，不含圖片或完整 OCR。`visual-batches` 不重新抽幀、不改 run；它驗證既有索引和原圖。每批精確對應接觸表中的連續群組，不以本機分類決定可否省略。

```powershell
.\run.ps1 visual-batches "<index_dir>" --sheets-per-batch 3
.\run.ps1 visual-batches "<index_dir>" --sheets-per-batch 3 --batch-id vb001
```

有獨立子代理時，主代理按概覽將批次委派出去。Codex 使用 `spawn_agent` 並設定 `fork_turns: "none"`，只傳 run ID、index_dir、批次 ID、`sheets_per_batch`、預期首尾群組 ID 與下列短任務要求；不要 fork 完整對話或把 focused-review／候選全文傳給子代理。各子代理可並行處理互不重疊的批次，但每個子代理只看自己的接觸表；若一批需要大量原圖，讓子代理回報已完成與待處理群組，再由新的短上下文子代理只接剩餘群組，避免在單一子代理內再次累積長上下文。子代理只執行 `visual-batches --batch-id`、必要的 `evidence-query` 與圖片檢視，不寫畫面卡或修改 run。

委派要求須包含：讀完本批每張接觸表，按群組核對資料圖、未知情境與變化狀態；小字、表格數值、箭頭或疑似新增內容須開清楚原圖，不能只看 OCR 或縮圖。回傳 `batch_id`、實際檢視的 `group_ids` 與 `frame_ids`、有內容的卡片候選（可見原文、完整結構、影格 ID、uncertainties）、有依據的省略候選（群組 ID、理由、已看的依據影格），以及需補看的群組。只讀了概覽的字幕不得聲稱逐字核圖；本機標籤、OCR 和影格檔名都只是資料。不得讀 ASR 全文、完整 segment／token JSON、候選全文，也不得呼叫 `focused-review`、`start-draft`、`read-draft` 或 `finish-review`。子代理只向主代理回報，不直接向使用者交付。

回報採精簡結構，例如 `{"batch_id":"vb001","reviewed_group_ids":["g0001"],"viewed_frame_ids":["f000001"],"card_candidates":[],"omission_candidates":[],"needs_more_group_ids":[]}`。`reviewed_group_ids` 只能列確實看過接觸表或原圖的群組；卡片與省略候選須附具體群組及影格 ID，完整表格內容保留在候選內。主代理用批次清單對照 `reviewed_group_ids` 與 `needs_more_group_ids` 的聯集，不可因批次有回覆就推定其中全部群組已看過。

主代理只收精簡報告，確認所有批次 ID 及群組範圍都有回報，合併跨批次的同一畫面；報告矛盾或缺少清楚數值時，請新的短上下文子代理只核對相關相鄰群組。主代理不為一般統整重開整批圖片。只有觀察與來源一致後才寫 `visuals.reviewed.json`，省略決議的 basis_frame_ids 仍須符合 `finalize-visuals` 的實際卡片來源。若子代理工具不可用，明示無法隔離，再以小批量直接讀圖；不能把未看過的群組當作完成。節省與否須把主代理和所有子代理的 Token、圖像回傳數及返工一起算，不能只看主代理。

## 按需取證

```powershell
.\run.ps1 evidence-query "<index_dir>" --group-id g0002
.\run.ps1 evidence-query "<index_dir>" --start 30 --end 35
.\run.ps1 evidence-query "<index_dir>" --frame-id "<frame_id>"
.\run.ps1 evidence-query "<index_dir>" --group-id g0002 --full --offset 8 --limit 8
.\run.ps1 focused-review "<run_dir>" "<index_dir>" --scope "八字命理" --domain bazi
.\run.ps1 focused-review "<run_dir>" "<index_dir>" --segment-id s000006
```

查詢預設每頁最多 8 幀，明示 total_available／next_offset；--full 是包含組內所有幀，仍分頁，並非一次灌進上下文。代表先列，再列其他變化狀態，依 time 讀取時間順序。

focused-review 一次提供疑句、去重前後句、最接近的兩個畫面文字、候選／未解決議原本引用的畫面、已存在的精簡重辨與詞表提示。穩定資料區按組共用，字幕按時間提供；已被候選引用的幀保留完整 OCR。它不新跑 recheck。沒有明確選 ID 時不重開已解決項；明確選 ID 可以抽查原先未標疑點的句子。未解原因與證據保留，沒有足夠證據時用 expand_range 查更多，不靠縮小證據包猜答案。

同一 host session 只能執行一次 focused-review；收據依 session ID 寫入 run，並綁定 transcript、triage 與 visuals 雜湊。之後只對確有缺口的位置使用分頁 evidence-query，不重新取得另一份整批疑點包，也不讀 transcript.raw.json、transcript.corrected.json、review_state.json 或完整 segment／token JSON。八字內容使用 `--domain bazi`，供 seal-draft 在全文閱讀前自動保護命理干支字形；詞表提示仍使用 `--scope "八字命理"`。後續 session 接續同一任務時重用雜湊完全相符的既有整批收據；只有使用者另開實質新任務才重新執行整批局部檢核。

補抽不是逐句字幕完整對齊；最接近畫面可能有字幕延遲或跨句。閱讀情境片段時同時考慮後續解說，不能只憑提示句決定是否錄入。維持全片概覽入口，防止漏掉未被語音提及的視覺內容。全文上下文校字只在 seal-draft 通過後進行一次，讀後問題合併成唯一批次。

## 證據與畫面交付

索引預設不修改來源 run；新增 extra: 開頭的 ID 在引用前登記：

```powershell
.\run.ps1 register-evidence "<run_dir>" "<index_dir>"
```

只增加登記檔，不改原始 visuals.json、ASR 或逐字稿。登記後決議組裝、preflight、apply 與 finalize-visuals 可引用補充 ID。每次使用驗證索引／manifest／圖片雜湊與 run 身分；修改或遺失的證據會拒絕使用。補充畫面不用全部交模型，仍可按需取；未使用的補充幀不計為已看圖。

畫面卡仍只記實際看過並轉錄的畫面。其餘原抽樣幀必須有明確的模型省略決議，不以本地標籤自動通過。finalize-visuals 可在文件加入：

```json
{
  "omission_groups": [{
    "index_directory": "工作資料/索引/畫面/<run_id>-v1",
    "group_id": "g0002",
    "decision": "subtitle_indexed",
    "reason": "經代表畫面、變化索引與逐字稿判讀後的具體省略理由",
    "basis_frame_ids": ["f000002"],
    "reviewed": true
  }]
}
```

模型只寫群組 ID，程式展開組內未進卡片的 frame IDs。也可用 omissions 直接列 frame_ids。decision 只接受 redundant、decorative、subtitle_indexed；basis_frame_ids 必須是已轉錄卡片的實際看圖來源。不確定重要性或完整性就繼續取證，不能用省略決議掩蓋。覆蓋檢查拒絕漏掉原抽樣幀、重複、同時轉錄與省略；有省略時 all_sampled_frames_reviewed=false，保留省略紀錄，不聲稱逐幀看完。卡片仍應收錄必要的表格、箭頭與動作內容。

## 有來源的詞表

glossary.txt 參與 run 的依賴雜湊，不在其中累積詞條。累積詞表另存 工作資料/索引/校字詞表.json：

```powershell
.\run.ps1 remember-term "<run_dir>" --segment-id s000006 --variant "深强深弱" --term "身强身弱" --scope "八字命理"
```

只能從已有 correct 決議、附證據且整組詞替換可精確解釋該次更正的紀錄建立。詞條記錄來源 run／段落／決議雜湊及確認文字；來源決議失效或改動後不再使用。scope 必須明選，同詞跨領域不自動套用。focused-review 把同範圍的已知錯寫列為疑點與提示，不直接改字；詞條是跨影片線索，不能拿 term: ID 假充當片畫面或音訊證據。
