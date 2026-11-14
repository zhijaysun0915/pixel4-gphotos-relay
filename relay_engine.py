#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixel 4 Unlimited Google Photos Relay Engine
Supports Mac folders, iPhone import folders, and external drives via USB ADB.
"""

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".wmv", ".3gp", ".mpg", ".mpeg", ".mts",
    ".m2ts", ".m4v", ".flv", ".mkv", ".asf", ".vob", ".m2v"
}
RAW_EXTS = {
    ".dng", ".cr2", ".cr3", ".arw", ".nef", ".orf", ".rw2", ".raf", ".pef", ".srw"
}
JUNK_EXTS = {
    ".json", ".ds_store", ".thm", ".ctg", ".info", ".moff", ".modd", ".db", ".ini", ".aae"
}

PIXEL_CODENAMES = {
    "flame": "Pixel 4",
    "coral": "Pixel 4 XL",
    "sunfish": "Pixel 4a",
    "bramble": "Pixel 4a (5G)",
    "redfin": "Pixel 5",
    "blueline": "Pixel 3",
    "crosshatch": "Pixel 3 XL",
    "sargo": "Pixel 3a",
    "bonito": "Pixel 3a XL",
    "walleye": "Pixel 2",
    "taimen": "Pixel 2 XL",
    "sailfish": "Pixel",
    "marlin": "Pixel XL",
}

COMPLETE_KEYWORDS = (
    "備份完成",
    "隨時備份",
    "備份已完成",
    "備份已關閉",
    "备份完成",
    "备份已完成",
    "Backup complete",
    "Backing up is off",
)

REMAINING_PATTERNS = (
    r"剩餘\s*(\d+)\s*項",
    r"剩余\s*(\d+)\s*项",
    r"(\d+)\s*items?\s*left",
    r"Backing up\s*(\d+)",
    r"正在備份\s*(\d+)",
)


def find_adb(configured_path: str = "auto") -> str:
    """Locate the adb binary across common macOS/Linux paths."""
    if configured_path and configured_path != "auto":
        expanded = os.path.expanduser(configured_path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded

    candidates = [
        shutil.which("adb"),
        os.path.expanduser("~/.local/bin/adb"),
        "/opt/homebrew/bin/adb",
        "/usr/local/bin/adb",
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "adb"


def load_config(config_path: str) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


class SyncDatabase:
    """SQLite tracker so files already backed up to a given Google account are skipped."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS synced_files (
                    account_label TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (account_label, source_name, rel_path)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def is_synced(self, account_label: str, source_name: str, rel_path: str, size: int) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                "SELECT size FROM synced_files WHERE account_label=? AND source_name=? AND rel_path=?",
                (account_label, source_name, rel_path),
            )
            row = cur.fetchone()
            return row is not None and row[0] == size
        finally:
            conn.close()

    def mark_synced(
        self,
        account_label: str,
        source_name: str,
        rel_path: str,
        filename: str,
        size: int,
        mtime: float,
    ) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            now_iso = datetime.datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT OR REPLACE INTO synced_files
                (account_label, source_name, rel_path, filename, size, mtime, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (account_label, source_name, rel_path, filename, size, mtime, now_iso),
            )
            conn.commit()
        finally:
            conn.close()

    def get_history(self, account_label: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        try:
            if account_label:
                cur = conn.execute(
                    """
                    SELECT account_label, source_name, rel_path, filename, size, synced_at
                    FROM synced_files WHERE account_label=?
                    ORDER BY synced_at DESC LIMIT ?
                    """,
                    (account_label, limit),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT account_label, source_name, rel_path, filename, size, synced_at
                    FROM synced_files ORDER BY synced_at DESC LIMIT ?
                    """,
                    (limit,),
                )
            rows = cur.fetchall()
            return [
                {
                    "account_label": r[0],
                    "source_name": r[1],
                    "rel_path": r[2],
                    "filename": r[3],
                    "size": r[4],
                    "synced_at": r[5],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_stats(self, account_label: Optional[str] = None) -> Dict[str, Any]:
        conn = sqlite3.connect(self.db_path)
        try:
            if account_label:
                cur = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM synced_files WHERE account_label=?",
                    (account_label,),
                )
            else:
                cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM synced_files")
            count, total_bytes = cur.fetchone()
            return {"synced_count": count or 0, "synced_bytes": total_bytes or 0}
        finally:
            conn.close()

    def clear_history(self, account_label: Optional[str] = None) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            if account_label:
                cur = conn.execute("DELETE FROM synced_files WHERE account_label=?", (account_label,))
            else:
                cur = conn.execute("DELETE FROM synced_files")
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


class PixelRelayEngine:
    """Core ADB relay engine with flexible account, source, and file-scope filtering."""

    def __init__(
        self,
        config: Dict[str, Any],
        db_path: str = "relay_history.db",
        log_file: str = "relay.log",
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.db = SyncDatabase(db_path)
        self.log_file = log_file
        self.log_callback = log_callback
        self.stop_event = threading.Event()

        relay_cfg = config.get("relay", {})
        self.adb = find_adb(relay_cfg.get("adb_path", "auto"))
        self.staging_dir = relay_cfg.get("staging_dir", "/sdcard/Download/PixelStaging")
        self.camera_dir = relay_cfg.get("camera_dir", "/sdcard/DCIM/Camera")
        self.device_serial: Optional[str] = None
        self.device_model: str = "Pixel"

    def log(self, msg: str) -> None:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {msg}"
        print(line, flush=True)
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        if self.log_callback:
            try:
                self.log_callback(line)
            except Exception:
                pass

    def run_adb(self, args: List[str], timeout: int = 60, serial: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = [self.adb]
        target_serial = serial or self.device_serial
        if target_serial:
            cmd.extend(["-s", target_serial])
        cmd.extend(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def adb_shell(self, shell_cmd: str, timeout: int = 60, serial: Optional[str] = None) -> str:
        try:
            res = self.run_adb(["shell", shell_cmd], timeout=timeout, serial=serial)
            return res.stdout.strip()
        except Exception as e:
            self.log(f"⚠️ ADB shell error ({shell_cmd[:40]}...): {e}")
            return ""

    def inspect_device(self) -> Dict[str, Any]:
        """Return live ADB connection status, device model, battery, free storage, and backup state."""
        preferred = self.config.get("account", {}).get("device_serial", "auto")
        try:
            res = subprocess.run([self.adb, "devices", "-l"], capture_output=True, text=True, timeout=10)
        except Exception as e:
            return {"connected": False, "error": f"ADB unavailable ({self.adb}): {e}"}

        lines = [l.strip() for l in res.stdout.splitlines()[1:] if l.strip() and "device " in l]
        if not lines:
            return {"connected": False, "error": "No authorized ADB device detected"}

        chosen = None
        if preferred and preferred != "auto":
            for l in lines:
                if l.startswith(preferred):
                    chosen = l
                    break
        if not chosen:
            for l in lines:
                if any(k in l.lower() for k in ("pixel", "flame", "coral", "redfin", "sunfish", "bramble")):
                    chosen = l
                    break
        if not chosen:
            chosen = lines[0]

        parts = chosen.split()
        serial = parts[0]
        model = "Android Device"
        product = ""
        for p in parts:
            if p.startswith("model:"):
                model = p.split("model:", 1)[1].replace("_", " ")
            elif p.startswith("product:"):
                product = p.split("product:", 1)[1]
        if product in PIXEL_CODENAMES and "pixel" not in model.lower():
            model = PIXEL_CODENAMES[product]

        self.device_serial = serial
        self.device_model = model

        # Free space
        df_out = self.adb_shell("df -k /sdcard | tail -n 1", timeout=10, serial=serial)
        free_gb = 0.0
        total_gb = 0.0
        df_parts = df_out.split()
        if len(df_parts) >= 4 and df_parts[1].isdigit() and df_parts[3].isdigit():
            total_gb = round(int(df_parts[1]) / (1024 * 1024), 2)
            free_gb = round(int(df_parts[3]) / (1024 * 1024), 2)

        # Battery level
        bat_out = self.adb_shell("dumpsys battery | grep level", timeout=10, serial=serial)
        battery_pct = None
        m_bat = re.search(r"level:\s*(\d+)", bat_out)
        if m_bat:
            battery_pct = int(m_bat.group(1))

        # Staging & Camera counts
        cam_count_out = self.adb_shell(f"ls -1 {self.camera_dir} 2>/dev/null | wc -l", timeout=10, serial=serial)
        cam_count = int(cam_count_out.strip()) if cam_count_out.strip().isdigit() else 0

        stg_out = self.adb_shell(f"du -sk {self.staging_dir} 2>/dev/null | cut -f1", timeout=10, serial=serial)
        staging_mb = round(int(stg_out) / 1024, 1) if stg_out.isdigit() else 0.0

        return {
            "connected": True,
            "serial": serial,
            "model": model,
            "product": product,
            "battery_pct": battery_pct,
            "total_gb": total_gb,
            "free_gb": free_gb,
            "camera_file_count": cam_count,
            "staging_mb": staging_mb,
        }

    def get_allowed_extensions(self) -> Set[str]:
        scope = self.config.get("scope", {})
        exts: Set[str] = set()
        if scope.get("include_photos", True):
            exts |= PHOTO_EXTS
        if scope.get("include_videos", True):
            exts |= VIDEO_EXTS
        if scope.get("include_raw", False):
            exts |= RAW_EXTS
        for custom in scope.get("custom_extensions", []):
            c = custom.strip().lower()
            if c:
                if not c.startswith("."):
                    c = "." + c
                exts.add(c)
        return exts

    def scan_pending_files(self, limit_preview: int = 50) -> Dict[str, Any]:
        """Scan enabled sources applying date, extension, size, exclude, and SQLite dedup filters."""
        account_label = self.config.get("account", {}).get("google_account_label", "default")
        scope = self.config.get("scope", {})
        allowed_exts = self.get_allowed_extensions()
        exclude_patterns = [p.lower() for p in scope.get("exclude_patterns", []) if p.strip()]
        min_bytes = int(scope.get("min_file_size_kb", 1)) * 1024
        max_mb = float(scope.get("max_file_size_mb", 0) or 0)
        max_bytes = int(max_mb * 1024 * 1024) if max_mb > 0 else 0

        date_from_ts: Optional[float] = None
        date_to_ts: Optional[float] = None
        if scope.get("date_from"):
            date_from_ts = datetime.datetime.strptime(scope["date_from"], "%Y-%m-%d").timestamp()
        if scope.get("date_to"):
            dt_to = datetime.datetime.strptime(scope["date_to"], "%Y-%m-%d") + datetime.timedelta(days=1)
            date_to_ts = dt_to.timestamp()

        pending: List[Dict[str, Any]] = []
        total_bytes = 0
        skipped_synced = 0
        source_summary: List[Dict[str, Any]] = []

        for src in self.config.get("sources", []):
            if not src.get("enabled", True):
                continue
            src_name = src.get("name", "Unnamed")
            src_type = src.get("type", "mac_folder")
            raw_path = src.get("path", "")
            expanded_path = os.path.abspath(os.path.expanduser(raw_path))

            src_count = 0
            src_bytes = 0
            exists = os.path.isdir(expanded_path)

            if exists:
                for root, dirs, files in os.walk(expanded_path):
                    # Skip hidden/excluded directories
                    dirs[:] = [
                        d for d in dirs
                        if not d.startswith(".") and not any(pat in d.lower() for pat in exclude_patterns)
                    ]
                    for fname in files:
                        if fname.startswith("."):
                            continue
                        full_path = os.path.join(root, fname)
                        lower_full = full_path.lower()
                        if any(pat in lower_full for pat in exclude_patterns):
                            continue
                        ext = os.path.splitext(fname)[1].lower()
                        if ext not in allowed_exts:
                            continue
                        try:
                            st = os.stat(full_path)
                        except OSError:
                            continue

                        sz = st.st_size
                        mtime = st.st_mtime
                        if sz < min_bytes:
                            continue
                        if max_bytes > 0 and sz > max_bytes:
                            continue
                        if date_from_ts is not None and mtime < date_from_ts:
                            continue
                        if date_to_ts is not None and mtime >= date_to_ts:
                            continue

                        rel_path = os.path.relpath(full_path, expanded_path)
                        if self.db.is_synced(account_label, src_name, rel_path, sz):
                            skipped_synced += 1
                            continue

                        pending.append(
                            {
                                "source_name": src_name,
                                "source_type": src_type,
                                "full_path": full_path,
                                "rel_path": rel_path,
                                "filename": fname,
                                "size": sz,
                                "mtime": mtime,
                                "date": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"),
                            }
                        )
                        total_bytes += sz
                        src_count += 1
                        src_bytes += sz

            source_summary.append(
                {
                    "name": src_name,
                    "type": src_type,
                    "path": expanded_path,
                    "exists": exists,
                    "pending_count": src_count,
                    "pending_gb": round(src_bytes / (1024 ** 3), 3),
                }
            )

        return {
            "account_label": account_label,
            "total_pending_count": len(pending),
            "total_pending_bytes": total_bytes,
            "total_pending_gb": round(total_bytes / (1024 ** 3), 3),
            "skipped_already_synced": skipped_synced,
            "sources": source_summary,
            "preview": pending[:limit_preview],
            "files": pending,
        }

    def get_backup_status(self) -> Tuple[str, int]:
        """Inspect Pixel Camera folder and Google Photos UI via UIAutomator."""
        cam_out = self.adb_shell(f"ls -1 {self.camera_dir} 2>/dev/null | wc -l", timeout=10)
        cam_count = int(cam_out.strip()) if cam_out.strip().isdigit() else 0
        if cam_count == 0:
            return ("complete", 0)

        self.adb_shell("uiautomator dump /sdcard/ui_status.xml >/dev/null 2>&1", timeout=15)
        tmp_xml = f"/tmp/pixel_ui_{self.device_serial or 'default'}.xml"
        try:
            self.run_adb(["pull", "/sdcard/ui_status.xml", tmp_xml], timeout=10)
        except Exception:
            return ("backing_up", cam_count)

        if not os.path.exists(tmp_xml):
            return ("backing_up", cam_count)

        try:
            tree = ET.parse(tmp_xml)
            for elem in tree.iter():
                combined = f"{elem.attrib.get('text', '')} {elem.attrib.get('content-desc', '')}"
                for pat in REMAINING_PATTERNS:
                    m = re.search(pat, combined, re.IGNORECASE)
                    if m:
                        return ("backing_up", int(m.group(1)))
                if any(kw.lower() in combined.lower() for kw in COMPLETE_KEYWORDS):
                    return ("complete", 0)
        except Exception:
            pass

        return ("backing_up", cam_count)

    def _init_pixel_dirs(self) -> None:
        self.adb_shell(f"mkdir -p {shlex.quote(self.staging_dir)}")
        self.adb_shell(f"mkdir -p {shlex.quote(self.camera_dir)}")
        self.adb_shell(f"touch {shlex.quote(self.staging_dir + '/.nomedia')}")
        self.adb_shell("settings put global stay_on_while_plugged_in 3")

    def _wait_for_camera_cleanup(self, poll_sec: int) -> bool:
        """Wait until Google Photos completes backup, then clear Camera folder if configured."""
        auto_clean = self.config.get("relay", {}).get("auto_clean_camera_after_backup", True)
        while not self.stop_event.is_set():
            status, rem = self.get_backup_status()
            if status == "complete":
                if auto_clean:
                    self.log(f"🧹 Google 相簿備份已完成，清空 {self.camera_dir} 騰出 Pixel 空間...")
                    self.adb_shell(f"rm -rf {shlex.quote(self.camera_dir)}/*")
                return True
            self.log(f"⏳ 等待 Google 相簿上傳中（剩餘約 {rem} 項），{poll_sec} 秒後再檢查...")
            if self.stop_event.wait(poll_sec):
                return False
        return False

    def run(self) -> Dict[str, Any]:
        """Execute batched double-buffered relay to Pixel 4."""
        self.stop_event.clear()
        account_label = self.config.get("account", {}).get("google_account_label", "default")
        relay_cfg = self.config.get("relay", {})
        batch_bytes = int(float(relay_cfg.get("batch_size_gb", 15.0)) * (1024 ** 3))
        min_free_gb = float(relay_cfg.get("min_free_gb", 12.0))
        delete_src = bool(relay_cfg.get("delete_source_after_push", False))
        poll_sec = int(relay_cfg.get("poll_interval_sec", 15))

        dev = self.inspect_device()
        if not dev.get("connected"):
            self.log(f"❌ {dev.get('error', '無法連接 Pixel 設備')}")
            return {"status": "error", "error": dev.get("error")}

        self.log("=" * 55)
        self.log(f"🚀 啟動 Pixel 無限 Google 相簿中繼任務")
        self.log(f"👤 目標 Google 帳號標籤: {account_label}")
        self.log(f"📱 連接裝置: {dev['model']} ({dev['serial']}) | 剩餘 {dev['free_gb']} GB")
        self.log(f"🔄 模式: {'移動並刪除來源' if delete_src else '安全複製 (保留 Mac/iPhone 來源檔)'}")
        self.log("=" * 55)

        self._init_pixel_dirs()
        scan = self.scan_pending_files(limit_preview=0)
        pending = scan["files"]

        if not pending:
            self.log("🎉 沒有待備份的新增照片或影片（符合範圍的檔案皆已同步）。")
            return {"status": "completed", "pushed_files": 0, "pushed_bytes": 0}

        self.log(f"📋 掃描完成：共 {len(pending)} 個待備份檔案（{scan['total_pending_gb']} GB），已略過 {scan['skipped_already_synced']} 個已備份檔案。")

        total_pushed = 0
        total_bytes = 0
        idx = 0
        batch_num = 1

        while idx < len(pending) and not self.stop_event.is_set():
            dev_now = self.inspect_device()
            if not dev_now.get("connected"):
                self.log("⚠️ 裝置斷線，等待重新連接...")
                if self.stop_event.wait(10):
                    break
                continue

            if dev_now["free_gb"] < min_free_gb:
                self.log(f"💾 Pixel 可用空間 ({dev_now['free_gb']} GB) 低於安全門檻 ({min_free_gb} GB)，檢查備份狀態...")
                self._wait_for_camera_cleanup(poll_sec)
                continue

            batch: List[Dict[str, Any]] = []
            current_b = 0
            while idx < len(pending):
                item = pending[idx]
                batch.append(item)
                current_b += item["size"]
                idx += 1
                if current_b >= batch_bytes:
                    break

            self.log(f"📦 [第 {batch_num} 批] 準備推送 {len(batch)} 個檔案 ({current_b / (1024**3):.2f} GB) 至預載區...")
            pushed_batch = 0

            for item in batch:
                if self.stop_event.is_set():
                    break
                # Avoid filename collisions across subfolders using short hash prefix if needed
                safe_name = item["filename"]
                dst_staging = f"{self.staging_dir}/{safe_name}"
                try:
                    res = self.run_adb(["push", item["full_path"], dst_staging], timeout=180)
                except Exception as e:
                    self.log(f"❌ 推送異常 {safe_name}: {e}")
                    continue

                if res.returncode == 0:
                    pushed_batch += 1
                    total_pushed += 1
                    total_bytes += item["size"]
                    self.db.mark_synced(
                        account_label,
                        item["source_name"],
                        item["rel_path"],
                        item["filename"],
                        item["size"],
                        item["mtime"],
                    )
                    if delete_src:
                        try:
                            os.remove(item["full_path"])
                        except OSError as e:
                            self.log(f"⚠️ 無法刪除來源 {safe_name}: {e}")
                    if pushed_batch % 25 == 0 or pushed_batch == len(batch):
                        self.log(f"📊 [第 {batch_num} 批進度] {pushed_batch}/{len(batch)} 檔")
                else:
                    self.log(f"❌ 推送失敗 {safe_name}: {res.stderr.strip()}")

            if self.stop_event.is_set():
                self.log("🛑 使用者要求停止任務，已安全中止。")
                break

            self.log(f"⏳ 確認 {self.camera_dir} 先前批次是否已備份完成...")
            if not self._wait_for_camera_cleanup(poll_sec):
                break

            self.log(f"🚀 將 {pushed_batch} 個檔案移入 {self.camera_dir} 觸發 Google 相簿無限空間備份...")
            self.adb_shell(f"mv {shlex.quote(self.staging_dir)}/* {shlex.quote(self.camera_dir)}/ 2>/dev/null || true")
            self.adb_shell(f"rm -f {shlex.quote(self.camera_dir + '/.nomedia')}")
            self.adb_shell(f"touch {shlex.quote(self.staging_dir + '/.nomedia')}")
            self.adb_shell(
                f'for f in {shlex.quote(self.camera_dir)}/*; do am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "file://$f" >/dev/null 2>&1; done &'
            )

            self.log(f"✅ 第 {batch_num} 批已進入相機目錄，Google 相簿正在背景上傳！")
            batch_num += 1
            if self.stop_event.wait(5):
                break

        self.log(f"🏆 任務結束：共處理 {total_pushed} 個檔案（{total_bytes / (1024**3):.2f} GB）。")
        return {"status": "completed", "pushed_files": total_pushed, "pushed_bytes": total_bytes}


def main() -> None:
    parser = argparse.ArgumentParser(description="Pixel 4 Unlimited Google Photos Relay CLI")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--scan-only", action="store_true", help="Only scan and report matching files")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Config file not found: {args.config}. Copy config.example.json to config.json first.")
        sys.exit(1)

    cfg = load_config(args.config)
    engine = PixelRelayEngine(cfg)
    if args.scan_only:
        res = engine.scan_pending_files(limit_preview=20)
        print(json.dumps({k: v for k, v in res.items() if k != "files"}, indent=2, ensure_ascii=False))
    else:
        engine.run()


if __name__ == "__main__":
    main()
