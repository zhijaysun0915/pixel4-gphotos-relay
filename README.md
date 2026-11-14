# 📸 Google Drive & 相簿數據管家 | Pixel 4 無限 Google 相簿中繼系統 (Windows + Mac + iPhone + Google Drive)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-green.svg)](https://www.python.org/)
[![Platform: Windows | macOS](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-0284c7.svg)](#%EF%B8%8F-%E4%BA%8B%E5%89%8D%E6%BA%96%E5%82%99)
[![GitHub Pages](https://img.shields.io/badge/Web%20Config%20Builder-GitHub%20Pages-38bdf8.svg)](https://zhijaysun0915.github.io/pixel4-gphotos-relay/)

這是一套支援 **Windows 10/11** 與 **macOS**、專為 **Google Pixel 4**（相容 Pixel 1–5）打造的 **Google Drive / Google 相簿無限空間中繼與磁碟分析網頁控制台**。  
整合 **iPhone 手機**、**Windows / Mac 電腦與外接硬碟** 以及 **Google Drive (`rclone`)** 多來源資料，透過本機網頁介面（預設 `http://localhost:8989`）與雙緩衝 ADB 引擎，將照片與影片自動分批推送至 Pixel 4 完成 **0-Byte 無限空間備份**。

> 🌐 **GitHub Pages 公開網頁**：
> - **相片搬遷監控看板（主控台）**：https://zhijaysun0915.github.io/pixel4-gphotos-relay/
> - **Disk Analyzer Pro（磁碟分析與重複清理）**：https://zhijaysun0915.github.io/pixel4-gphotos-relay/disk_analyzer.html
> - **Google 相簿管理專頁**：https://zhijaysun0915.github.io/pixel4-gphotos-relay/google_photos_manager.html

---

## ✨ 核心功能一覽

1. **Google Drive & Google 相簿中繼控制台 (`static/index.html` & `static/google_photos_manager.html`)**
   - 支援 **Google Drive (`rclone`)**、**Mac 本機資料夾**、**iPhone 匯入資料夾**、**外接硬碟 / NAS** 多來源統一管理。
   - 內建 **Pixel 4 / Pixel 5 ADB 即時狀態監控**（連線序號、電量、可用空間、相機佇列）。
   - 支援 **帳號資訊彈性設定**（主帳號、副帳號 / Partner Sharing、rclone remote 名稱、NAS 主機皆可透過 `config.json` 或 API 自訂）。
2. **雙緩衝防爆空間中繼引擎 (`universal_relay_pipeline.py`)**
   - 先將安全批次容量（預設 `15~20 GB`）推入 `/sdcard/Download/PixelStaging`（含 `.nomedia` 隔離，避免 Google 相簿讀到傳輸一半的檔案）。
   - 透過 ADB `uiautomator` 偵測 Google 相簿畫面文字（同時支援**繁中 / 簡中 / 英文**：「備份完成 / Backup complete」），確認上傳完畢後自動清空 `/sdcard/DCIM/Camera` 再接力下一批。
   - **檔案範圍彈性過濾**：支援 `--date-from`、`--date-to`（日期區間）、`--no-photos` / `--no-videos` / `--include-raw`（媒體類型）、`--exclude`（排除關鍵字）、`--batch-gb` 與 `--delete-source`（預設安全保留來源）。
3. **Disk Analyzer Pro 磁碟分析與重複檔案清理 ([static/disk_analyzer.html](static/disk_analyzer.html) / [線上預覽](https://zhijaysun0915.github.io/pixel4-gphotos-relay/disk_analyzer.html))**
   - 由側邊欄「💽 開啟 Disk Analyzer Pro ↗」直接進入（本機同時監聽 `http://localhost:8989/disk_analyzer.html` 與 `http://localhost:8998`）。
   - SQLite 增量掃描快取，快速分析資料夾佔用空間、Top 100 大檔與跨資料夾 MD5 重複檔案比對（支援 Windows `explorer /select,` 與 macOS `Finder` 定位檔案）。

---

## 🏗️ 系統架構

```text
┌─────────────────────┐   ┌──────────────────────┐   ┌───────────────────────┐
│   📱 iPhone 手機     │   │  💻 Mac / 外接硬碟    │   │ ☁️ Google Drive       │
│ Image Capture / App │   │ ~/Pictures / Volumes │   │ rclone mount / sync   │
└──────────┬──────────┘   └──────────┬───────────┘   └───────────┬───────────┘
           │                         │                           │
           └─────────────────────────┼───────────────────────────┘
                                     ▼
        ┌────────────────────────────────────────────────────────────┐
        │  🖥️ 本機 Web 伺服器 (server.py, Port 8989 / 8998)           │
        │  • 讀取 config.json (帳號標籤 & 檔案範圍彈性設定)              │
        │  • Disk Analyzer Pro / Google Drive / Pixel 中繼任務控制    │
        └────────────────────────────┬───────────────────────────────┘
                                     │ USB-C / ADB Push (universal_relay_pipeline.py)
                                     ▼
        ┌────────────────────────────────────────────────────────────┐
        │  📱 Google Pixel 4 (USB 偵錯 + 節省空間無限備份)             │
        │  1. /sdcard/Download/PixelStaging (.nomedia 安全緩衝區)     │
        │  2. /sdcard/DCIM/Camera (觸發 Google 相簿 0-Byte 無限上傳)   │
        │  3. UIAutomator 偵測備份完成後自動清空，循環接力下一批          │
        └────────────────────────────────────────────────────────────┘
```

---

## 🛠️ 事前準備

### 1. Pixel 4 手機端設定
1. **開啟開發人員選項**：設定 ➔ 關於手機 ➔ 連續點擊「版本號碼」7 次。
2. **開啟 USB 偵錯**：設定 ➔ 系統 ➔ 開發人員選項 ➔ 開啟 **USB 偵錯**，並建議開啟 **不鎖定螢幕（充電時不休眠）**。
3. **Google 相簿設定**：登入目標 Google 帳號 ➔ 備份畫質設為 **節省空間 (Storage saver)**（Pixel 2–5 享無限容量；Pixel 1 享原始畫質無限容量），並將 Google 相簿 App 保持開啟。

### 2. 電腦端安裝工具（Windows & Mac）

#### 🪟 Windows 10 / 11
1. **Python 3.9+**：從 [python.org](https://www.python.org/downloads/) 或 Microsoft Store 安裝（安裝時記得勾選 `Add Python to PATH`）。
2. **ADB (Android Platform Tools)**：在 PowerShell / CMD 執行：
   ```powershell
   winget install Google.PlatformTools
   ```
   *(或手動下載 [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) 解壓到 `C:\platform-tools`，程式會自動偵測 `C:\platform-tools\adb.exe`。)*
3. **rclone（選用，Google Drive 雲端同步）**：
   ```powershell
   winget install Rclone.Rclone
   rclone config
   ```

#### 🍎 macOS
- **Python 3.9+**：macOS 內建或使用 Homebrew 安裝（本專案核心伺服器使用 Python 標準函式庫，無需額外 `pip install`）。
- **ADB**：
  ```bash
  brew install android-platform-tools
  adb devices -l
  ```
- **rclone（選用，若需同步/掛載 Google Drive）**：
  ```bash
  brew install rclone
  rclone config
  ```

### 3. iPhone 照片如何匯入電腦中繼？
將 iPhone 照片集中到設定的匯入資料夾（Windows 例如 `C:\Users\你的名字\Pictures\iPhone_Import`，Mac 例如 `~/Pictures/iPhone_Import`）：
- **Windows USB 匯入**：iPhone 插上電腦 ➔ 開啟檔案總管「Apple iPhone ➔ Internal Storage ➔ DCIM」直接複製，或使用 Windows 內建「相片 App」匯入至 `C:\Users\你的名字\Pictures\iPhone_Import`。
- **Mac USB 匯入（最穩）**：iPhone 連接 Mac ➔ 開啟 macOS「影像擷取 (Image Capture)」➔ 匯入到 `~/Pictures/iPhone_Import`。
- **雙平台無線自動傳檔**：iPhone 安裝 **PhotoSync** 或 **LocalSend**，自動傳輸至 Windows/Mac 的匯入資料夾。
- **iCloud / Mac 照片圖庫**：Windows 可裝 **iCloud for Windows** 同步照片資料夾；Mac 可從「照片」App 選取照片 ➔ `檔案` ➔ `輸出未修改的原始檔`。

---

## 🚀 快速開始

### 1. 下載專案並建立 `config.json`
```bash
git clone https://github.com/zhijaysun0915/pixel4-gphotos-relay.git
cd pixel4-gphotos-relay
cp config.example.json config.json
```
你也可以先到 [GitHub Pages 線上設定產生器](https://zhijaysun0915.github.io/pixel4-gphotos-relay/) 填好帳號與檔案範圍後直接下載 `config.json` 放入資料夾。

> 🔒 **隱私保護**：`config.json`、`disk_cache.db` 與 `*.log` 均已列入 `.gitignore`，不會被提交到公開 Git 倉庫。

### 2. 啟動本機 Web 控制台
```bash
python3 server.py
```
啟動後同時監聽 **`http://localhost:8989`** 與 **`http://localhost:8998`**，打開瀏覽器即可使用完整網頁介面：
- `http://localhost:8989/` — 整合數據管家首頁（Google 相簿 / Google Drive / Pixel 中繼）
- `http://localhost:8989/google_photos_manager.html` — Google 相簿與合作夥伴共用 / 洗白策略頁面
- `http://localhost:8989/disk_analyzer.html` — Disk Analyzer Pro 磁碟空間與重複檔分析頁面

### 3. CLI 命令列直接啟動中繼（含彈性檔案範圍參數）
```bash
python3 universal_relay_pipeline.py \
  --source ~/Pictures/iPhone_Import \
  --device auto \
  --batch-gb 15 \
  --min-free-gb 12 \
  --date-from 2024-01-01 \
  --date-to 2026-12-31 \
  --exclude ".DS_Store,@eaDir,Thumbnails"
```

---

## ⚙️ `config.json` 帳號與檔案範圍設定說明

| 區塊 | 欄位 | 說明 |
| :--- | :--- | :--- |
| `account` | `primary_google_account` / `secondary_google_account` | 目標備份與來源 Google 帳號標籤 |
| `account` | `device_serial` | `"auto"` 自動偵測 Pixel，或指定特定 ADB 序號 |
| `account` | `rclone_primary_remote` / `rclone_secondary_remote` | 對應本機 `~/.config/rclone/rclone.conf` 的遠端名稱 |
| `account` | `nas_host` / `nas_share_path` | 選填，若有 Synology NAS 掛載可填入 |
| `sources` | `name` / `type` / `path` / `enabled` | 支援多組 Mac、iPhone 匯入、Google Drive、外接硬碟路徑 |
| `scope` | `date_from` / `date_to` | 依檔案修改日期篩選 (`YYYY-MM-DD`，留空不限) |
| `scope` | `include_photos` / `include_videos` / `include_raw` | 過濾照片、影片、相機 RAW 檔 |
| `scope` | `exclude_patterns` | 排除指定關鍵字或資料夾 |
| `scope` | `batch_size_gb` / `min_free_gb` / `delete_source_after_push` | 單批容量、Pixel 安全保留空間、是否刪除來源（預設 `false`） |

---

## 📄 License

[MIT License](LICENSE)
