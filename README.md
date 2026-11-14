# 📸 Pixel 4 無限 Google 相簿中繼系統 (iPhone & Mac ➔ Pixel 4)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-green.svg)](https://www.python.org/)
[![GitHub Pages](https://img.shields.io/badge/Web%20Config%20Builder-GitHub%20Pages-38bdf8.svg)](https://zhijaysun0915.github.io/pixel4-gphotos-relay/)

透過 **Google Pixel 4**（或 Pixel 1–5）獨有的 **Google 相簿無限備份權限**，將 **iPhone 手機** 與 **Mac 電腦 / 外接硬碟** 的照片與影片，透過 Mac 上的 Python 中繼引擎與 USB ADB **分批雙緩衝** 推送到 Pixel 4 自動上傳，解決手機本機容量有限（64GB / 128GB）與大量照片手動搬運的痛點。

> 🌐 **線上互動網頁（GitHub Pages）**：[https://zhijaysun0915.github.io/pixel4-gphotos-relay/](https://zhijaysun0915.github.io/pixel4-gphotos-relay/)  
> 可直接在瀏覽器上設定你的帳號標籤、Mac / iPhone 來源資料夾與日期/檔案範圍，一鍵下載專屬 `config.json`。

---

## ✨ 核心特色

- **零外部套件依賴**：純 Python 3.9+ 標準函式庫撰寫（無須 `pip install` 複雜環境），開箱即用。
- **內建本機 Web 控制台 (`http://localhost:8765`)**：圖形化檢視 Pixel 4 電量/剩餘空間/連線狀態、掃描預覽待備份檔案、調整篩選條件、一鍵啟動/安全停止與即時日誌監控。
- **帳號資訊彈性（多帳號不混淆）**：`google_account_label` 欄位會寫入本地 SQLite 同步紀錄（`relay_history.db`）。若你有家人或多個 Google 帳號輪流在 Pixel 4 備份，切換帳號標籤即可各自追蹤進度，不會誤跳過。
- **檔案範圍彈性（精準篩選）**：
  - 支援多組來源資料夾（Mac 本機、iPhone 匯入資料夾、外接 SSD / NAS 掛載點）。
  - 支援 **起訖日期 (`date_from` / `date_to`)**、**媒體類型（照片 / 影片 / RAW）**、**自訂副檔名**、**排除關鍵字（如 `.DS_Store`, `@eaDir`, `Thumbnails`）** 與 **檔案大小上下限**。
- **雙緩衝防爆空間保護（Double-Buffered Staging）**：
  1. 先將單批容量上限（預設 `15 GB`）推送至 Pixel 的 `/sdcard/Download/PixelStaging`（內含 `.nomedia`，避免 Google 相簿讀取到傳輸一半的殘檔）。
  2. 確認 `/sdcard/DCIM/Camera` 先前批次已上傳完成後，才瞬間 `mv` 移入 `Camera` 並觸發媒體掃描。
  3. 透過 ADB `uiautomator` 自動讀取 Google 相簿畫面文字（同時支援 **繁中 / 簡中 / 英文**），確認顯示「備份完成 / Backup complete」後自動清空 `Camera` 騰出空間，接力下一批。
- **預設安全唯讀**：`delete_source_after_push` 預設為 `false`，不會刪除 Mac 或 iPhone 匯入資料夾的原始檔案。

---

## 🏗️ 系統架構圖

```text
┌─────────────────────┐      ┌──────────────────────┐
│   📱 iPhone 手機     │      │   💻 Mac 本機 / 外接碟  │
│ AirDrop / iCloud /  │      │ ~/Pictures / Volumes │
│ Image Capture / App │      │                      │
└──────────┬──────────┘      └──────────┬───────────┘
           │                            │
           └──────────────┬─────────────┘
                          ▼
        ┌───────────────────────────────────┐
        │  🖥️ Mac 中繼引擎 (app.py)          │
        │  • 帳號標籤 & 日期/副檔名範圍過濾     │
        │  • SQLite 去重 (relay_history.db)  │
        │  • Web 控制台 (:8765)              │
        └─────────────────┬─────────────────┘
                          │ USB-C / ADB Push
                          ▼
        ┌───────────────────────────────────┐
        │  📱 Google Pixel 4 (USB 偵錯)      │
        │  1. /sdcard/Download/PixelStaging │
        │     (.nomedia 隔離安全緩衝區)       │
        │  2. /sdcard/DCIM/Camera           │
        │     (觸發 Google 相簿 0-Byte 上傳) │
        │  3. UIAutomator 偵測完成後自動清空   │
        └─────────────────┬─────────────────┘
                          │ Wi-Fi 自動上傳
                          ▼
        ┌───────────────────────────────────┐
        │  ☁️ Google Photos 無限空間備份      │
        └───────────────────────────────────┘
```

---

## 🛠️ 事前準備

### 1. Pixel 4 手機端設定
> 💡 **支援機型提醒**：Pixel 2 / 3 / 4 / 5 支援「**節省空間 (Storage saver)**」無限容量免費備份；Pixel 1 代支援「**原始畫質**」無限備份。請確認 Google 相簿 App 內的備份畫質已設定正確。

1. **開啟開發人員選項**：設定 ➔ 關於手機 ➔ 連續點擊「版本號碼」7 次。
2. **開啟 USB 偵錯**：設定 ➔ 系統 ➔ 開發人員選項 ➔ 開啟 **USB 偵錯 (USB debugging)**。
3. **建議開啟「充電時保持喚醒」**：在開發人員選項中開啟 **不鎖定螢幕（充電時螢幕不會休眠）**，避免螢幕關閉後背景上傳降速（程式啟動後也會自動下 `stay_on_while_plugged_in 3` 輔助）。
4. **Google 相簿設定**：
   - 登入欲備份的 Google 帳號。
   - 確認「備份」已開啟，且僅勾選 `Camera` 資料夾。
   - **開啟 Google 相簿 App 停留在主畫面**（讓 `uiautomator` 能順利讀取進度文字）。

### 2. Mac 電腦端安裝 ADB
若 Mac 尚未安裝 `adb`，使用 Homebrew 一行安裝即可：
```bash
brew install android-platform-tools
```
插上 Pixel 4 的 USB 傳輸線，手機端跳出「允許 USB 偵錯嗎？」時勾選「**一律允許**」並確認。執行以下指令應可看到裝置：
```bash
adb devices -l
```

### 3. iPhone 照片如何匯入 Mac 中繼？
你可以依照習慣選擇任一方式，將 iPhone 照片集中到 `config.json` 設定的資料夾（例如 `~/Pictures/iPhone_Import`）：

- **方法 A（USB 有線最穩，免 iCloud 付費）**：  
  iPhone 插上 Mac ➔ 開啟 macOS 內建「**影像擷取 (Image Capture)**」➔ 選擇匯入目標資料夾為 `~/Pictures/iPhone_Import` ➔ 一鍵全選匯入。
- **方法 B（無線自動傳檔 App）**：  
  在 iPhone 安裝 **PhotoSync** 或 **LocalSend**，設定透過區域網路自動將新拍照片傳送到 Mac 的 `~/Pictures/iPhone_Import`。
- **方法 C（iCloud / macOS 照片 App）**：  
  若已在 Mac 開啟 iCloud 照片，可在 Mac「照片」App 中選取欲備份的年份/相簿 ➔ `檔案` ➔ `輸出未修改的原始檔` 到 `~/Pictures/ToGooglePhotos`。

---

## 🚀 快速開始

### 步驟 1：下載專案
```bash
git clone https://github.com/zhijaysun0915/pixel4-gphotos-relay.git
cd pixel4-gphotos-relay
```

### 步驟 2：建立個人設定檔 `config.json`
你可以直接從 [線上設定產生器](https://zhijaysun0915.github.io/pixel4-gphotos-relay/) 下載 `config.json`，或是複製範本：
```bash
cp config.example.json config.json
```
*(註：`config.json` 與 `*.db`、`*.log` 已加入 `.gitignore`，不會被意外上傳到公開 GitHub。)*

### 步驟 3：啟動本機 Web 控制台
```bash
python3 app.py
```
瀏覽器會自動開啟 **`http://localhost:8765`**（若需從區網其他電腦操作，可加上 `--host 0.0.0.0`）。

在網頁介面中你可以：
1. 確認右上角或頂部卡片顯示 **Pixel 4 已連線** 與剩餘容量。
2. 填入 **Google 相簿帳號標籤** 與 **來源資料夾路徑**。
3. 設定 **檔案範圍（起始/結束日期、照片/影片/RAW、排除關鍵字）** 與 **單批上限（GB）**。
4. 點擊 **「🔍 掃描預覽待備份檔案」** 確認數量與容量無誤。
5. 點擊 **「▶ 開始中繼備份」** 即可全自動運行！

---

## 💻 CLI 命令列模式（進階 / 自動化排程用）

若你想配合 `cron` 或背景腳本執行，也可以直接使用 `relay_engine.py`：

```bash
# 僅掃描預覽符合範圍的待備份檔案（Dry-run，不會推送）
python3 relay_engine.py --config config.json --scan-only

# 直接執行中繼備份
python3 relay_engine.py --config config.json
```

---

## ⚙️ `config.json` 設定欄位說明

| 區塊 | 欄位 | 說明 | 預設值 |
| :--- | :--- | :--- | :--- |
| `account` | `google_account_label` | 標記目前 Pixel 4 登入的 Google 帳號（作為 SQLite 去重主鍵之一） | `"your.account@gmail.com"` |
| `account` | `device_serial` | 指定 ADB 裝置序號；填 `"auto"` 會自動優先選擇已連線的 Pixel | `"auto"` |
| `sources` | `name` / `type` / `path` / `enabled` | 支援多個來源資料夾（`mac_folder`、`iphone_import`、`external_drive`） | 3 組範本 |
| `scope` | `date_from` / `date_to` | 依檔案修改時間篩選（格式 `YYYY-MM-DD`，留空代表不限） | `""` |
| `scope` | `include_photos` / `include_videos` / `include_raw` | 決定是否包含照片、影片與相機 RAW 檔 | `true` / `true` / `false` |
| `scope` | `custom_extensions` | 額外自訂副檔名陣列，例如 `[".avif"]` | `[]` |
| `scope` | `exclude_patterns` | 路徑或檔名包含這些字詞時略過 | `[".DS_Store", "@eaDir", ...]` |
| `relay` | `batch_size_gb` | 單批推送到 Pixel 的容量上限（建議 64GB Pixel 4 設 `10~15` GB） | `15.0` |
| `relay` | `min_free_gb` | Pixel 剩餘空間低於此值時暫停推送並等待備份清空 | `12.0` |
| `relay` | `delete_source_after_push` | 推送成功後是否刪除 Mac 來源檔（**安全起見建議維持 `false`**） | `false` |
| `relay` | `auto_clean_camera_after_backup` | 偵測 Google 相簿備份完成後是否自動清空 Pixel `DCIM/Camera` | `true` |

---

## ❓ 常見問題 (FAQ)

1. **為什麼偵測不到 Pixel 4 (`No authorized ADB device detected`)？**
   - 請確認使用的是**支援資料傳輸的 USB 線**（部分純充電線無法傳輸資料）。
   - 手機端解鎖後查看是否跳出「允許 USB 偵錯」授權視窗。
   - 若裝在非預設路徑，可在 `config.json` 的 `relay.adb_path` 填入 `which adb` 的完整路徑。
2. **Pixel 4 會不會因為照片太多而爆空間當機？**
   - 本工具採用「預載區 + 相機區」雙緩衝與 `min_free_gb` 安全水位檢查（預設保留至少 12 GB），只要前一輪還沒上傳清空，就不會推入下一批。
3. **如果我中途按停止，下次會重複推送嗎？**
   - 不會。每個成功推送的檔案都會立即寫入 `relay_history.db`，下次掃描會自動略過同帳號下已推送的檔案。

---

## 📄 License

[MIT License](LICENSE)
