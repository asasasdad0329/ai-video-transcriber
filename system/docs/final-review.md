# 一次成稿與局部修正

正常路徑以一個 host session 為一個任務，只讓模型讀一次已封存的最終候選全文。原始 ASR 全文、token、全部片段 JSON／版本對照都留在本機，由程式處理；不得用 shell 或檔案讀取把它們送進模型上下文。局部校字有明確語境支持就直接決定更正，不必固定重辨；模型不為每次改兩字規劃新工具序列。

## 累積局部決議

每個任務先用一次 focused-review 完成整批局部取證，再依 [本機證據流程](local-evidence.md) 讓短上下文子代理分批看圖；主代理只接收帶來源 ID 的觀察並統整畫面卡。將所有判斷保存成簡短的 edits 文件，尚不產生正式 Patch、不 apply。命盤、表格、圖表凡來源可讀，就先補齊全部行列；真的不可讀才保留 uncertainty。先完成自動疑點、畫面卡、排版、正規化保護詞與全部局部決議，再建立 staging 草稿：

```powershell
.\run.ps1 start-draft "<run_dir>" --output-dir "工作資料/候選稿/<run_id>" --changes "<edits.json>"
```

edits.json 範例：

```json
{"edits":[
  {"segment_id":"s000006","new_text":"身强身弱理论就是典型代表","reason":"同段術語與上下文一致"},
  {"segment_id":"s000044","decision":"needs_review","reason":"兩個解讀尚不能排除"},
  {"segment_id":"s000051","decision":"needs_review","new_text":"身強的人用印〔？〕比較好","reason":"身強已確定，印字仍有兩種聽法"}
]}
```

更正預設 decision=correct；沒給 evidence_ids 時明示使用 text:<segment_id> 作上下文依據，沒有假稱聽音。有畫面／重辨依據時可直接附 evidence_ids。keep 與 needs_review 可省略 new_text。needs_review 不給 new_text 時保留整段辨識原文，〔？〕標在段尾。只有部分字詞不確定時做部分更正：new_text 改好有把握的字，並在仍照辨識保留的字詞後面加〔？〕；這段照樣計為未解。來源原文、版本、合法證據 ID 與長覆蓋清單由程式管理。先前未解項不會因這次未提及便自動清除。

第一次建立草稿時，目錄須在正式 run 外。final_review_tasks.json 以 `CODEX_SESSION_ID`／`CODEX_THREAD_ID`（沒有 session 環境變數的 host 必須用 `--task-id`）記錄這個任務所綁定的唯一草稿；host 已提供 session ID 時，`--task-id` 不得改成另一個值。同一任務再次 start-draft 只回傳原草稿。若另一個 session 使用相同輸出目錄，CLI 自動加任務雜湊後綴，因此它能從同一 run 重新開始自己的後續流程。

來源基礎在全文閱讀前更新時，可在同一任務替換尚未讀過的 staging 草稿；封存後不得再以一般小修改動候選。讀過全文後，同一任務不得重建或再取全文。新 session 可承接既有閱讀 checkpoint，修正機械錯誤或完成發布，但不因修正次數再次取得同一候選全文。一般 status 會列出目前任務的封存、閱讀及提交狀態。視覺複核仍有 uncertainty 時，start-draft 預設停止；只有來源確實不可讀才在 start-draft 或 seal-draft 加 `--accept-visual-uncertainties`，接受數量會寫入封存收據。

draft.json 保存完整本機狀態，不提供給模型閱讀。草稿目錄不持久化可閱讀的 candidate.md，避免繞過一次性 read-draft；candidate-map.json 也只保存 block 與來源 ID，不含正文。封存前的內容變更用固定的 edit-draft 入口；唯一一批讀後修正只經 finish-review 的 changes 檔進入，兩者都會保留來源對應。

## 固定小修入口

```powershell
.\run.ps1 edit-draft "<draft_dir>" --find "水土之像" --replace "水土之象"
.\run.ps1 edit-draft "<draft_dir>" --segment-id s000492 --find "面粉" --replace "麵粉"
.\run.ps1 edit-draft "<draft_dir>" --after s000031 --ending ""
.\run.ps1 edit-draft "<draft_dir>" --changes "<local-edits.json>"
```

find/replace 必須唯一命中，繁／簡顯示可定位；同詞多處只回傳最多八個局部候選，不讀全文。跨片段刪除重複以明確的兩三段 new_text 處理，避免刪掉講者有意重複。after/ending 控制片段後的標點；空字串去除，例如「為自己。打工」移除 s000031 的句點。

可批次提交：

```json
{
  "edits": [
    {"segment_id":"s000416","new_text":"比如"},
    {"segment_id":"s000417","new_text":"写作"}
  ],
  "punctuation": {"s000416":"", "s000417":"，"},
  "split_before": ["s000420"],
  "join_before": [],
  "card_before": {"card-chart-01": "s000420"},
  "section_titles": {"原小節名":"新小節名"},
  "protected_terms": ["丑"]
}
```

section_titles 的鍵是目前的小節名（必須唯一），同名小節用 `#N` 指第 N 個小節；同一批的改名先找齊目標再一起套用，可以互換名稱。值為 null 時把該小節併入前一節：標題拿掉，內容留在原處；要把被小節標題隔開的段落接起來，先併節再 join_before。

圖卡不會切斷句子。講者常在句子講到一半時換投影片；每次建立草稿與每批變更後，程式把落在句子中間的圖卡，連同它開頭的小節標題，移到該句結束處，被切開的段落也接回同一段，移動結果列在變更紀錄的 `moved_after_sentence_end`。所以只要把句讀標對，圖卡位置會自己跟上，不要為了放圖卡而加句號。每批變更全部有效才保存；片段修改不能偷換未知 ID 或使用不存在的證據。protected_terms、標點、分段與文字都在 staging 草稿累積，不為任何小修另建 planning 或工具鏈。多次局部編輯最後合併成同一原文至最終文字的單筆 Patch 項，不建立中間正式版本。`edit-draft` 只供封存前使用；封存後會拒絕逐項修補。

## 封存最終候選

```powershell
.\run.ps1 seal-draft "<draft_dir>"
```

`seal-draft` 是全文閱讀前的硬門檻。它確認整批 focused-review 已完成或既有 run 已有完整決議、所有本機疑點都有決定、畫面卡已定稿、可讀的畫面不確定項已消除、排版恰好涵蓋全部口述與圖卡、圖卡沒有插進未完成句子、正規化設定與 protected_terms 已鎖定。命理欄位或多組干支會自動啟用 bazi domain，保護干支及獨立表格中的地支字形；整批 focused-review 仍可明示 `--domain bazi`，`--scope` 只用於校字詞表。所有檢查結果、來源雜湊、候選雜湊與 revision 寫入 seal receipt。

封存成功後，候選進入 `final_candidate_ready`，一般 `edit-draft`、換 layout 或重建候選都被拒絕。若門檻回報缺漏，回到 staging 一次補齊所有項目，再封存；此時尚未消耗全文閱讀。

## 全文只讀一次

```powershell
.\run.ps1 read-draft "<draft_dir>"
```

只有 seal receipt 與目前候選雜湊完全一致時才能呼叫。它回傳已套用全部前置決議、繁體轉換與畫面卡的最終候選，以及段落起訖 ID。這次同時標記剩餘的上下文、銜接、重複、段落、標點、用字與排版問題。閱讀收據綁定目前任務；同一任務換草稿目錄也不能再取得全文。已讀候選可由後續 session 當 checkpoint 接續，但修正或換版不會解鎖第二次全文。

讀完先把全文剩餘問題全部記在同一份 final-changes.json，不逐項呼叫 edit-draft。需確認 ID 時，`draft-excerpt <draft_dir> --segment-id <id>` 在整個任務內累計最多取十二個不同段落，不提供原始全文；反覆呼叫也不能重建第二份全文。它只能協助填寫同一批問題，不形成下一輪複核。

## 一次正式提交

final-changes.json 同時保存全文閱讀找到的問題與處理方式；沒有剩餘問題時也要明示空清單：

```json
{
  "review_issues": [
    {
      "id": "issue-001",
      "kind": "normalization",
      "summary": "地支丑被一般字形轉換",
      "resolution": "加入 protected_terms 並保留命理字形",
      "segment_ids": ["s000492"],
      "card_ids": []
    }
  ],
  "edits": [
    {"segment_id": "s000492", "new_text": "丑運"}
  ],
  "protected_terms": ["丑"]
}
```

```powershell
.\run.ps1 finish-review "<draft_dir>" --reviewed --revision 3 --changes "<final-changes.json>"
```

revision 使用 read-draft 回傳的封存 revision；--reviewed 是代理對「本任務確實讀過該封存候選全文，或正沿用已有閱讀 checkpoint」的明確聲明，不是使用者追加批准，也不是工具自動推定已閱讀。`review_issues` 每項必須記錄種類、摘要、處理方式與受影響的 segment／card ID；可為空清單。其餘 edits、punctuation、split_before、join_before、card_before、section_titles 與 protected_terms 是唯一一批讀後修正；card_before 把指定圖卡移到目標 segment 之前，值為 null 時移到全文末尾；圖卡若是小節的第一塊，小節標題跟著移動，中間的段落改歸前一節。

`kind` 只接受下列六種；其他值（例如 asr、format、unresolved）會讓整批被拒絕：

- `text`：口述用字，含辨識錯字、同音誤字與重複；讀後仍無法確定的字也歸此類，處理方式寫明以 needs_review 保留〔？〕。
- `punctuation`：句讀與標點，含句尾標點、全形與半形。
- `paragraph`：分段、併段與段落銜接。
- `layout`：圖卡位置、小節標題等版面安排。
- `normalization`：繁體字形、台灣用語，以及用 protected_terms 保留的字形。
- `visual`：畫面卡文字或結構的誤讀。

畫面卡在全文閱讀時才發現的誤字也放在同一批次：`card_replacements` 以卡片 ID 對應 `[{"find":"天干地只","replace":"天干地支"}]`。每條必須唯一命中該卡的顯示文字（比對時 `,?!` 的全形半形與台／臺視為相同）；replace 照寫入的樣子呈現，不再經繁體轉換或標點正規化，因此英文句中的半形逗號等也能直接改正。替換後顯示文字不變的規則會被拒絕並指出卡片 ID。來源畫面和原始卡片不覆寫，修正保存於排版及正式問題批次，沿用封存閱讀收據，不解鎖第二次全文閱讀。

工具先在記憶體中原子式套用整批問題；任何欄位、ID 或證據錯誤都不改草稿。有效後在一次呼叫中完成：合併一份正式 Patch → 一次完整 preflight → 一次保存整批修正與排版 → 一次輸出 → 程式比對輸出與最終內容一致。任務 ledger 在進入 preflight 前就記錄本次正式提交；同一 session 不再重做。所有初始疑點已在 seal-draft 前有明確決議，不會靠全文聲明自動補 keep。回傳只含交付位置、狀態、修正數與未解 ID。

成功就交付，不再 apply、resume、通讀成品、重建 context-bundle、讀 ASR 全文、dump catalog --show 歷史或做另一輪全文版本綁定。若正式 preflight 失敗，錯誤與斷點留在 run；新的 session 可從該斷點重跑後續流程，同一 session 不反覆預檢。本機完整性檢查不是額外 LLM 校字輪次。

發布中斷時只重試同一 finish-review，沿用已通過的 Patch 與交易資料，不重做預檢、不增加稽核筆數。其他處理入口在待恢復交易存在時拒絕改動 run。成功後再次提交只回傳既有收據。已發布草稿不可繼續自行拋光；後續只有使用者指示或新實質證據才另開修正任務。

## 相容性與範圍

既有 apply、preflight、assemble-review、review-bundle、context-bundle 只保留給舊任務相容與本機除錯；正常製作不得逐一串接，也不得藉相容命令讀出原始全文或完整片段 JSON。偵測到 Codex／Claude session 時，CLI 直接拒絕 review-bundle、context-bundle、review-plan-bundle、所有 decision-bundle，以及完整重辨輸出；局部段落只能走任務累計十二段上限的 draft-excerpt。context_review 的長 ID 清單只留在本機稽核紀錄；模型不讀、不抄，也不把每次改版視為全文重讀理由。

機械比對不證明校字語意正確；本機寫完整 Markdown 也不代表模型重新讀全文。沒有以限制修正次數掩蓋已知錯誤，不強迫把不確定內容判成正確。
