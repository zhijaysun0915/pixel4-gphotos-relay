#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Universal Multi-Source to Pixel Relay Engine
Supports:
- Any Source: External HDD/SSD, SD Card, Synology NAS (/Volumes/home/Photos, GoogleDrive_Relay), iPhone staging, local folder
- Any Target: Pixel 4, Pixel 4 XL, Pixel 5 (specified by serial or auto-detected)
- Strategy: Move & Free Space (Delete source on push) OR Copy Only (Keep source intact)
- Safe Staging: Double-buffered via /sdcard/Download/PixelStaging with .nomedia
- Zero-Quota Unlimited Upload: Moves to /sdcard/DCIM/Camera for Google Photos native 0-byte backup
"""

import os
import sys
import time
import argparse
import subprocess
import datetime
import xml.etree.ElementTree as ET

ADB = os.path.expanduser("~/.local/bin/adb")
PIXEL_STAGING = "/sdcard/Download/PixelStaging"
PIXEL_CAMERA = "/sdcard/DCIM/Camera"

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp",
    ".tif", ".tiff"
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".wmv", ".3gp", ".mpg", ".mpeg", ".mts",
    ".m2ts", ".m4v", ".flv", ".mkv", ".asf", ".vob", ".m2v"
}
RAW_EXTS = {
    ".dng", ".cr2", ".cr3", ".arw", ".nef", ".orf", ".rw2", ".raf"
}
VALID_EXTS = PHOTO_EXTS | VIDEO_EXTS | RAW_EXTS

JUNK_EXTS = {
    ".json", ".ds_store", ".thm", ".ctg", ".info", ".moff", ".modd",
    ".db", ".ini", ".aae"
}

def parse_args():
    parser = argparse.ArgumentParser(description="Universal Relay Engine")
    parser.add_argument("--source", required=True, help="Source directory path (Disk / NAS / Folder)")
    parser.add_argument("--device", default="auto", help="Target ADB device serial (or 'auto')")
    parser.add_argument("--batch-gb", type=float, default=20.0, help="Batch size in GB (e.g. 15, 20, 30)")
    parser.add_argument("--delete-source", action="store_true", help="Delete source files after successful push")
    parser.add_argument("--log-file", default="", help="Path to write log output")
    parser.add_argument("--min-free-gb", type=float, default=8.0, help="Minimum free GB to keep on phone")
    parser.add_argument("--date-from", default="", help="Filter files modified on/after YYYY-MM-DD")
    parser.add_argument("--date-to", default="", help="Filter files modified on/before YYYY-MM-DD")
    parser.add_argument("--no-photos", action="store_true", help="Exclude photo files")
    parser.add_argument("--no-videos", action="store_true", help="Exclude video files")
    parser.add_argument("--include-raw", action="store_true", help="Include camera RAW files")
    parser.add_argument("--exclude", default="", help="Comma-separated exclude keywords/paths")
    return parser.parse_args()

class UniversalRelay:
    def __init__(self, args):
        self.source = os.path.abspath(os.path.expanduser(args.source))
        self.device = args.device
        self.batch_bytes = int(args.batch_gb * 1024 * 1024 * 1024)
        self.delete_source = args.delete_source
        self.min_free_gb = args.min_free_gb
        self.log_file = args.log_file or os.path.expanduser(f"~/relay_{int(time.time())}.log")
        self.device_serial = None
        self.device_model = "Pixel"
        self.allowed_exts = set()
        if not args.no_photos:
            self.allowed_exts |= PHOTO_EXTS
        if not args.no_videos:
            self.allowed_exts |= VIDEO_EXTS
        if args.include_raw:
            self.allowed_exts |= RAW_EXTS
        self.exclude_patterns = [p.strip().lower() for p in args.exclude.split(",") if p.strip()]
        self.date_from_ts = datetime.datetime.strptime(args.date_from, "%Y-%m-%d").timestamp() if args.date_from else None
        self.date_to_ts = (datetime.datetime.strptime(args.date_to, "%Y-%m-%d") + datetime.timedelta(days=1)).timestamp() if args.date_to else None

    def log(self, msg):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {msg}"
        print(line, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def run_adb(self, cmd_args, timeout=60):
        base = [ADB]
        if self.device_serial:
            base.extend(["-s", self.device_serial])
        return subprocess.run(base + cmd_args, capture_output=True, text=True, timeout=timeout)

    def adb_shell(self, shell_cmd, timeout=60):
        try:
            res = self.run_adb(["shell", shell_cmd], timeout=timeout)
            return res.stdout.strip()
        except Exception as e:
            self.log(f"⚠️ ADB shell 指令逾時或異常 ({shell_cmd[:40]}...): {e}")
            return ""

    def detect_device(self):
        res = subprocess.run([ADB, "devices", "-l"], capture_output=True, text=True)
        lines = [l.strip() for l in res.stdout.splitlines()[1:] if l.strip()]
        if not lines:
            self.log("❌ 未偵測到任何已連接並授權的 ADB Android 設備！請檢查 USB 連線與 USB 偵錯開關。")
            sys.exit(1)

        target = None
        if self.device != "auto":
            for line in lines:
                if self.device in line:
                    target = line
                    break
            if not target:
                self.log(f"❌ 指定設備 {self.device} 未在連線清單中！")
                sys.exit(1)
        else:
            # Prefer Pixel devices
            for line in lines:
                if "Pixel" in line or "redfin" in line or "flame" in line or "coral" in line or "sunfish" in line or "bramble" in line:
                    target = line
                    break
            if not target:
                target = lines[0]

        parts = target.split()
        self.device_serial = parts[0]
        model = "Pixel 設備"
        for p in parts:
            if p.startswith("model:"):
                model = p.split("model:")[1].replace("_", " ")
        self.device_model = model
        self.log(f"📱 成功連接目標設備: {self.device_model} (序號: {self.device_serial})")

    def get_phone_free_gb(self):
        out = self.adb_shell("df -k /sdcard | tail -n 1")
        parts = out.split()
        if len(parts) >= 4 and parts[3].isdigit():
            return int(parts[3]) / (1024 * 1024)
        return 0.0

    def get_staging_bytes(self):
        out = self.adb_shell(f"du -sk {PIXEL_STAGING} 2>/dev/null | cut -f1")
        if out.isdigit():
            return int(out) * 1024
        return 0

    def get_camera_files_count(self):
        out = self.adb_shell(f"ls -1 {PIXEL_CAMERA} 2>/dev/null | wc -l")
        return int(out) if out.isdigit() else 0

    def get_backup_status(self):
        # 1. Check camera dir count first
        cam_count = self.get_camera_files_count()
        if cam_count == 0:
            return ("complete", 0)

        # 2. UIAutomator inspect Google Photos status
        self.adb_shell(f"uiautomator dump /sdcard/ui_status.xml >/dev/null 2>&1", timeout=15)
        tmp_path = f"/tmp/ui_status_{self.device_serial}.xml"
        self.run_adb(["pull", "/sdcard/ui_status.xml", tmp_path], timeout=10)
        if not os.path.exists(tmp_path):
            return ("backing_up", cam_count)

        try:
            tree = ET.parse(tmp_path)
            for elem in tree.iter():
                t = elem.attrib.get("text", "")
                d = elem.attrib.get("content-desc", "")
                combined = f"{t} {d}"
                m = re.search(r"剩餘\s*(\d+)\s*項|(\d+)\s*items?\s*left", combined, re.IGNORECASE)
                if m:
                    rem_val = next(g for g in m.groups() if g is not None)
                    return ("backing_up", int(rem_val))
                if any(k in combined for k in ("備份完成", "隨時備份", "備份已完成", "備份已關閉", "Backup complete", "Backing up is off")):
                    return ("complete", 0)
        except Exception:
            pass

        return ("backing_up", cam_count)

    def init_environment(self):
        self.adb_shell(f"mkdir -p {PIXEL_STAGING}")
        self.adb_shell(f"mkdir -p {PIXEL_CAMERA}")
        self.adb_shell(f"touch {PIXEL_STAGING}/.nomedia")
        # Keep screen awake while charging
        self.adb_shell("settings put global stay_on_while_plugged_in 3")
        self.adb_shell("settings put system screen_brightness 10")
        free_gb = self.get_phone_free_gb()
        self.log(f"💾 {self.device_model} 可用儲存空間: {free_gb:.1f} GB (保留緩衝: {self.min_free_gb} GB)")

    def clean_source_junk(self):
        self.log(f"🧹 正在快速清理來源路徑垃圾檔 (.json, .thm, .aae, .DS_Store)...")
        cmd = (
            f"find '{self.source}' -type f \\( "
            f"-iname '*.json' -o -iname '.DS_Store' -o -iname 'Thumbs.db*' -o "
            f"-iname '*.aae' -o -iname '*.thm' -o -iname '*.ctg' -o "
            f"-iname '*.info' -o -iname '*.moff' -o -iname '*.modd' -o -name '.*' "
            f"\\) -delete 2>/dev/null"
        )
        subprocess.run(cmd, shell=True)
        self.log(f"✨ 垃圾檔清理完畢！")

    def run(self):
        self.log(f"==================================================")
        self.log(f"🚀 啟動通用照片中繼遷移任務")
        self.log(f"📁 來源目錄: {self.source}")
        self.log(f"🔄 遷移策略: {'【邊推邊刪 (釋放來源空間)】' if self.delete_source else '【僅複製 (保留來源原始檔)】'}")
        self.log(f"📦 批次大小: {self.batch_bytes / (1024**3):.1f} GB")
        self.log(f"📜 日誌路徑: {self.log_file}")
        self.log(f"==================================================")

        self.detect_device()
        self.init_environment()
        self.clean_source_junk()

        batch_count = 1
        total_pushed_files = 0
        total_pushed_bytes = 0

        while True:
            # Check phone free space
            free_gb = self.get_phone_free_gb()
            if free_gb < self.min_free_gb:
                self.log(f"⏳ {self.device_model} 可用空間剩餘 {free_gb:.1f} GB (< {self.min_free_gb} GB)，等待 Google 相簿備份清空...")
                status, rem = self.get_backup_status()
                if status == "complete":
                    self.log(f"🧹 相機目錄已備份完成，清空以騰出空間...")
                    self.adb_shell(f"rm -rf {PIXEL_CAMERA}/*")
                time.sleep(30)
                continue

            # Gather next batch files
            current_batch_files = []
            current_batch_bytes = 0

            for root, dirs, files in os.walk(self.source):
                dirs[:] = [d for d in dirs if not d.startswith(".") and not any(pat in d.lower() for pat in self.exclude_patterns)]
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in self.allowed_exts and not f.startswith("."):
                        full_p = os.path.join(root, f)
                        if self.exclude_patterns and any(pat in full_p.lower() for pat in self.exclude_patterns):
                            continue
                        try:
                            st = os.stat(full_p)
                            sz = st.st_size
                            mtime = st.st_mtime
                        except OSError:
                            continue
                        if self.date_from_ts is not None and mtime < self.date_from_ts:
                            continue
                        if self.date_to_ts is not None and mtime >= self.date_to_ts:
                            continue
                        current_batch_files.append((full_p, f, sz))
                        current_batch_bytes += sz
                        if current_batch_bytes >= self.batch_bytes:
                            break
                if current_batch_bytes >= self.batch_bytes:
                    break

            if not current_batch_files:
                self.log(f"🎉 來源目錄中已無待備份照片或影片！任務圓滿完成！")
                break

            self.log(f"📦 [第 {batch_count} 批] 收集 {len(current_batch_files)} 個檔案 ({current_batch_bytes / (1024**3):.2f} GB)，開始直推至 {self.device_model} 預載區...")

            # Push files into staging
            pushed_in_batch = 0
            bytes_in_batch = 0

            for full_p, fname, sz in current_batch_files:
                # Direct adb push
                res = self.run_adb(["push", full_p, f"{PIXEL_STAGING}/{fname}"], timeout=120)
                if res.returncode == 0:
                    pushed_in_batch += 1
                    bytes_in_batch += sz
                    total_pushed_files += 1
                    total_pushed_bytes += sz

                    if self.delete_source:
                        try:
                            os.remove(full_p)
                        except Exception as e:
                            self.log(f"⚠️ 無法刪除來源檔案 {fname}: {e}")

                    if pushed_in_batch % 50 == 0 or pushed_in_batch == len(current_batch_files):
                        self.log(f"📊 [推入進度] 第 {batch_count} 批: {pushed_in_batch}/{len(current_batch_files)} 檔 ({bytes_in_batch / (1024**3):.2f} GB)")
                else:
                    self.log(f"❌ 推送失敗: {fname} (保留來源檔)")

            # Wait for any previous camera backup to finish before moving into Camera folder
            self.log(f"⏳ 檢查 {self.device_model} 相機目錄狀態...")
            while True:
                status, rem = self.get_backup_status()
                if status == "complete":
                    self.log(f"🧹 相機目錄已備份完成，清空目錄準備接收新批次...")
                    self.adb_shell(f"rm -rf {PIXEL_CAMERA}/*")
                    break
                else:
                    self.log(f"⏳ 上一批次仍在備份中 (剩餘約 {rem} 項)，等待 15 秒...")
                    time.sleep(15)

            # Move from staging into Camera folder
            self.log(f"🚀 將預載區的 {pushed_in_batch} 個檔案移入相機目錄 ({PIXEL_CAMERA}) 並觸發 Google 相簿無限備份...")
            self.adb_shell(f"mv {PIXEL_STAGING}/* {PIXEL_CAMERA}/ 2>/dev/null || true")
            self.adb_shell(f"rm -f {PIXEL_CAMERA}/.nomedia")
            self.adb_shell(f"touch {PIXEL_STAGING}/.nomedia")

            # Broadcast media scanner
            self.adb_shell(
                f'for f in {PIXEL_CAMERA}/*; do am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "file://$f" >/dev/null 2>&1; done &'
            )

            self.log(f"✅ 第 {batch_count} 批已就位！Google 相簿正在背景享受 0-Byte 無限空間上傳！")
            batch_count += 1
            time.sleep(10)

        self.log(f"🏆 全案總結: 累計遷移 {total_pushed_files} 個檔案，共 {total_pushed_bytes / (1024**3):.2f} GB！")

if __name__ == "__main__":
    args = parse_args()
    engine = UniversalRelay(args)
    engine.run()
