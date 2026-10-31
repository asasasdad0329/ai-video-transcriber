# 工作區維護

根目錄維持入口文件、system、.runtime 與工作資料。Git 管理 system 與根目錄入口；.gitignore 以允許清單排除其他根目錄內容。新增共用入口檔時須明確加入允許清單。不要將開發報告或真實使用者測試資料放進 system。README.md（繁體中文）與 README.en.md（英文）內容對應，任何一份修改都在同一次變更中同步另一份。

## 路由與程式邊界

AGENTS.md 是唯一共用入口，CLAUDE.md 只以 `@AGENTS.md` 匯入它，讓 Claude Code 與 Codex 讀到同一份規則。影片轉錄讀 system/skills/video-transcribe/SKILL.md，資料卡讀 system/skills/datacard/SKILL.md，講義讀 system/skills/handout/SKILL.md。每條 workflow 自己管理內容與文風規範，共用入口只處理路由、隔離、來源安全及成果位置。資料卡與講義使用 `.runtime/envs/publish`，與轉錄 venv 分開，共用專案私人 Python。


轉錄程式在 system/src/video_workflow，跨流程路徑定義在 system/src/workspace_paths.py。資料卡與講義只讀轉錄 run，不修改它：system/src/derived_common 從主稿與 reading_map.json 取得帶編號的段落，system/src/datacard 與 system/src/handout 各自驗證與組裝，system/src/domains 放可替換的領域模組。講義主題放 system/config/handout-themes，publish 環境與 Mermaid 資產鎖在 publish-requirements.lock.txt 與 publish-lock.json。system/bootstrap 包含命令分派（launcher.py）、環境建立（setup.py）、資產下載與檢查程式；system/config 包含設定及版本鎖。FFmpeg 與 whisper.cpp 需要的 cuBLAS 鎖在 tools-lock.json，setup 只解出其中列出的檔案並逐一核對雜湊。無法放進專案的系統元件由 doctor 的 system 欄位回報，安裝指引在 README.md「系統元件與安裝指引」。資料不因副檔名為 JSON／MD 就屬於系統。

## 入口與殼層

run.ps1 與 run.cmd 只做兩件事：第一次使用時用 Windows 內建工具（curl、PowerShell、tar）下載 uv 並由 uv 安裝鎖定的 Python，然後把參數原封不動交給 system/bootstrap/launcher.py。命令分派、環境變數、setup 與下載全部寫在 Python。理由是 Windows PowerShell 5.1 以系統代碼頁讀沒有 BOM 的指令碼、傳給外部程式的參數會掉雙引號、導向的 stderr 會變成錯誤；Python 以 UTF-8 讀原始碼、用參數清單呼叫子程序，三者都不受影響。因此 .ps1 與 .cmd 檔只能用英文字元，不內嵌程式碼，外部程式成功與否只看結束碼；system/tests/test_entrypoints.py 用 Windows PowerShell 5.1 實際檢查這些規則與參數傳遞。

下載依序嘗試 curl.exe、Windows PowerShell 與 Python。前兩者走 Windows 自己的憑證驗證，會即時補抓缺少的根憑證與中繼憑證；Python 只讀憑證庫的副本。檔案是否正確一律由鎖定的 SHA-256 判定。uv 預設信任自帶的 Mozilla 根憑證，失敗時改用 Windows 憑證庫重試一次，以涵蓋公司網路的代理憑證。

## 任務與輸出

每次轉錄的 state.json、task.json、音訊、ASR、OCR、截圖、visual_review.json、review_state.json、patch、reading_layout.json、focused_review_tasks.json 與 final_review_tasks.json 均放在工作資料/任務/轉錄/<run_id>。task.json 是精簡摘要，state.json 為 CLI 狀態；兩份 review task ledger 以 host session ID 區分任務，記錄整批局部檢核、最終候選封存、唯一全文閱讀、唯一問題批次及正式提交。原始識別資料及更正決議保留作本機證據，不提供模型全文閱讀。

中間匯出放在 run/exports/transcripts 和 run/exports/visuals；主要可獨立閱讀的 MD 放在工作資料/輸出/逐字稿。chapters/finalize 命令是 Markdown 筆記匯出介面，輸出放 run/exports/legacy-notes，不是資料卡或講義流程。

資料卡任務在工作資料/任務/資料卡/<run_id>，講義任務在工作資料/任務/講義/<run_id>，各有 task.json（記錄所依據主稿的 sha256）、輸入包與 check.json。主稿更新後 status 回報 stale，重新 prepare 再 build。成品分別放工作資料/輸出/資料卡（.md）與工作資料/輸出/講義（.docx），檔名與主稿相同。跨多支影片的系列資料卡任務在工作資料/任務/資料卡/series-<hash>，task.json 記錄各部的 run 與主稿 sha256，成品以系列名為檔名；索引另闢系列資料卡一節，並在各部影片列標出所屬系列。使用者確認過的範例放工作資料/參考樣本/標準範例，Agent 撰寫前先讀。

用 catalog 查每支影片已完成哪些成果（system/src/catalog，只用標準函式庫，兩個 venv 與專案基礎 Python 都能執行）。資料庫與表格版都在 工作資料/索引；run.ps1 在 process、resume、apply、finish-review、finalize-visuals、datacard、handout 之後自動重掃，工作索引.md 不要手改。list-runs 仍列轉錄任務；status 檢查完成狀態。正常轉錄交付採 [一次成稿流程](final-review.md)，finish-review 已完成輸出；resume 只供明確要求的重匯出。

索引以狀態取代刪除：原檔或成品消失標 missing，檔案放回去會回到 present 並留下兩次事件；成品內容改變時，舊雜湊標 superseded、新版標 current，版本數顯示在索引表。事件表記錄每次新增、變更、消失，用來回答「這支影片以前做過什麼、後來被移除了什麼」。資料庫可以砍掉重建，重建後只會少掉已移除項目的歷史，因此它屬於工作資料而非系統。不要直接改原始證據或複核文件來修路徑，否則會破壞雜湊與校字版本。

## 路徑解析

run 內的證據路徑不改寫；CLI 透過 workspace_paths.resolve 解析到目前位置。要開啟 run 內記錄的證據路徑時，可使用：

```powershell
.\run.ps1 resolve-path "<run 內記錄的路徑>"
```

解析只對專案內完整路徑元件生效，不改寫外部絕對路徑。搬移整個工作區後，相對證據路徑仍可解析；專案外的來源不會自動搬家。bootstrap-lock.json 參與 run 的依賴雜湊，其中的資產路徑由下載程式解析到 .runtime；不要因名稱整理而改動它或更新套件版本。

Git 對 bootstrap-lock.json、requirements.lock.txt、glossary.txt 保留原始位元組，不自動轉換換行，以免 checkout 後破壞既有 run 的依賴雜湊。

## 歸檔與清理

工作資料/歸檔/開發紀錄 保存 verify_build.py 的驗收結果。這些是紀錄，不是新的操作指令。

可重用的測試程式留在 system/tests，測試在 .runtime/tmp 產生暫存。失敗試跑或過期暫存可歸檔；清理前確認它們未被有效 run 引用。原始 ASR／OCR、校字證據及原檔不作一般垃圾清除。下載與安裝快取供離線重建，預設保留。

setup 重建轉錄 venv 時，原環境備份到 .runtime/backups；不作日常環境使用。僅有 Git 不會備份私人工作資料，應另外備份工作資料及需要離線保留的安裝快取。

## 驗證

日常程式修改執行 `./run.ps1 test`，它依序跑轉錄測試與 system/tests/publish 內的資料卡／講義測試（publish venv 不存在時略過並警告）。環境檢查用 doctor 與 publish-doctor。既有成品驗收：

```powershell
& '.\.runtime\envs\transcribe\Scripts\python.exe' -X utf8 -I -B system/bootstrap/verify_build.py <run_id>
```

verify_build.py 驗證口述順序、視覺卡片涵蓋與檔案雜湊，結果寫入 工作資料/歸檔/開發紀錄。若已有 external-dependencies-before.json，也檢查追蹤的外部檔案；沒有基準時不宣稱已驗證外部環境。

需要驗證新環境的實際 GPU／OCR 才執行 integration_smoke.py，明確指定來源與片段長度；不要為一般匯出修改重跑整片 ASR。
