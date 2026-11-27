# 進階功能指南

[← 回到 README](README.zh-TW.md)

本文件說明選用的 GDMS 補強、臺灣區域矩張量（CMT）試算與自動機制研究流程。
這些功能預設關閉，啟用前請先完成 [README](README.zh-TW.md#快速開始) 的基本安裝。

- [GDMS 手動登入](#gdms-手動登入)
- [臺灣區域矩張量（CMT）試算](#臺灣區域矩張量cmt試算)
- [自動機制研究試算](#自動機制研究試算)

## GDMS 手動登入

啟用自動下載時，設定 `.env`：

```dotenv
TIGHNARI_GDMS_AUTO_DOWNLOAD=true
```

Tighnari 不會自動登入 GDMS，也不保存或送出 GDMS 帳密。登入一律由人在專用的
Microsoft Edge 視窗完成，程式只在確認登入成功後把登入狀態交給背景服務，
之後以 HTTP 申請與下載資料。須安裝 Microsoft Edge，並在 Tighnari 的
Python 環境執行 `python -m pip install -e ".[gdms-edge]"`。

登入狀態失效時，下載佇列會暫停並保留，即時定位不受影響；若設定
`TIGHNARI_GDMS_ATTENTION_WEBHOOK`，會發送一次手動登入提醒。
暫停期間每四小時會檢查一次登入狀態是否恢復，但不會嘗試登入。
網路斷線、系統忙碌或下載申請被拒會自動延後重試，不會要求手動登入。
個別資料工作的錯誤不會暫停其他事件；結果不明的申請只查詢原申請，不重複送件。

需要登入時：

Windows 可直接雙擊專案根目錄的 `gdms.bat`，選擇「4」安裝 Edge 支援（首次）、
「5」啟動服務（尚未運行時）、「1」手動登入、「2」重新檢查登入並恢復下載，或「3」查看狀態。
批次檔預設尋找名為 `tighnari` 的 conda 環境，不需要先手動啟用 conda。
可用環境變數 `TIGHNARI_CONDA_ENV` 指定環境名稱或完整路徑，
`TIGHNARI_CONDA_ACTIVATE` 指定 conda 的 `activate.bat`。

若要使用指令操作：

1. 在**實際運行 Tighnari 的電腦**開啟桌面或遠端桌面，使用與背景服務相同的
   系統帳號。Windows 服務須在該 Windows 桌面操作；在 Mac 的一般瀏覽器登入
   不會把登入狀態傳給 Windows 服務。
2. 開啟終端機，切換到專案目錄並使用 Tighnari 的 Python 環境。
   首次使用先安裝上述 Edge 支援。
3. 執行 `python scripts/gdms_control.py login`。在開啟的**專用 Edge 視窗**
   輸入 GDMS 帳密，完成人工驗證並登入。預設等待五分鐘，可加 `--timeout 600` 延長。
4. 指令確認登入成功後會關閉視窗，交接登入狀態並通知背景服務恢復。
   保持服務運行，下次輪詢（預設約一分鐘）會接收登入狀態、解除可重試工作的暫停。
5. 執行 `python scripts/gdms_control.py status`，確認 `authentication.attention`
   已變成 `0`，並觀察下載工作的狀態。狀態指令不會輸出帳密或 Cookie。

Windows 主機的操作範例（PowerShell；請把路徑換成實際的專案目錄與 conda 環境）：

```powershell
cd <專案目錄>\Tighnari-SeisLOC
& <conda 環境>\python.exe -m pip install -e ".[gdms-edge]"
& <conda 環境>\python.exe scripts/gdms_control.py login
& <conda 環境>\python.exe scripts/gdms_control.py status
```

Mac 使用 `.venv/bin/python scripts/gdms_control.py login`，登入只交給同一台 Mac 的服務。
`python scripts/gdms_control.py resume` 只要求服務立即重新檢查現有登入狀態，
不會登入，也不會匯入一般瀏覽器的登入狀態。

手動登入交接檔只用一次，十分鐘後失效。Windows 使用同一系統使用者的 DPAPI 加密；
Mac/Linux 檔案權限為 `0600`，交接後會刪除。不要傳送或提交 `runtime/gdms` 中的
登入交接檔與 Edge 設定檔。若登入失敗或關閉視窗，服務不會被標示為已恢復。

## 臺灣區域矩張量（CMT）試算

區域 CMT 以獨立寬頻位移波形求解矩張量、矩心深度、延遲與 Mw，在事件詳情頁與
P 波初動結果各自呈現。背景試算及公開展示皆預設關閉。先將
[QSEIS 2006b](https://git.pyrocko.org/pyrocko/fomosto-qseis) 的
`fomosto_qseis2006b` 可執行檔放入 PATH，再於 Tighnari 的 Python 3.11 或 3.12 環境執行：

```bash
python -m pip install -e ".[cmt]"
tighnari-seisloc cmt-build-greens --workers 4
tighnari-seisloc cmt-build-greens --short-period --workers 8
tighnari-seisloc cmt-replay event.json runtime/cmt-replay --download
tighnari-seisloc cmt-benchmark cases.json runtime/cmt-greens/validation.json
```

`--short-period` 另建 `taiwan_wu2007_short` 與 `taiwan_wu2007_slow_short`：
4–304 km、深度 1–101 km（間隔 2 km）、1 Hz 取樣，供 M3.5–5 的短週期 CAP 分窗反演使用。
兩個模型在 14 核心 Mac 約各需 7 分鐘，部署主機可直接建置或複製整個目錄。
沒有這兩個目錄時，研究面板會顯示「等待短週期格林函數庫」，其他方法照常運作。
QSEIS 沒有現成套件時，可從原始碼以 gfortran 編譯：
`gfortran -O2 -ffixed-line-length-none -fno-automatic -std=legacy -o fomosto_qseis2006b src/*.f`。

批次驗證可用 `scripts/replay_cmt_suite.py`，以各案例的 Tighnari 事件檔，或僅以
`window_center_utc` 指定波形擷取時間。使用時間窗時須安裝 `.[cmt,ml]` 並準備
PhaseNet 模型，定位與初動會由波形獨立重算。輸入案例名單與校準／盲測分組須預先固定。

離線重播接受重複的 `--waveforms 檔案.mseed 檔案.xml` 和選用的
`--gdms-inbox 目錄`。每次試算保存 `result.json`，有代表解時另存
`fits.npz` 的逐站 Z/R/T 實測與合成位移。比較清單在頂層使用 `cases` 陣列，
每筆提供 `id`、`split`（`diagnostic`、`calibration` 或 `blind`）和
`result`，並可附 `first_motion`、USGS GeoJSON 的 `comcat`、
AutoBATS 標準文字匯出的 `autobats` 和 GCMT NDK 的 `gcmt`。
後兩個欄位使用含 `file`、`event_id` 的物件。檔案路徑以清單所在目錄為準；
外部產品僅在獨立反演完成後讀入。

`TIGHNARI_REGIONAL_CMT_ENABLED=true` 啟用背景試算。EarthScope 提供公開 TW
測站，另有可用的 BATS FDSN 服務時可設定 `TIGHNARI_REGIONAL_CMT_BATS_URL`。
`TIGHNARI_REGIONAL_CMT_PUBLISH=true` 需要同版格林函數目錄中的盲測合格
`validation.json` 才會顯示合格解。粗網格 `--diagnostic-grid` 只供驗證流程。
`tighnari-seisloc wphase-replay event.json GREENS OUTPUT --python GA_PYTHON`
呼叫獨立安裝的 GA W-phase。以 `--archive MSEED STATIONXML` 讀取封存長週期波形，
或以 `--fdsn-server https://service.earthscope.org` 擷取。`GREENS` 可使用 GA 格式的
SAC 目錄或 HDF5。比較清單以 `wphase` 載入 `ga-wphase.json` 與 USGS Mww 對照。
批次回放可加上 `--wphase-python` 和 `--wphase-greens`，並於個案設定 `wphase_archive`
的封存資料路徑，或在提供 `--wphase-fdsn-server` 時以 `wphase_fdsn: true` 取得波形。
[GA W-phase 的格林函數說明](https://github.com/GeoscienceAustralia/ga-wphase#greens-functions)

另可用 `scripts/replay_global_cmt.py EVENT STORE OUTPUT --download --centroid` 讀取公開 EarthScope
長週期波形，使用 Pyrocko `global_20s_shallow` 波形庫進行獨立試算。這個流程以 W-phase 時間窗及
垂直位移反演，輸出逐站省略驗證、深度可接受範圍和輸入雜湊。
此流程僅供離線研究。2022–2026 年 13 件案例的[比較摘要](research/global-cmt-taiwan-2022-2026.json)
已封存，完整回放及比較圖存於 `runtime/global-cmt-taiwan-2022-2026/`。
[Pyrocko 全球波形庫](https://gf.pyrocko.org/gfws/static/stores/global_20s_shallow/)

以封存資料比較 RMT 方法時，可在專案根目錄執行：

```bash
python scripts/compare_cmt_methods.py runtime/cmt-validation-20261122 runtime/cmt-rmt-methods
python scripts/compare_cmt_methods.py runtime/cmt-validation-20261122 runtime/cmt-rmt-methods --cohort
```

第一個指令比較站權重上限、封存波形逐分量回收、保留原測站再補方位、速度波形、
分段加權與水平矩心搜尋。
第二個指令把以四筆 RMT 案例選出的固定設定套用至既有其他事件。兩者只讀取本機已封存資料，
研究結果獨立於事件頁發布門檻。

多候選反演可在 `cmt-replay` 或 `scripts/replay_cmt_suite.py` 加上 `--ensemble`。
有至少八個合格寬頻站時，由最多 16 站的測站池按方位保留三個共同驗證站，
再用方位均衡、高訊噪比與近站等測站組合及不同速度模型反演。每個候選需通過原有品質門檻，
共同留站的 VR 中位數也須達 35%。比較圖存為 `candidates.png`，逐站分數及品質未通過原因在
`result.json`。在最高共同留站分數 10 個百分點內，Kagan 角相近的候選歸為同一組。
多個方向不同的解族若同時符合此條件，事件頁並列顯示各組代表供覆核。

`TIGHNARI_REGIONAL_CMT_ENSEMBLE=true` 可讓背景區域 CMT 使用多候選模式。此模式採用
`regional-cmt-ensemble-v1`，正式顯示仍須同方法、同格林函數版本的盲測合格檔。

`cmt-replay` 和 `scripts/replay_cmt_suite.py` 的 `--moment` 啟用 `regional-cmt-moment-v1`。
此模式以公定站儀響應還原絕對位移，在短週期、中間參考頻帶與長週期之間比較，
並比較點源、8 秒與 16 秒的單位面積三角形震源時間函數。分段時移借鑑 CAP，以距離估計
體波與表面波的轉換窗，網格搜尋兩者的相對時移。共用三個留站依固定寬頻頻帶計分，
輸出波形擬合、振幅比例、相關係數與 Mw 對測站及速度模型的敏感範圍。三分量覆蓋或方位
不足時，可取用封存檔中可用的單分量，保留儀器記錄方向並遮罩缺段。
`TIGHNARI_REGIONAL_CMT_MOMENT=true` 可啟用背景試算。ML 的 Wood–Anderson 倍率維持 6800。
基準比較依目錄、規模類型與規模級距列出帶正負號的偏差、絕對誤差中位數與第 90 百分位。
比對 RMT 時，個案中的 `rmt` 欄位可記錄 `event_id`、`source_url`、`mw`、`depth_km` 和
`plane`（走向、傾角、滑移角）。若有 RMT 反演位置，可加上 `centroid` 的 `lat`、`lon`。
[CAP 分段波形反演](https://doi.org/10.1029/2005JB003775)

`cmt-replay` 或 `scripts/replay_cmt_suite.py` 加上 `--centroid`，用相同的測站、儀器方向、
頻帶與時間窗，比較固定震央和水平矩心搜尋。網格最遠達東西及南北各 36 公里，每個節點
同時搜尋深度及時間，逐站省略時重新選取節點。三個共同留站只用於評分。輸出保留初動震央、
矩心座標及留出站各分量的擬合、相關係數與振幅比。結果也標出格網邊界或位置約束不足，供回放驗證。
[RMT 網格反演](https://doi.org/10.1093/gji/ggt371) · [水平矩心搜尋](https://doi.org/10.1029/2011JB008501)

事件總覽圖與近景圖每張最多顯示一顆海灘球。已通過發布驗證且收斂為單一機制族的區域 CMT
優先作為代表解，其次採用通過門檻的 P 波初動解。若兩者皆通過且雙力偶 Kagan 角大於
35° 或無法比較，總覽與近景顯示機制歧異。若區域 CMT 有多個可信機制族，可在事件詳情
展開比較各族代表。兩種方法的海灘球與品質資訊各自保留在事件詳情中。

初動機制試算增加逐站省略檢查，固定同一批候選角度，檢查少一個獨立站時機制是否旋轉超過
35°。GDMS 重算會以最新校正檔取代同一段，避免重複累計。定位未變且原機制已通過時，若新解
未通過或兩解的 Kagan 角超過 35°，GDMS 與本機初動背景工作會保留原解並記錄本次重算狀態。

## 自動機制研究試算

設定 `TIGHNARI_ADAPTIVE_CMT_ENABLED=true` 啟用即時模式的研究排程，登入 `/hoshi` 選取事件
即可查看自動更新的候選解、海灘球與留站資訊。區域工作使用 `.[cmt]` 與
`TIGHNARI_REGIONAL_CMT_STORE`，並在 GDMS 資料到齊後試算保留 IRIS 基線的補站解。
全球工作依規模或 Mwp 先查詢遠震測站，完整時窗約在震後 45 分鐘可用。
可用 `TIGHNARI_ADAPTIVE_GLOBAL_MIN_MAGNITUDE` 和 `TIGHNARI_ADAPTIVE_GLOBAL_STORE`
設定排程門檻與全球格林函數庫。研究結果供後台覆核，公開主圖沿用原有發布驗證。

研究流程會對初動＋S/P 及初動＋SH/P 顯示留站與留方位區結果。區域基線和補站解均
未通過時，若可保留共同驗證站，會另外以體波與表面波分窗加權試算。
設定 `TIGHNARI_ADAPTIVE_WPHASE_PYTHON` 和 `TIGHNARI_ADAPTIVE_WPHASE_GREENS`
可為符合全球排程門檻的事件加跑 GA W-phase。GA 需要其自身格式的格林函數庫。

短週期 CAP 先用依規模選定的頻帶；若沒有候選通過品質檢查，再改用下一個較長週期的頻帶重試一次，
結果以 `regional_cap:large` 等編號另列。2026-11-24 以 54 筆封存事件回放，通過數由 39 增為 44，
Kagan 中位數維持約 15°，沒有新增超過 60° 的解。定位深度未約束（例如外海事件固定在地表）時，
初動極性檢查與 CAP 極性約束解改用波形解的矩心深度計算射線，並另外列出「以矩心深度重算射線」的
初動研究解。CAP 候選會在研究目錄輸出逐站波形擬合圖，可從 `/hoshi` 開啟。啟用波形反演時，GDMS
寬頻 HH 改申請震前 200 秒至震後 620 秒，排程也延後到該時窗資料可取得之後。初動解另附 A–D 品質
等級（參考 HASH），只作說明；公開顯示仍沿用原本的發布門檻。

啟用 `TIGHNARI_ADAPTIVE_CMT_ENABLED` 時，公開事件詳細頁也會在「震源參數與品質」下方顯示「自動機制試算」，
內容與 `/hoshi` 相同但為唯讀：研究代表解與通過檢查的候選列在最前，其餘方法收在可展開區塊，並可開啟逐站
波形擬合圖。只顯示最新修訂；錯誤事件與僅保留摘要的事件不顯示。原「P 波初動與 GDMS 震後補強」方塊改名為
「機制解計算」，移到定位圖下方。公開 API 為 `/api/earthquakes/{id}/mechanisms`。

規模（ML 或 Mwp）6 以上的事件，研究代表解優先採用全球長週期 CMT（遠震 W 相時窗）。目前只有一個全球
格林函數庫，因此用同一批資料在較低頻帶（0.0015–0.005 Hz）另外反演一次，代替第二個速度模型。通過條件為：
至少 20 站、留站 VR 至少 80%、逐站省略的機制變化 P90 不超過 30°，以及兩個頻帶的機制差不超過 30°。
另外，近最佳深度中若有張量無法定義雙力偶，只在全球路徑中略過，不再視為機制隨深度改變。
以 2022–2026 年 13 筆臺灣 M6 以上事件回放（`runtime/global-cmt-taiwan-2022-2026/qualify.py`），
2024-04-03 Mw 7.4 與 2025-12-27 Mw 6.6 通過，與 USGS Mww 相差 13° 和 4°，其餘未通過；與 USGS 差超過 30°
的解全部未通過。區域解與全球解相差超過 35° 時標示為多解。

若定位服務與 Pyrocko 需要不同的 NumPy 版本，可在獨立 Python 環境安裝
`.[cmt]`，並將 `TIGHNARI_ADAPTIVE_CMT_PYTHON` 設為該環境的 Python 路徑。
Windows 範例為 `runtime/adaptive-cmt-python/Scripts/python.exe`。
