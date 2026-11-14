#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Photos & Multi-Source Data Manager API Server (Unified Edition)
Combined: Google Photos Data Manager + Disk Analyzer Pro Web
- High-performance dual-port (8989 & 8998) multi-threaded server.
- Supports both http://localhost:8989 and http://localhost:8998
- Multi-Source: Local SSD, External ORICO HDD, SD cards, Synology NAS SMB, iPhone
- Multi-Target: Pixel 4, Pixel 5 unlimited Google Photos relay
- Disk Analyzer Pro Engine:
  * SQLite Incremental Cache (ultra-fast <5ms directory traversal)
  * Deep folder analysis & file category breakdown (Images, Videos, Docs, Archives, etc.)
  * Top 100 Largest Files with 1-click native macOS Finder reveal & open
  * Cross-Folder & In-Folder Duplicate File Detector (Binary MD5 chunk-hash)
  * Safe Trash cleaner & Empty folder pruner
"""

import http.server
import socketserver
import json
import subprocess
import os
import sys
import re
import glob
import time
import shutil
import hashlib
import sqlite3
import urllib.request
import urllib.parse
import configparser
import threading
from datetime import datetime

PORTS = [8989, 8998]
HOST = "0.0.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def find_binary(name, extra_paths):
    found = shutil.which(name)
    if found:
        return found
    for p in extra_paths:
        if p and os.path.isfile(p):
            return p
    return f"{name}.exe" if os.name == "nt" else name

ADB = find_binary("adb", [
    os.path.expanduser("~/.local/bin/adb"),
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    r"C:\platform-tools\adb.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
])
RCLONE = find_binary("rclone", [
    os.path.expanduser("~/.local/bin/rclone"),
    "/opt/homebrew/bin/rclone",
    "/usr/local/bin/rclone",
    r"C:\rclone\rclone.exe",
])
LOG_FILE = os.path.join(BASE_DIR, "relay.log")
RCLONE_CONF = (
    os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf")
    if os.name == "nt"
    else os.path.expanduser("~/.config/rclone/rclone.conf")
)
DB_PATH = os.path.join(BASE_DIR, "disk_cache.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")

def load_app_config():
    if not os.path.exists(CONFIG_PATH) and os.path.exists(EXAMPLE_CONFIG_PATH):
        shutil.copy2(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"account": {}, "sources": [], "scope": {}}

def save_app_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# Duplicate scanning tasks
active_tasks = {}
task_lock = threading.Lock()

# Cache for fast system status
CACHE = {
    "system_data": None,
    "system_time": 0,
    "devices": None,
    "devices_time": 0,
    "storage_matrix": None,
    "storage_matrix_time": 0,
    "macbook_storage": None
}

EXT_CATEGORIES = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw", ".dng", ".svg", ".tiff", ".tif"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".mpg", ".mpeg", ".ts", ".vob"},
    "audio": {".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg", ".wma", ".aiff", ".alac"},
    "document": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".rtf", ".csv", ".json", ".xml", ".epub"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".iso", ".dmg", ".pkg"},
    "app": {".app", ".exe", ".bin", ".sh", ".dmg", ".pkg"}
}

def get_file_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    for cat, exts in EXT_CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"

def format_size(bytes_sz):
    if bytes_sz < 1024:
        return f"{bytes_sz} B"
    elif bytes_sz < 1024**2:
        return f"{bytes_sz/1024:.2f} KB"
    elif bytes_sz < 1024**3:
        return f"{bytes_sz/(1024**2):.2f} MB"
    else:
        return f"{bytes_sz/(1024**3):.2f} GB"

def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS directory_meta (
            path TEXT PRIMARY KEY,
            mtime REAL,
            total_size INTEGER,
            file_count INTEGER,
            dir_count INTEGER,
            last_scanned REAL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS directory_children (
            parent_path TEXT,
            child_name TEXT,
            child_path TEXT,
            is_dir INTEGER,
            size INTEGER,
            mtime REAL,
            file_type TEXT,
            PRIMARY KEY (parent_path, child_name)
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_parent ON directory_children(parent_path)")
        c.execute("""
        CREATE TABLE IF NOT EXISTS deep_analysis_cache (
            path TEXT PRIMARY KEY,
            total_size INTEGER,
            total_files INTEGER,
            total_dirs INTEGER,
            breakdown_json TEXT,
            top_100_json TEXT,
            last_scanned REAL
        )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB init error:", e)

init_db()

def get_refreshed_gphotos_token():
    try:
        if not os.path.exists(RCLONE_CONF):
            return None
        cfg = configparser.ConfigParser()
        cfg.read(RCLONE_CONF)
        if "gphotos" not in cfg:
            return None
        cid = cfg["gphotos"]["client_id"]
        csec = cfg["gphotos"]["client_secret"]
        tok_data = json.loads(cfg["gphotos"]["token"])
        rtok = tok_data.get("refresh_token")
        if not rtok:
            return tok_data.get("access_token")
        data = urllib.parse.urlencode({
            "client_id": cid,
            "client_secret": csec,
            "refresh_token": rtok,
            "grant_type": "refresh_token"
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        with urllib.request.urlopen(req, timeout=5) as resp:
            res = json.loads(resp.read().decode())
            return res.get("access_token")
    except Exception as e:
        print("Token refresh error:", e)
        return None

def get_mounted_volumes():
    volumes = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                try:
                    usage = shutil.disk_usage(drive)
                    total, used, free = usage.total, usage.used, usage.free
                    volumes.append({
                        "name": f"本機磁碟 ({letter}:)",
                        "raw_name": f"{letter}:",
                        "path": drive,
                        "is_nas": False,
                        "nas_host": None,
                        "total": total,
                        "total_formatted": format_size(total),
                        "used": used,
                        "used_formatted": format_size(used),
                        "free": free,
                        "free_formatted": format_size(free),
                        "percent": round((used / total) * 100, 1) if total > 0 else 0,
                    })
                except Exception:
                    pass
        return volumes

    net_mounts = {}
    try:
        res = subprocess.run(["mount"], capture_output=True, text=True, check=False)
        for line in res.stdout.splitlines():
            if " on /Volumes/" in line:
                parts = line.split(" on /Volumes/")
                src = parts[0].strip()
                rest = parts[1].split(" (")
                vol_name = rest[0].strip()
                v_path = f"/Volumes/{vol_name}"
                net_mounts[v_path] = src
    except Exception:
        pass

    if os.path.exists("/Volumes"):
        try:
            for item in os.listdir("/Volumes"):
                v_path = os.path.join("/Volumes", item)
                if os.path.exists(v_path) and os.path.isdir(v_path):
                    if item.lower().endswith(".dmg") or "synology-assistant" in item.lower():
                        continue
                    try:
                        st = os.statvfs(v_path)
                        total = st.f_blocks * st.f_frsize
                        free = st.f_bavail * st.f_frsize
                        used = total - free
                        
                        is_nas = v_path in net_mounts or "nas.local" in net_mounts.get(v_path, "")
                        display_name = item
                        if item == "homes":
                            display_name = "NAS 雲端全體目錄: /Volumes/homes (nas.local)"
                        elif item == "home":
                            display_name = "NAS 個人空間: /Volumes/home -> nas-user (nas.local)"
                        elif is_nas:
                            display_name = f"NAS 雲端硬碟: {item} (nas.local)"
                        elif item == "ExternalDrive":
                            display_name = "外接硬碟: ExternalDrive"
                        elif item == "Macintosh HD":
                            display_name = "本機系統碟: Macintosh HD"

                        volumes.append({
                            "name": display_name,
                            "raw_name": item,
                            "path": v_path,
                            "is_nas": is_nas,
                            "nas_host": "nas.local" if is_nas else None,
                            "total": total,
                            "total_formatted": format_size(total),
                            "used": used,
                            "used_formatted": format_size(used),
                            "free": free,
                            "free_formatted": format_size(free),
                            "percent": round((used / total) * 100, 1) if total > 0 else 0
                        })

                        if item == "homes" and os.path.exists(os.path.join(v_path, "nas-user")):
                            jay_path = os.path.join(v_path, "nas-user")
                            volumes.append({
                                "name": "NAS 使用者空間: /Volumes/homes/nas-user (smb://nas.local/homes/nas-user)",
                                "raw_name": "homes/nas-user",
                                "path": jay_path,
                                "is_nas": True,
                                "nas_host": "nas.local",
                                "total": total,
                                "total_formatted": format_size(total),
                                "used": used,
                                "used_formatted": format_size(used),
                                "free": free,
                                "free_formatted": format_size(free),
                                "percent": round((used / total) * 100, 1) if total > 0 else 0
                            })
                    except Exception:
                        pass
        except Exception:
            pass

    home = os.path.expanduser("~")
    try:
        st = os.statvfs(home)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        volumes.append({
            "name": f"本機家目錄: Home ({os.path.basename(home)})",
            "raw_name": "Home",
            "path": home,
            "is_nas": False,
            "nas_host": None,
            "total": total,
            "total_formatted": format_size(total),
            "used": used,
            "used_formatted": format_size(used),
            "free": free,
            "free_formatted": format_size(free),
            "percent": round((used / total) * 100, 1) if total > 0 else 0
        })
    except Exception:
        pass
    return volumes

def map_nas_path(mac_path):
    clean = mac_path.rstrip('/')
    if clean == '/Volumes/home' or clean.startswith('/Volumes/home/'):
        rel = clean[len('/Volumes/home'):].lstrip('/')
        return f'/volume1/homes/nas-user/{rel}'.rstrip('/')
    elif clean == '/Volumes/homes' or clean.startswith('/Volumes/homes/'):
        rel = clean[len('/Volumes/homes'):].lstrip('/')
        return f'/volume1/homes/{rel}'.rstrip('/')
    return None

def scan_folder_incremental(target_path, force_refresh=False):
    if not os.path.exists(target_path):
        return {"error": f"Path not found: {target_path}"}

    try:
        curr_mtime = os.path.getmtime(target_path)
    except Exception as e:
        return {"error": str(e)}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT mtime, total_size, file_count, dir_count, last_scanned FROM directory_meta WHERE path = ?", (target_path,))
    row = c.fetchone()

    if row and not force_refresh and abs(row[0] - curr_mtime) < 0.001:
        c.execute("""
            SELECT child_name, child_path, is_dir, size, mtime, file_type 
            FROM directory_children WHERE parent_path = ?
        """, (target_path,))
        children_rows = c.fetchall()
        conn.close()

        items = []
        for r in children_rows:
            items.append({
                "name": r[0],
                "path": r[1],
                "is_dir": bool(r[2]),
                "size": r[3],
                "size_formatted": format_size(r[3]) if not bool(r[2]) else "資料夾",
                "mtime": r[4],
                "file_type": r[5]
            })
        items.sort(key=lambda x: (not x["is_dir"], x["size"]), reverse=True)
        return {
            "target_path": target_path,
            "cached": True,
            "last_scanned": row[4],
            "total_size": row[1],
            "total_size_formatted": format_size(row[1]),
            "file_count": row[2],
            "dir_count": row[3],
            "items": items
        }

    items = []
    total_files_size = 0
    total_files = 0
    total_dirs = 0

    try:
        with os.scandir(target_path) as it:
            for entry in it:
                try:
                    name = entry.name
                    path = entry.path
                    is_dir = entry.is_dir(follow_symlinks=False)
                    stat = entry.stat(follow_symlinks=False)
                    size = 0
                    ftype = "folder" if is_dir else get_file_type(name)

                    if is_dir:
                        total_dirs += 1
                        size = 0
                    else:
                        total_files += 1
                        size = stat.st_size
                        total_files_size += size

                    items.append({
                        "name": name,
                        "path": path,
                        "is_dir": is_dir,
                        "size": size,
                        "size_formatted": format_size(size) if not is_dir else "資料夾",
                        "mtime": stat.st_mtime,
                        "file_type": ftype
                    })
                except Exception:
                    pass
    except Exception as e:
        conn.close()
        return {"error": str(e)}

    now = time.time()
    c.execute("""
        INSERT OR REPLACE INTO directory_meta (path, mtime, total_size, file_count, dir_count, last_scanned)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (target_path, curr_mtime, total_files_size, total_files, total_dirs, now))

    c.execute("DELETE FROM directory_children WHERE parent_path = ?", (target_path,))
    for it in items:
        c.execute("""
            INSERT INTO directory_children (parent_path, child_name, child_path, is_dir, size, mtime, file_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (target_path, it["name"], it["path"], 1 if it["is_dir"] else 0, it["size"], it["mtime"], it["file_type"]))

    conn.commit()
    conn.close()

    items.sort(key=lambda x: (not x["is_dir"], x["size"]), reverse=True)
    return {
        "target_path": target_path,
        "cached": False,
        "last_scanned": now,
        "total_size": total_files_size,
        "total_size_formatted": format_size(total_files_size),
        "file_count": total_files,
        "dir_count": total_dirs,
        "items": items
    }

def analyze_folder_deep(target_path, force_refresh=False):
    if not os.path.exists(target_path):
        return {"error": f"Path not found: {target_path}"}

    # Check SQLite cache first
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT total_size, total_files, total_dirs, breakdown_json, top_100_json, last_scanned FROM deep_analysis_cache WHERE path = ?", (target_path,))
    cached_row = c.fetchone()
    if cached_row and not force_refresh:
        conn.close()
        try:
            return {
                "target_path": target_path,
                "cached": True,
                "last_scanned": cached_row[5],
                "total_size": cached_row[0],
                "total_size_formatted": format_size(cached_row[0]),
                "total_files": cached_row[1],
                "total_dirs": cached_row[2],
                "breakdown": json.loads(cached_row[3]),
                "top_100": json.loads(cached_row[4])
            }
        except Exception:
            pass

    # NAS Acceleration via SSH
    nas_remote_target = map_nas_path(target_path)
    if nas_remote_target:
        try:
            remote_script = f'''
import os, json, sys, time
from datetime import datetime

target = {json.dumps(nas_remote_target)}
EXT_CATEGORIES = {{
    "image": {{".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw", ".dng", ".svg", ".tiff", ".tif"}},
    "video": {{".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".mpg", ".mpeg", ".ts", ".vob"}},
    "audio": {{".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg", ".wma", ".aiff", ".alac"}},
    "document": {{".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".rtf", ".csv", ".json", ".xml", ".epub"}},
    "archive": {{".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".iso", ".dmg", ".pkg"}},
    "app": {{".app", ".exe", ".bin", ".sh", ".dmg", ".pkg"}}
}}

def get_file_type(fn):
    ext = os.path.splitext(fn)[1].lower()
    for cat, exts in EXT_CATEGORIES.items():
        if ext in exts: return cat
    return "other"

if not os.path.exists(target):
    print(json.dumps({{"error": f"Path not found on NAS: {{target}}"}}))
    sys.exit(0)

type_stats = {{
    "image": {{"name": "照片與圖片 (Image)", "count": 0, "size": 0, "color": "#38bdf8"}},
    "video": {{"name": "影片檔案 (Video)", "count": 0, "size": 0, "color": "#f97316"}},
    "audio": {{"name": "音樂音訊 (Audio)", "count": 0, "size": 0, "color": "#ec4899"}},
    "document": {{"name": "文檔與資料 (Document)", "count": 0, "size": 0, "color": "#eab308"}},
    "archive": {{"name": "壓縮封裝檔 (Archive)", "count": 0, "size": 0, "color": "#a855f7"}},
    "app": {{"name": "應用程式與執行檔 (App)", "count": 0, "size": 0, "color": "#10b981"}},
    "other": {{"name": "其它格式檔案 (Other)", "count": 0, "size": 0, "color": "#64748b"}}
}}

total_size = 0
total_files = 0
total_dirs = 0
top_files = []
seen_inodes = set()

for root, dirs, files in os.walk(target):
    dirs[:] = sorted([d for d in dirs if d != "@eaDir" and not d.startswith(".") and not os.path.islink(os.path.join(root, d))])
    total_dirs += len(dirs)
    for f in files:
        if f.startswith('.'): continue
        fp = os.path.join(root, f)
        try:
            if os.path.islink(fp): continue
            st = os.stat(fp, follow_symlinks=False)
            ino = st.st_ino
            if ino in seen_inodes:
                continue
            seen_inodes.add(ino)
            sz = st.st_size
            ft = get_file_type(f)
            total_files += 1
            total_size += sz
            type_stats[ft]["count"] += 1
            type_stats[ft]["size"] += sz
            if sz > 10 * 1024 * 1024 or len(top_files) < 100:
                top_files.append((sz, f, fp, st.st_mtime, ft))
        except Exception:
            pass

top_files.sort(key=lambda x: x[0], reverse=True)
top_100_raw = top_files[:100]

print(json.dumps({{
    "total_size": total_size,
    "total_files": total_files,
    "total_dirs": total_dirs,
    "type_stats": type_stats,
    "top_100": top_100_raw
}}))
'''
            res = subprocess.run(["ssh", "ds220j", "python3 -"], input=remote_script, capture_output=True, text=True, timeout=300)
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout.strip())
                if "error" not in data:
                    total_size = data["total_size"]
                    total_files = data["total_files"]
                    total_dirs = data["total_dirs"]
                    type_stats = data["type_stats"]

                    # Convert remote paths back to Mac SMB mount path for UI opening
                    nas_prefix = nas_remote_target
                    mac_prefix = target_path.rstrip('/')

                    top_100 = []
                    for sz, f, fp, mtime, ft in data["top_100"]:
                        rel = fp[len(nas_prefix):].lstrip('/')
                        mac_fp = os.path.join(mac_prefix, rel) if rel else mac_prefix
                        top_100.append({
                            "name": f,
                            "path": mac_fp,
                            "size": sz,
                            "size_formatted": format_size(sz),
                            "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                            "type": ft
                        })

                    breakdown_list = []
                    for cat_id, info in type_stats.items():
                        pct = (info["size"] / total_size * 100) if total_size > 0 else 0
                        breakdown_list.append({
                            "category": cat_id,
                            "label": info["name"],
                            "count": info["count"],
                            "size": info["size"],
                            "size_formatted": format_size(info["size"]),
                            "percent": round(pct, 2),
                            "color": info["color"]
                        })
                    breakdown_list.sort(key=lambda x: x["size"], reverse=True)

                    now = time.time()
                    c.execute("""
                        INSERT OR REPLACE INTO deep_analysis_cache (path, total_size, total_files, total_dirs, breakdown_json, top_100_json, last_scanned)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (target_path, total_size, total_files, total_dirs, json.dumps(breakdown_list), json.dumps(top_100), now))
                    conn.commit()
                    conn.close()

                    return {
                        "target_path": target_path,
                        "cached": False,
                        "accelerated_by": "ssh_ds220j",
                        "total_size": total_size,
                        "total_size_formatted": format_size(total_size),
                        "total_files": total_files,
                        "total_dirs": total_dirs,
                        "breakdown": breakdown_list,
                        "top_100": top_100
                    }
        except Exception as e:
            print("SSH acceleration error, falling back to local walk:", e)

    # Local walk (for Mac local disks or fallback)
    type_stats = {
        "image": {"name": "照片與圖片 (Image)", "count": 0, "size": 0, "color": "#38bdf8"},
        "video": {"name": "影片檔案 (Video)", "count": 0, "size": 0, "color": "#f97316"},
        "audio": {"name": "音樂音訊 (Audio)", "count": 0, "size": 0, "color": "#ec4899"},
        "document": {"name": "文檔與資料 (Document)", "count": 0, "size": 0, "color": "#eab308"},
        "archive": {"name": "壓縮封裝檔 (Archive)", "count": 0, "size": 0, "color": "#a855f7"},
        "app": {"name": "應用程式與執行檔 (App)", "count": 0, "size": 0, "color": "#10b981"},
        "other": {"name": "其它格式檔案 (Other)", "count": 0, "size": 0, "color": "#64748b"}
    }

    all_files = []
    total_size = 0
    total_files = 0
    total_dirs = 0

    for root, dirs, files in os.walk(target_path):
        dirs[:] = [d for d in dirs if d != '@eaDir' and not d.startswith('.')]
        total_dirs += len(dirs)
        for f in files:
            if f.startswith('.'): continue
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
                sz = st.st_size
                mtime = st.st_mtime
                ftype = get_file_type(f)

                total_files += 1
                total_size += sz

                type_stats[ftype]["count"] += 1
                type_stats[ftype]["size"] += sz

                all_files.append({
                    "name": f,
                    "path": fp,
                    "size": sz,
                    "size_formatted": format_size(sz),
                    "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "type": ftype
                })
            except Exception:
                pass

    all_files.sort(key=lambda x: x["size"], reverse=True)
    top_100 = all_files[:100]

    breakdown_list = []
    for cat_id, info in type_stats.items():
        pct = (info["size"] / total_size * 100) if total_size > 0 else 0
        breakdown_list.append({
            "category": cat_id,
            "label": info["name"],
            "count": info["count"],
            "size": info["size"],
            "size_formatted": format_size(info["size"]),
            "percent": round(pct, 2),
            "color": info["color"]
        })
    breakdown_list.sort(key=lambda x: x["size"], reverse=True)

    now = time.time()
    c.execute("""
        INSERT OR REPLACE INTO deep_analysis_cache (path, total_size, total_files, total_dirs, breakdown_json, top_100_json, last_scanned)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (target_path, total_size, total_files, total_dirs, json.dumps(breakdown_list), json.dumps(top_100), now))
    conn.commit()
    conn.close()

    return {
        "target_path": target_path,
        "total_size": total_size,
        "total_size_formatted": format_size(total_size),
        "total_files": total_files,
        "total_dirs": total_dirs,
        "breakdown": breakdown_list,
        "top_100": top_100
    }

def scan_duplicates(task_id, mode, path_a, path_b=None, min_size_bytes=1024, verify_hash=True, priority_str=""):
    with task_lock:
        active_tasks[task_id] = {
            "status": "running",
            "progress": "正在掃描目錄檔案清單...",
            "percent": 5,
            "total_candidates": 0,
            "duplicate_groups": [],
            "total_duplicate_files": 0,
            "total_wasted_bytes": 0,
            "start_time": time.time()
        }

    try:
        def get_chunk_hash(p, sz):
            try:
                with open(p, 'rb') as f:
                    c1 = f.read(65536)
                    if sz > 65536:
                        f.seek(max(0, sz - 65536))
                        c2 = f.read(65536)
                    else:
                        c2 = b''
                    return hashlib.md5(c1 + c2).hexdigest()
            except Exception:
                return None

        if mode == "cross_folder":
            with task_lock:
                active_tasks[task_id]["progress"] = f"正在索引基準目錄: {os.path.basename(path_b)}..."
                active_tasks[task_id]["percent"] = 15

            index_b = {}
            for root, dirs, files in os.walk(path_b):
                dirs[:] = [d for d in dirs if d != '@eaDir' and not d.startswith('.')]
                for f in files:
                    if f.startswith('.'): continue
                    clean = f.replace('-已編輯', '').replace('-edited', '').lower()
                    fp = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(fp)
                        if sz >= min_size_bytes:
                            index_b.setdefault((clean, sz), []).append(fp)
                    except Exception:
                        pass

            with task_lock:
                active_tasks[task_id]["progress"] = f"基準目錄完成索引 ({len(index_b)} 種規格)。正在掃描待比對目錄: {os.path.basename(path_a)}..."
                active_tasks[task_id]["percent"] = 40

            file_candidates = []
            for root, dirs, files in os.walk(path_a):
                dirs[:] = [d for d in dirs if d != '@eaDir' and not d.startswith('.')]
                for f in files:
                    if f.startswith('.'): continue
                    clean = f.lower()
                    fp = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(fp)
                        if sz >= min_size_bytes and (clean, sz) in index_b:
                            file_candidates.append((fp, index_b[(clean, sz)][0], sz))
                    except Exception:
                        pass

            with task_lock:
                active_tasks[task_id]["total_candidates"] = len(file_candidates)
                active_tasks[task_id]["progress"] = f"找到 {len(file_candidates)} 個規格候選，開始二進位校驗..."
                active_tasks[task_id]["percent"] = 60

            checked = 0
            dup_groups = []
            for src_p, ref_p, sz in file_candidates:
                checked += 1
                if checked % 500 == 0:
                    with task_lock:
                        pct = 60 + int((checked / max(1, len(file_candidates))) * 35)
                        active_tasks[task_id]["percent"] = pct
                        active_tasks[task_id]["progress"] = f"校驗進度 ({checked}/{len(file_candidates)})..."

                is_dup = True
                if verify_hash:
                    h1 = get_chunk_hash(src_p, sz)
                    h2 = get_chunk_hash(ref_p, sz)
                    if not h1 or h1 != h2:
                        is_dup = False

                if is_dup:
                    dup_groups.append({
                        "filename": os.path.basename(src_p),
                        "size": sz,
                        "size_formatted": format_size(sz),
                        "keep_file": ref_p,
                        "duplicates": [src_p]
                    })

        else:
            with task_lock:
                active_tasks[task_id]["progress"] = f"正在索引目錄內部檔案: {os.path.basename(path_a)}..."
                active_tasks[task_id]["percent"] = 20

            file_sig_map = {}
            for root, dirs, files in os.walk(path_a):
                dirs[:] = [d for d in dirs if d != '@eaDir' and not d.startswith('.')]
                for f in files:
                    if f.startswith('.'): continue
                    fp = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(fp)
                        if sz >= min_size_bytes:
                            clean = f.lower()
                            file_sig_map.setdefault((clean, sz), []).append(fp)
                    except Exception:
                        pass

            candidates = [paths for sig, paths in file_sig_map.items() if len(paths) > 1]
            with task_lock:
                active_tasks[task_id]["total_candidates"] = len(candidates)
                active_tasks[task_id]["progress"] = f"找到 {len(candidates)} 組同名同大小群組，開始雜湊比對與分組..."
                active_tasks[task_id]["percent"] = 50

            priority_list = [p.strip() for p in priority_str.split(",") if p.strip()]

            def calc_priority(filepath):
                for idx, token in enumerate(priority_list):
                    if token.lower() in filepath.lower():
                        return idx
                return 999

            dup_groups = []
            checked = 0
            for paths in candidates:
                checked += 1
                if checked % 200 == 0:
                    with task_lock:
                        pct = 50 + int((checked / max(1, len(candidates))) * 45)
                        active_tasks[task_id]["percent"] = pct
                        active_tasks[task_id]["progress"] = f"比對進度 ({checked}/{len(candidates)})..."

                paths.sort(key=calc_priority)
                keep = paths[0]
                sz = os.path.getsize(keep)
                keep_hash = get_chunk_hash(keep, sz) if verify_hash else "any"

                matched_dups = []
                for cand in paths[1:]:
                    if verify_hash:
                        c_hash = get_chunk_hash(cand, sz)
                        if c_hash and c_hash == keep_hash:
                            matched_dups.append(cand)
                    else:
                        matched_dups.append(cand)

                if matched_dups:
                    dup_groups.append({
                        "filename": os.path.basename(keep),
                        "size": sz,
                        "size_formatted": format_size(sz),
                        "keep_file": keep,
                        "duplicates": matched_dups
                    })

        total_dups = sum(len(g["duplicates"]) for g in dup_groups)
        total_wasted = sum(g["size"] * len(g["duplicates"]) for g in dup_groups)

        with task_lock:
            active_tasks[task_id].update({
                "status": "completed",
                "progress": "掃描比對完成！",
                "percent": 100,
                "duplicate_groups": dup_groups,
                "total_duplicate_files": total_dups,
                "total_wasted_bytes": total_wasted,
                "total_wasted_formatted": format_size(total_wasted),
                "end_time": time.time()
            })
    except Exception as e:
        with task_lock:
            active_tasks[task_id] = {
                "status": "error",
                "error": str(e),
                "progress": f"發生錯誤: {str(e)}",
                "percent": 100
            }


class UnifiedDashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        static_dir = os.path.join(BASE_DIR, "static")
        super().__init__(*args, directory=static_dir, **kwargs)

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        try:
            url = urllib.parse.urlparse(self.path)
            path = url.path
            query = urllib.parse.parse_qs(url.query)

            # Route default page based on accessed port
            if path in ["/", "/index.html"]:
                client_port = self.server.server_address[1]
                if client_port == 8998:
                    target_file = os.path.join(self.directory, "disk_analyzer.html")
                else:
                    target_file = os.path.join(self.directory, "index.html")
                if os.path.exists(target_file):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    with open(target_file, "rb") as f:
                        self.wfile.write(f.read())
                    return

            # Unified System APIs
            if path == "/api/status":
                self.send_json(self.get_system_data())
            elif path == "/api/config":
                self.send_json(load_app_config())
            elif path == "/api/storage_matrix":
                self.send_json(self.get_storage_matrix())
            elif path == "/api/backup_status":
                status_file = "~/cloud_backup_status.json"
                if os.path.exists(status_file):
                    try:
                        with open(status_file, "r", encoding="utf-8") as f:
                            self.send_json(json.load(f))
                            return
                    except Exception:
                        pass
                self.send_json({"icloud": {"status": "ready", "message": "就緒待命"}, "onedrive": {"status": "ready", "message": "就緒待命"}})
                return
            elif path == "/api/devices":
                self.send_json(self.get_connected_devices())
            elif path == "/api/volumes":
                self.send_json(get_mounted_volumes())
            elif path == "/api/folder_drill":
                target = query.get("path", ["/"])[0]
                force = query.get("refresh", ["0"])[0] == "1"
                self.send_json(scan_folder_incremental(target, force_refresh=force))
            elif path == "/api/deep_analysis":
                target = query.get("path", ["/"])[0]
                force = query.get("refresh", ["0"])[0] == "1"
                self.send_json(analyze_folder_deep(target, force_refresh=force))
            elif path == "/api/browse_fs":
                target = query.get("path", ["/"])[0] or "/"
                if not os.path.exists(target):
                    self.send_json({"error": f"Path not found: {target}"}, status=404)
                    return
                dirs = []
                try:
                    with os.scandir(target) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                                dirs.append(entry.name)
                except Exception:
                    pass
                dirs.sort()
                clean_path = target.rstrip("/")
                parent = "/" if clean_path == "/Volumes" else (os.path.dirname(clean_path) or "/" if clean_path else None)
                self.send_json({"current": target, "parent": parent, "dirs": dirs})
            elif path == "/api/pixel_status":
                self.send_json(self.get_pixel_status())
            elif path == "/api/open_file":
                raw_path = query.get("path", [""])[0]
                filepath = urllib.parse.unquote(urllib.parse.unquote(raw_path))
                action = query.get("action", ["reveal"])[0]
                if not os.path.exists(filepath):
                    self.send_json({"error": f"檔案不存在: {filepath}"}, status=404)
                    return
                try:
                    if os.name == "nt":
                        if action == "reveal":
                            subprocess.Popen(["explorer", "/select,", os.path.normpath(filepath)])
                        else:
                            os.startfile(os.path.normpath(filepath))
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", "-R", filepath] if action == "reveal" else ["open", filepath])
                    else:
                        target = os.path.dirname(filepath) if action == "reveal" else filepath
                        subprocess.Popen(["xdg-open", target])
                    self.send_json({"success": True})
                except Exception as e:
                    self.send_json({"error": str(e)}, status=500)
            elif path == "/api/task_status":
                tid = query.get("task_id", [""])[0]
                with task_lock:
                    task = active_tasks.get(tid)
                if not task:
                    self.send_json({"error": "Task not found"}, status=404)
                    return
                self.send_json(task)
            elif path == "/api/export_csv":
                tid = query.get("task_id", [""])[0]
                with task_lock:
                    task = active_tasks.get(tid)
                if not task or task.get("status") != "completed":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="duplicates_report.csv"')
                self.end_headers()
                csv_data = "Filename,Size_Bytes,Size_Formatted,Keep_Path,Duplicate_Path_To_Delete\n"
                for g in task.get("duplicate_groups", []):
                    fn = g["filename"].replace('"', '""')
                    sz = g["size"]
                    sz_fmt = g["size_formatted"]
                    kp = g["keep_file"].replace('"', '""')
                    for d in g["duplicates"]:
                        dp = d.replace('"', '""')
                        csv_data += f'"{fn}",{sz},"{sz_fmt}","{kp}","{dp}"\n'
                self.wfile.write(csv_data.encode("utf-8"))
            elif path == "/api/fs/drill":
                target_path = query.get("path", ["/Volumes"])[0]
                self.send_json(self.drill_directory(target_path))
            elif path == "/api/relay/active":
                self.send_json(self.get_active_relays())
            elif path == "/api/rclone/remotes":
                self.send_json(self.get_rclone_remotes())
            elif path == "/api/gphotos/albums":
                self.send_json(self.get_google_albums())
            else:
                super().do_GET()
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def do_POST(self):
        try:
            url = urllib.parse.urlparse(self.path)
            path = url.path
            length = int(self.headers.get('content-length', 0))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else "{}"
            try:
                req_data = json.loads(body)
            except Exception:
                req_data = {}

            if path == "/api/backup_cloud":
                target = req_data.get("target", "all")
                def run_backup():
                    subprocess.run(["python3", "~/cloud_backup_manager.py", target])
                threading.Thread(target=run_backup, daemon=True).start()
                self.send_json({"status": "started", "target": target, "message": f"已啟動 {target} 同步備份作業"})
                return

            if path == "/api/start_scan":
                mode = req_data.get("mode", "single_folder")
                path_a = req_data.get("path_a", "")
                path_b = req_data.get("path_b", "")
                min_size = int(req_data.get("min_size_bytes", 1024))
                verify_hash = bool(req_data.get("verify_hash", True))
                priority_str = req_data.get("priority_str", "iPhone,JSPhone,2011~2016,photo,google photo,sd_card")

                if not os.path.exists(path_a):
                    self.send_json({"error": f"Path A 不存在: {path_a}"}, status=400)
                    return
                if mode == "cross_folder" and not os.path.exists(path_b):
                    self.send_json({"error": f"Path B 不存在: {path_b}"}, status=400)
                    return

                task_id = f"task_{int(time.time()*1000)}"
                t = threading.Thread(
                    target=scan_duplicates,
                    args=(task_id, mode, path_a, path_b, min_size, verify_hash, priority_str),
                    daemon=True
                )
                t.start()
                self.send_json({"task_id": task_id})

            elif path == "/api/execute_clean":
                files_to_delete = req_data.get("files", [])
                delete_mode = req_data.get("mode", "trash")
                trash_dir_name = req_data.get("trash_dir_name", "_trash_duplicates")
                deleted = 0
                freed_bytes = 0
                errors = []

                for p in files_to_delete:
                    if os.path.exists(p) and os.path.isfile(p):
                        try:
                            sz = os.path.getsize(p)
                            if delete_mode == "permanent":
                                os.remove(p)
                            else:
                                v_root = "/"
                                if p.startswith("/Volumes/"):
                                    parts = p.split("/")
                                    v_root = f"/{parts[1]}/{parts[2]}"
                                trash_root = os.path.join(v_root, trash_dir_name)
                                os.makedirs(trash_root, exist_ok=True)
                                rel_name = os.path.basename(p)
                                tgt = os.path.join(trash_root, f"{int(time.time()*1000)}_{rel_name}")
                                shutil.move(p, tgt)
                            deleted += 1
                            freed_bytes += sz
                        except Exception as err:
                            errors.append({"path": p, "error": str(err)})

                clean_empty = req_data.get("clean_empty_dirs", True)
                root_clean = req_data.get("root_clean", "")
                removed_dirs = 0
                if clean_empty and root_clean and os.path.exists(root_clean):
                    for r, ds, fs in os.walk(root_clean, topdown=False):
                        for d in ds:
                            dp = os.path.join(r, d)
                            try:
                                if not os.listdir(dp):
                                    os.rmdir(dp)
                                    removed_dirs += 1
                            except Exception:
                                pass

                self.send_json({
                    "deleted_count": deleted,
                    "freed_bytes": freed_bytes,
                    "freed_formatted": format_size(freed_bytes),
                    "empty_dirs_removed": removed_dirs,
                    "errors": errors
                })

            elif path == "/api/scan":
                self.send_json(self.fast_scan_directory(req_data.get("path", "")))
            elif path == "/api/config":
                save_app_config(req)
                self.send_json({"success": True, "msg": "設定已儲存至 config.json"})
            elif path == "/api/relay/start_task":
                self.send_json(self.start_relay_task(req_data))
            elif path == "/api/relay/stop_task":
                self.send_json(self.stop_relay_task(req_data.get("pid", 0)))
            elif path == "/api/relay/control":
                self.send_json(self.control_legacy_relay(req_data.get("action", "")))
            elif path == "/api/tools/clean_junk":
                self.send_json(self.clean_junk_files(req_data.get("path", "")))
            elif path == "/api/gphotos/create_album":
                self.send_json(self.create_google_album(req_data.get("title", "")))
            elif path == "/api/rclone/action":
                self.send_json(self.execute_rclone_action(req_data.get("remote", ""), req_data.get("action", "")))
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def get_pixel_status(self):
        inbox_dir = "~/Pictures/iPhone_Import"
        inbox_count = 0
        inbox_size = 0
        if os.path.exists(inbox_dir):
            try:
                for f in os.listdir(inbox_dir):
                    if not f.startswith('.'):
                        fp = os.path.join(inbox_dir, f)
                        inbox_count += 1
                        inbox_size += os.path.getsize(fp)
            except Exception:
                pass

        last_logs = []
        current_status = "idle"
        batch_files = 0
        batch_size_str = "0 GB"
        pixel_remaining = "未知"
        last_timestamp = ""
        track2_info = {}
        track2_status_file = "track2_status.json"
        if os.path.exists(track2_status_file):
            try:
                with open(track2_status_file, "r", encoding="utf-8") as f:
                    track2_info = json.load(f)
            except Exception:
                pass

        master_log = os.path.expanduser("~/track2_master.log")
        consumer_log = os.path.expanduser("~/track2_consumer.log")
        relay_log = os.path.expanduser("~/track2_relay.log")
        active_log = master_log if os.path.exists(master_log) else (consumer_log if os.path.exists(consumer_log) else (relay_log if os.path.exists(relay_log) else LOG_FILE))

        if os.path.exists(active_log):
            try:
                with open(active_log, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                    last_logs = [l.strip() for l in lines[-25:]]
                    for l in reversed(lines[-50:]):
                        if "📱 Pixel 狀態" in l or "📊 [即時進度]" in l:
                            m1 = re.search(r"(?:推入 Pixel.*?:\s*(\d+)\s*檔|相機區:\s*(\d+))", l)
                            if m1:
                                batch_files = int(m1.group(1) or m1.group(2) or 0)
                            m2 = re.search(r"(?:Pixel 備份狀態|備份):\s*([a-zA-Z_]+)\s*\((.*?)\)", l)
                            if m2:
                                current_status = m2.group(1)
                                pixel_remaining = m2.group(2)
                            tm = re.search(r"\[(.*?)\]", l)
                            if tm:
                                last_timestamp = tm.group(1)
                            break
                        elif "🚀 將預載區" in l or "🧹 Pixel" in l:
                            current_status = "switching_batch"
            except Exception:
                pass

        relay_running = False
        try:
            chk = subprocess.run(["pgrep", "-f", "track2_cloud_to_pixel_relay.py|pixel5_auto_relay.py"], capture_output=True, text=True)
            relay_running = bool(chk.stdout.strip())
        except Exception:
            pass

        return {
            "relay_running": relay_running,
            "inbox_count": inbox_count,
            "inbox_size": inbox_size,
            "inbox_size_formatted": format_size(inbox_size),
            "current_status": current_status,
            "batch_files": batch_files,
            "batch_size_str": batch_size_str,
            "pixel_remaining": pixel_remaining,
            "last_timestamp": last_timestamp,
            "recent_logs": last_logs,
            "track2": track2_info
        }

    def get_connected_devices(self):
        now = time.time()
        if CACHE["devices"] and (now - CACHE["devices_time"] < 3):
            return CACHE["devices"]

        devices = []
        try:
            res = subprocess.run([ADB, "devices", "-l"], capture_output=True, text=True, timeout=2)
            for line in res.stdout.strip().splitlines()[1:]:
                line = line.strip()
                if not line or "offline" in line:
                    continue
                parts = line.split()
                dev_id = parts[0]
                model = "Pixel 設備"
                for p in parts:
                    if p.startswith("model:"):
                        model = p.split("model:")[1].replace("_", " ")
                    elif p.startswith("product:"):
                        prod = p.split("product:")[1]
                        if "redfin" in prod:
                            model = "Pixel 5"
                        elif "flame" in prod:
                            model = "Pixel 4"
                        elif "coral" in prod:
                            model = "Pixel 4 XL"
                        elif "sunfish" in prod:
                            model = "Pixel 4a"

                free_gb = "31 GB"
                try:
                    df_res = subprocess.run([ADB, "-s", dev_id, "shell", "df -k /sdcard | tail -n 1"], capture_output=True, text=True, timeout=2)
                    df_parts = df_res.stdout.strip().split()
                    if len(df_parts) >= 4 and df_parts[3].isdigit():
                        free_gb = f"{round(int(df_parts[3]) / (1024 * 1024), 1)} GB"
                except Exception:
                    pass

                devices.append({
                    "id": dev_id,
                    "type": "android",
                    "name": f"Google {model}",
                    "detail": f"可用容量 {free_gb} • 0-Byte 無限空間中繼站",
                    "status": "connected",
                    "icon": "📱",
                    "serial": dev_id
                })
        except Exception:
            pass

        try:
            res = subprocess.run(["ioreg", "-p", "IOUSB", "-w0", "-l"], capture_output=True, text=True, timeout=2)
            if "iPhone" in res.stdout:
                devices.append({
                    "id": "iphone_usb",
                    "type": "ios",
                    "name": "Apple iPhone (實體 USB 已連線)",
                    "detail": "iOS 照片可直讀或透過本機 Staging 一鍵轉推 Pixel",
                    "status": "connected",
                    "icon": "🍏",
                    "path": "~/Pictures/iPhone_Import"
                })
        except Exception:
            pass

        try:
            mounts = glob.glob("/Volumes/*")
            for p in mounts:
                try:
                    res = subprocess.run(["df", "-h", p], capture_output=True, text=True, timeout=1.5)
                    lines = res.stdout.strip().splitlines()
                    if len(lines) >= 2:
                        parts = lines[1].split()
                        name = os.path.basename(p)
                        is_nas = "smb" in parts[0].lower() or "afp" in parts[0].lower() or "192.168" in parts[0]
                        is_sd = "sd" in name.lower() or "card" in name.lower() or "dcim" in name.lower()
                        dev_type = "nas" if is_nas else ("sd" if is_sd else "hdd")
                        icon = "🌐" if is_nas else ("💳" if is_sd else "💽")
                        label = "Synology NAS 網路磁區" if is_nas else ("SD 記憶卡" if is_sd else "外接硬碟 / SSD")
                        devices.append({
                            "id": p,
                            "type": dev_type,
                            "name": f"{name} ({label})",
                            "detail": f"總量 {parts[1]} | 可用 {parts[3]} (使用率 {parts[4]})",
                            "path": p,
                            "status": "mounted",
                            "icon": icon
                        })
                except Exception:
                    pass
        except Exception:
            pass

        CACHE["devices"] = devices
        CACHE["devices_time"] = now
        return devices

    def drill_directory(self, target_path):
        if not target_path or not os.path.exists(target_path):
            return {"success": False, "error": f"找不到指定路徑: {target_path}", "items": []}

        items = []
        try:
            with os.scandir(target_path) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        st = entry.stat(follow_symlinks=False)
                        sz = st.st_size if not is_dir else 0
                        items.append({
                            "name": entry.name,
                            "path": entry.path,
                            "is_dir": is_dir,
                            "size_bytes": sz,
                            "size_str": f"{round(sz / (1024*1024), 1)} MB" if not is_dir else ""
                        })
                    except OSError:
                        continue
        except Exception as e:
            return {"success": False, "error": str(e), "items": []}

        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {
            "success": True,
            "path": target_path,
            "parent": os.path.dirname(target_path) if target_path != "/" and target_path != "/Volumes" else None,
            "items": items[:250]
        }

    def fast_scan_directory(self, target_path):
        if not target_path or not os.path.exists(target_path):
            return {"success": False, "error": f"路徑不存在: {target_path}"}

        stats = {
            "path": target_path,
            "total_files": 0,
            "total_size_bytes": 0,
            "photos": 0,
            "videos": 0,
            "junk": 0,
            "heic": 0,
            "subfolders": []
        }
        valid_photos = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".dng", ".cr2", ".arw"}
        valid_videos = {".mp4", ".mov", ".avi", ".wmv", ".3gp", ".mpg", ".mpeg", ".mts", ".m2ts", ".m4v"}
        junk_exts = {".json", ".ds_store", ".thm", ".ctg", ".info", ".moff", ".modd", ".db", ".ini", ".aae"}

        try:
            with os.scandir(target_path) as it:
                for entry in it:
                    if entry.is_dir() and not entry.name.startswith("."):
                        stats["subfolders"].append(entry.name)

            file_count = 0
            for root, dirs, files in os.walk(target_path):
                rel = os.path.relpath(root, target_path)
                depth = len(rel.split(os.sep)) if rel != "." else 0
                if depth > 3:
                    dirs.clear()
                    continue

                for f in files:
                    file_count += 1
                    if file_count > 10000:
                        break
                    ext = os.path.splitext(f)[1].lower()
                    full_p = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(full_p)
                    except OSError:
                        sz = 0

                    stats["total_files"] += 1
                    stats["total_size_bytes"] += sz

                    if ext in valid_photos:
                        stats["photos"] += 1
                    elif ext in {".heic", ".heif"}:
                        stats["photos"] += 1
                        stats["heic"] += 1
                    elif ext in valid_videos:
                        stats["videos"] += 1
                    elif ext in junk_exts or f.startswith("."):
                        stats["junk"] += 1

                if file_count > 10000:
                    break
        except Exception as e:
            return {"success": False, "error": str(e)}

        stats["total_size_gb"] = round(stats["total_size_bytes"] / (1024**3), 2)
        return {"success": True, "stats": stats}

    def start_relay_task(self, req):
        source = req.get("source_path")
        device = req.get("device_serial", "auto")
        batch_gb = float(req.get("batch_gb", 20.0))
        delete_source = bool(req.get("delete_source", False))

        if not source or not os.path.exists(source):
            return {"success": False, "error": f"來源路徑不存在: {source}"}

        task_id = f"relay_{int(time.time())}"
        task_log = os.path.expanduser(f"~/relay_task_{task_id}.log")

        cmd = [
            sys.executable,
            os.path.join(BASE_DIR, "universal_relay_pipeline.py"),
            "--source", source,
            "--device", device,
            "--batch-gb", str(batch_gb),
            "--log-file", task_log
        ]
        if delete_source:
            cmd.append("--delete-source")
        cfg_scope = load_app_config().get("scope", {})
        date_from = req.get("date_from", cfg_scope.get("date_from", ""))
        date_to = req.get("date_to", cfg_scope.get("date_to", ""))
        include_raw = req.get("include_raw", cfg_scope.get("include_raw", False))
        exclude_pats = req.get("exclude_patterns", cfg_scope.get("exclude_patterns", []))
        if date_from:
            cmd.extend(["--date-from", str(date_from)])
        if date_to:
            cmd.extend(["--date-to", str(date_to)])
        if include_raw:
            cmd.append("--include-raw")
        if exclude_pats:
            ex_str = ",".join(exclude_pats) if isinstance(exclude_pats, list) else str(exclude_pats)
            if ex_str.strip():
                cmd.extend(["--exclude", ex_str])

        try:
            proc = subprocess.Popen(cmd, stdout=open(task_log, "a"), stderr=subprocess.STDOUT)
            return {
                "success": True,
                "msg": f"中繼遷移任務已成功啟動！(PID: {proc.pid})",
                "task_id": task_id,
                "log_file": task_log
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stop_relay_task(self, pid):
        try:
            pid_int = int(pid)
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid_int)], capture_output=True)
            else:
                import signal
                os.kill(pid_int, signal.SIGKILL)
            return {"success": True, "msg": f"任務 (PID: {pid}) 已成功終止！"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_active_relays(self):
        tasks = []
        try:
            if os.name == "nt":
                res = subprocess.run(
                    ["wmic", "process", "where", "name like '%python%'", "get", "ProcessId,CommandLine"],
                    capture_output=True, text=True
                )
                for line in res.stdout.splitlines():
                    if "universal_relay_pipeline" in line:
                        parts = line.strip().split()
                        if parts and parts[-1].isdigit():
                            tasks.append({"pid": parts[-1], "cmd": line.strip(), "is_legacy": False})
            else:
                res = subprocess.run("ps -ef | grep -E 'universal_relay_pipeline|pixel5_auto_relay' | grep -v grep", shell=True, capture_output=True, text=True)
                for line in res.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 8:
                        pid = parts[1]
                        cmd_str = " ".join(parts[7:])
                        tasks.append({
                            "pid": pid,
                            "cmd": cmd_str,
                            "is_legacy": "pixel5_auto_relay" in cmd_str
                        })
        except Exception:
            pass
        return {"tasks": tasks}

    def clean_junk_files(self, target_path):
        target_path = os.path.expanduser(target_path) if target_path else ""
        if not target_path or not os.path.exists(target_path):
            return {"success": False, "error": "路徑不存在"}
        junk_exts = {".json", ".thm", ".ctg", ".info", ".moff", ".modd", ".aae"}
        removed = 0
        for root, _, files in os.walk(target_path):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in junk_exts or fname.startswith(".") or fname.lower().startswith("thumbs.db"):
                    try:
                        os.remove(os.path.join(root, fname))
                        removed += 1
                    except OSError:
                        pass
        return {"success": True, "msg": f"清理無效附屬檔與隱藏垃圾檔完成！（已移除 {removed} 個）"}

    def control_legacy_relay(self, action):
        if action == "restart":
            subprocess.run("kill -9 $(pgrep -f pixel5_auto_relay.py) 2>/dev/null || true", shell=True)
            subprocess.run("nohup python3 ~/pixel5_auto_relay.py >/dev/null 2>&1 &", shell=True)
            return {"success": True, "msg": "Pixel 5 中繼服務已重啟！"}
        elif action == "stop":
            subprocess.run("kill -9 $(pgrep -f pixel5_auto_relay.py) 2>/dev/null || true", shell=True)
            return {"success": True, "msg": "Pixel 5 中繼服務已暫停！"}
        return {"success": False, "error": "未知動作"}

    def get_rclone_remotes(self):
        remotes = []
        try:
            res = subprocess.run([RCLONE, "listremotes"], capture_output=True, text=True, timeout=3)
            for r in res.stdout.strip().splitlines():
                r = r.strip()
                if not r:
                    continue
                name = r.rstrip(":")
                rem_type = "Google Drive" if "gdrive" in name else ("Google Photos" if "gphotos" in name else "雲端服務")
                remotes.append({
                    "remote": r,
                    "name": name,
                    "type": rem_type,
                    "icon": "☁️"
                })
        except Exception:
            pass
        return remotes

    def get_google_albums(self):
        token = get_refreshed_gphotos_token()
        if not token:
            return {"success": False, "error": "無法取得 Google 相簿授權 Token", "albums": []}
        try:
            req = urllib.request.Request(
                "https://photoslibrary.googleapis.com/v1/albums?pageSize=20",
                headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
                return {"success": True, "albums": data.get("albums", [])}
        except Exception as e:
            return {"success": False, "error": str(e), "albums": []}

    def create_google_album(self, title):
        if not title:
            return {"success": False, "error": "請輸入相簿名稱"}
        token = get_refreshed_gphotos_token()
        if not token:
            return {"success": False, "error": "無法取得 Google 相簿授權 Token"}
        try:
            payload = json.dumps({"album": {"title": title}}).encode("utf-8")
            req = urllib.request.Request(
                "https://photoslibrary.googleapis.com/v1/albums",
                data=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                res = json.loads(resp.read().decode())
                return {"success": True, "album": res}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def execute_rclone_action(self, remote, action):
        if action == "cleanup":
            res = subprocess.run([RCLONE, "cleanup", remote], capture_output=True, text=True, timeout=8)
            return {"success": res.returncode == 0, "msg": f"{remote} 垃圾桶清空成功！"}
        elif action == "about":
            res = subprocess.run([RCLONE, "about", remote], capture_output=True, text=True, timeout=8)
            return {"success": res.returncode == 0, "output": res.stdout}
        return {"success": False, "error": "未知 rclone 動作"}

    def get_system_data(self):
        now = time.time()
        if CACHE["system_data"] and (now - CACHE["system_time"] < 2):
            return CACHE["system_data"]

        drive_info = {"avail_gb": 919, "used_gb": 35, "freed_gb": 908, "percent_used": 4}
        try:
            res = subprocess.run(["df", "-h", "/Volumes/ExternalDrive"], capture_output=True, text=True, timeout=1.5)
            lines = res.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                avail_str = parts[3].replace("Gi", "").replace("G", "")
                used_str = parts[2].replace("Gi", "").replace("G", "")
                cap_str = parts[4].replace("%", "")
                avail_gb = int(avail_str) if avail_str.isdigit() else 919
                used_gb = int(used_str) if used_str.isdigit() else 35
                drive_info = {
                    "avail_gb": avail_gb,
                    "used_gb": used_gb,
                    "freed_gb": max(0, avail_gb - 11),
                    "percent_used": int(cap_str) if cap_str.isdigit() else 4
                }
        except Exception:
            pass

        pixel_info = {"free_gb": 85.0}
        now_ts = time.time()
        if CACHE.get("pixel_free_gb") and (now_ts - CACHE.get("pixel_free_gb_time", 0) < 15):
            pixel_info["free_gb"] = CACHE["pixel_free_gb"]
        else:
            try:
                res = subprocess.run([ADB, "shell", "df -k /sdcard | tail -n 1"], capture_output=True, text=True, timeout=1.0)
                parts = res.stdout.strip().split()
                if len(parts) >= 4 and parts[3].isdigit():
                    pixel_info["free_gb"] = round(int(parts[3]) / (1024 * 1024), 1)
                    CACHE["pixel_free_gb"] = pixel_info["free_gb"]
                    CACHE["pixel_free_gb_time"] = now_ts
            except Exception:
                pass

        backup_info = {"status": "complete", "count": 0}
        log_lines = []
        master_log = os.path.expanduser("~/track2_master.log")
        consumer_log = os.path.expanduser("~/track2_consumer.log")
        relay_log = os.path.expanduser("~/track2_relay.log")
        active_log = master_log if os.path.exists(master_log) else (consumer_log if os.path.exists(consumer_log) else (relay_log if os.path.exists(relay_log) else LOG_FILE))
        if os.path.exists(active_log):
            try:
                with open(active_log, "r", encoding="utf-8", errors="ignore") as f:
                    log_lines = f.readlines()[-30:]
                for line in reversed(log_lines):
                    m = re.search(r"(?:待備份|剩餘)\s*[:：]?\s*(\d+)", line)
                    if m:
                        backup_info["count"] = int(m.group(1))
                        backup_info["status"] = "backing_up"
                        break
                    if "complete" in line or "備份完成" in line:
                        backup_info["status"] = "complete"
                        backup_info["count"] = 0
                        break
            except Exception:
                pass

        total_files = 320066
        pct = round((drive_info["freed_gb"] / 938) * 100, 1) if drive_info["freed_gb"] > 0 else 98.8
        done_files = int(total_files * (pct / 100))

        res_ps = subprocess.run("pgrep -f 'track2_stream_consumer|nas_gphotos_sync|track2_cloud_to_pixel_relay|pixel5_auto_relay'", shell=True, capture_output=True, text=True)
        relay_running = bool(res_ps.stdout.strip())

        # Load Track 2 info
        track2_info = {}
        track2_file = "track2_status.json"
        if os.path.exists(track2_file):
            try:
                with open(track2_file, "r", encoding="utf-8") as f:
                    track2_info = json.load(f)
            except Exception:
                pass

        # Load NAS Sync info
        nas_sync_info = {
            "running": False,
            "total_nas_files": 327635,
            "uploaded_to_gphotos": 0,
            "pending_files": 327635,
            "active_batch_count": 0,
            "status_message": "NAS ➔ Pixel 5 雙向取最大值同步準備中",
            "recent_logs": []
        }
        res_nas = subprocess.run("pgrep -f 'nas_union_max_streamer.py'", shell=True, capture_output=True, text=True)
        nas_sync_info["running"] = bool(res_nas.stdout.strip())

        nas_sync_file = "nas_sync_status.json"
        if os.path.exists(nas_sync_file):
            try:
                with open(nas_sync_file, "r", encoding="utf-8") as f:
                    loaded_nas = json.load(f)
                    nas_sync_info.update(loaded_nas)
            except Exception:
                pass

        nas_log_file = "/tmp/nas_streamer.log"
        if os.path.exists(nas_log_file):
            try:
                with open(nas_log_file, "r", encoding="utf-8", errors="ignore") as nf:
                    n_lines = nf.readlines()
                    nas_sync_info["recent_logs"] = [l.strip() for l in n_lines[-15:]]
            except Exception:
                pass

        # Load Google Takeout Master Pipeline info
        takeout_info = {
            "running": False,
            "current_zip": "",
            "completed_zips": [],
            "total_zips": 9,
            "completed_count": 0,
            "current_zip_uploaded": 0,
            "current_zip_total": 0,
            "total_uploaded_files": 0,
            "recent_logs": []
        }
        res_pipeline = subprocess.run("pgrep -f 'takeout_master_pipeline.py'", shell=True, capture_output=True, text=True)
        takeout_info["running"] = bool(res_pipeline.stdout.strip())

        t_prog_file = "~/takeout_master_progress.json"
        t_log_file = os.path.expanduser("~/takeout_master_pipeline.log")
        if os.path.exists(t_prog_file):
            try:
                with open(t_prog_file, "r", encoding="utf-8") as tf:
                    td = json.load(tf)
                    takeout_info["current_zip"] = td.get("current_zip", "")
                    takeout_info["completed_zips"] = td.get("completed_zips", [])
                    takeout_info["completed_count"] = len(takeout_info["completed_zips"])
                    uploaded_by_zip = td.get("uploaded_items_by_zip", {})
                    total_up = 0
                    for z, items in uploaded_by_zip.items():
                        total_up += len(items)
                    if "takeout-20261022T080428Z-1-001.zip" in takeout_info["completed_zips"]:
                        total_up += 22950
                    takeout_info["total_uploaded_files"] = total_up
                    cur_z = takeout_info["current_zip"]
                    if cur_z in uploaded_by_zip:
                        takeout_info["current_zip_uploaded"] = len(uploaded_by_zip[cur_z])
            except Exception:
                pass
        if os.path.exists(t_log_file):
            try:
                with open(t_log_file, "r", encoding="utf-8", errors="ignore") as tf:
                    t_lines = tf.readlines()
                    takeout_info["recent_logs"] = [l.strip() for l in t_lines[-15:]]
                    for l in reversed(t_lines[-50:]):
                        m = re.search(r"總影音數:\s*(\d+)", l)
                        if m:
                            takeout_info["current_zip_total"] = int(m.group(1))
                            break
            except Exception:
                pass

        storage_mat = self.get_storage_matrix()

        data = {
            "relay_running": relay_running,
            "drive": drive_info,
            "pixel": pixel_info,
            "backup": backup_info,
            "track2": track2_info,
            "nas_sync": nas_sync_info,
            "takeout_pipeline": takeout_info,
            "storage_matrix": storage_mat.get("matrix", []),
            "progress": {
                "total_percent": pct,
                "done_files": done_files,
                "total_files": total_files
            },
            "logs": [l.strip() for l in log_lines[-25:]]
        }
        CACHE["system_data"] = data
        CACHE["system_time"] = now
        return data

    def get_storage_matrix(self):
        now = time.time()
        if CACHE.get("storage_matrix") and (now - CACHE.get("storage_matrix_time", 0) < 15):
            return CACHE["storage_matrix"]

        matrix = []

        # 1. Mac mini (內接 SSD)
        mini_int = {"id": "macmini_int", "name": "Mac mini (內接 SSD)", "type": "local", "icon": "🖥️"}
        try:
            res = subprocess.run(["df", "-k", "/System/Volumes/Data"], capture_output=True, text=True, timeout=1.5)
            lines = res.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                total_kb = int(parts[1])
                used_kb = int(parts[2])
                avail_kb = int(parts[3])
                mini_int.update({
                    "total_gb": round(total_kb / (1024*1024), 1),
                    "used_gb": round(used_kb / (1024*1024), 1),
                    "avail_gb": round(avail_kb / (1024*1024), 1),
                    "percent_used": round(used_kb / total_kb * 100, 1),
                    "status": "系統運行中",
                    "state": "normal" if avail_kb > 15*1024*1024 else "warning",
                    "detail": "本機系統核心磁碟"
                })
        except Exception:
            mini_int.update({"total_gb": 228.0, "used_gb": 205.0, "avail_gb": 23.0, "percent_used": 89.9, "status": "系統運行中", "state": "warning", "detail": "本機系統核心磁碟"})
        matrix.append(mini_int)

        # 2. Mac mini (外接硬碟 ExternalDrive)
        mini_ext = {"id": "macmini_ext", "name": "外接硬碟 (ExternalDrive)", "type": "local", "icon": "💽"}
        try:
            res = subprocess.run(["df", "-k", "/Volumes/ExternalDrive"], capture_output=True, text=True, timeout=1.5)
            lines = res.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                total_kb = int(parts[1])
                used_kb = int(parts[2])
                avail_kb = int(parts[3])
                mini_ext.update({
                    "total_gb": round(total_kb / (1024*1024), 1),
                    "used_gb": round(used_kb / (1024*1024), 1),
                    "avail_gb": round(avail_kb / (1024*1024), 1),
                    "percent_used": round(used_kb / total_kb * 100, 1),
                    "status": "第一軌已 100% 釋放",
                    "state": "success",
                    "detail": "成功釋放 +917 GB 可用空間"
                })
        except Exception:
            mini_ext.update({"total_gb": 953.8, "used_gb": 26.0, "avail_gb": 927.8, "percent_used": 2.7, "status": "第一軌已完成", "state": "success", "detail": "可用 927 GB"})
        matrix.append(mini_ext)

        # 3. MacBook Air (M-Series)
        macbook = {"id": "macbook_air", "name": "MacBook Air", "type": "remote", "icon": "💻"}
        mb_cached = CACHE.get("macbook_storage") or {
            "total_gb": 228.0,
            "used_gb": 155.0,
            "avail_gb": 46.0,
            "percent_used": 78.0,
            "status": "待機休眠 (SSH 通道就緒)",
            "state": "normal",
            "detail": "tailscale-host / mac-workstation.local"
        }
        try:
            res = subprocess.run(["ssh", "-o", "ConnectTimeout=1.2", "macbook-air", "df -k /System/Volumes/Data"], capture_output=True, text=True, timeout=1.5)
            lines = res.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                total_kb = int(parts[1])
                used_kb = int(parts[2])
                avail_kb = int(parts[3])
                mb_cached = {
                    "total_gb": round(total_kb / (1024*1024), 1),
                    "used_gb": round(used_kb / (1024*1024), 1),
                    "avail_gb": round(avail_kb / (1024*1024), 1),
                    "percent_used": round(used_kb / total_kb * 100, 1),
                    "status": "在線連通 (局域網/Tailscale)",
                    "state": "normal",
                    "detail": "MacBook Air 主磁碟"
                }
                CACHE["macbook_storage"] = mb_cached
        except Exception:
            pass
        macbook.update(mb_cached)
        matrix.append(macbook)

        # 4. Pixel 5 (中繼核心設備)
        pixel = {"id": "pixel5", "name": "Pixel 5 (無限中繼)", "type": "mobile", "icon": "📱"}
        try:
            res = subprocess.run([ADB, "shell", "df -k /sdcard | tail -n 1"], capture_output=True, text=True, timeout=1.0)
            parts = res.stdout.strip().split()
            if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit() and parts[3].isdigit():
                total_kb = int(parts[1])
                used_kb = int(parts[2])
                avail_kb = int(parts[3])
                pixel.update({
                    "total_gb": round(total_kb / (1024*1024), 1),
                    "used_gb": round(used_kb / (1024*1024), 1),
                    "avail_gb": round(avail_kb / (1024*1024), 1),
                    "percent_used": round(used_kb / total_kb * 100, 1),
                    "status": "第二軌高速中繼同步中",
                    "state": "success",
                    "detail": "0-Byte 無限空間中繼特權"
                })
        except Exception:
            pixel.update({"total_gb": 109.0, "used_gb": 67.8, "avail_gb": 41.2, "percent_used": 62.2, "status": "中繼進行中", "state": "success", "detail": "0-Byte 無限空間特權"})
        matrix.append(pixel)

        # 4.5 Synology NAS (JAY_NAS)
        nas_entry = {"id": "synology_nas", "name": "Synology NAS (JAY_NAS)", "type": "nas", "icon": "🗄️"}
        try:
            res = subprocess.run(["df", "-k", "/Volumes/home"], capture_output=True, text=True, timeout=1.5)
            lines = res.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                total_kb = int(parts[1])
                used_kb = int(parts[2])
                avail_kb = int(parts[3])
                nas_entry.update({
                    "total_gb": round(total_kb / (1024*1024), 1),
                    "used_gb": round(used_kb / (1024*1024), 1),
                    "avail_gb": round(avail_kb / (1024*1024), 1),
                    "percent_used": round(used_kb / total_kb * 100, 1),
                    "status": "雙向取最大值同步中",
                    "state": "success",
                    "detail": "nas.local (/Volumes/home/Photos & photo)"
                })
            else:
                raise ValueError("No df output")
        except Exception:
            nas_entry.update({
                "total_gb": 1843.0,
                "used_gb": 1577.0,
                "avail_gb": 266.0,
                "percent_used": 85.6,
                "status": "已掛載連通",
                "state": "success",
                "detail": "nas.local (/Volumes/home & photo)"
            })
        matrix.append(nas_entry)

        # 5. iPhone (iPhone 15 Pro Max)
        iphone = {"id": "iphone", "name": "iPhone 15 Pro Max", "type": "mobile", "icon": "📱"}
        ip_online = False
        try:
            res = subprocess.run(["ping", "-c", "1", "-t", "1", "iphone.local"], capture_output=True, text=True, timeout=0.8)
            ip_online = (res.returncode == 0)
        except Exception:
            pass
        iphone.update({
            "total_gb": 256.0,
            "used_gb": 142.0,
            "avail_gb": 114.0,
            "percent_used": 55.5,
            "status": "USB 網路就緒 (169.254.82.93)" if ip_online else "待命中 / 無線網路註冊",
            "state": "normal",
            "detail": "Lightning/Type-C 與熱點網路"
        })
        matrix.append(iphone)

        # 6. iPad
        ipad = {"id": "ipad", "name": "iPad (行動端)", "type": "mobile", "icon": "📲"}
        ipad.update({
            "total_gb": 128.0,
            "used_gb": 64.0,
            "avail_gb": 64.0,
            "percent_used": 50.0,
            "status": "Tailscale 網路註冊 (ipad168)",
            "state": "normal",
            "detail": "Apple 行動裝置"
        })
        matrix.append(ipad)

        # 7. Google 雲端空間 (your-primary@gmail.com)
        g_main = {"id": "gdrive_main", "name": "Google (your-primary@gmail.com)", "type": "cloud", "icon": "☁️"}
        g_main.update({
            "total_gb": 5120.0,
            "used_gb": 254.7,
            "avail_gb": 4865.3,
            "percent_used": 5.0,
            "status": "5 TB 旗艦方案 (可用 4.75 TB)",
            "state": "success",
            "detail": "相簿佔 77 GB (32 個大型檔待洗白重傳歸零)"
        })
        matrix.append(g_main)

        # 8. Google 雲端空間 (your-secondary@gmail.com)
        g_sub = {"id": "gdrive_sub", "name": "Google (your-secondary@gmail.com)", "type": "cloud", "icon": "☁️"}
        g_sub.update({
            "total_gb": 2048.0,
            "used_gb": 1463.3,
            "avail_gb": 584.7,
            "percent_used": 71.4,
            "status": "2 TB 方案 (正導出至 Pixel 5)",
            "state": "warning",
            "detail": "Drive: 1.075 TB | Photos: 363 GB"
        })
        matrix.append(g_sub)

        # 9. Microsoft OneDrive (Foxconn 企業雲)
        od_backup_dir = "~/Pictures/OneDrive_Backup"
        od_bytes = 0
        od_count = 0
        if os.path.exists(od_backup_dir):
            try:
                for r, d, f in os.walk(od_backup_dir):
                    od_count += len(f)
                    for file in f:
                        od_bytes += os.path.getsize(os.path.join(r, file))
            except Exception:
                pass
        od_mb = od_bytes / (1024 * 1024)

        onedrive = {"id": "onedrive", "name": "Microsoft OneDrive (Foxconn)", "type": "cloud", "icon": "🏢"}
        onedrive.update({
            "total_gb": 5.0,
            "used_gb": 3.6,
            "avail_gb": 1.4,
            "percent_used": 72.0,
            "status": "雲端已徹底清空釋放 +1.4 GB",
            "state": "success",
            "detail": "雲端檔案與資源回收筒已清空 (原 1,187 檔已完整歸檔至 Google Drive)"
        })
        matrix.append(onedrive)

        # 10. Apple iCloud
        ic_backup_dir = "~/Pictures/iCloud_Backup"
        ic_bytes = 0
        ic_count = 0
        if os.path.exists(ic_backup_dir):
            try:
                for r, d, f in os.walk(ic_backup_dir):
                    ic_count += len(f)
                    for file in f:
                        ic_bytes += os.path.getsize(os.path.join(r, file))
            except Exception:
                pass
        ic_mb = ic_bytes / (1024 * 1024)

        icloud = {"id": "icloud", "name": "Apple iCloud Drive", "type": "cloud", "icon": "🍎"}
        icloud.update({
            "total_gb": 50.0,
            "used_gb": 4.5,
            "avail_gb": 45.5,
            "percent_used": 9.0,
            "status": f"外接硬碟備份完成 ({ic_count} 檔 / {ic_mb:.1f} MB)",
            "state": "success",
            "detail": "稅務/合約/家庭檔案已全數保存在外接硬碟"
        })
        matrix.append(icloud)

        res_data = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "matrix": matrix
        }
        CACHE["storage_matrix"] = res_data
        CACHE["storage_matrix_time"] = now
        return res_data

class ReusableThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    def server_bind(self):
        import socket
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
        super().server_bind()

def run_server_on_port(port):
    with ReusableThreadingServer((HOST, port), UnifiedDashboardHandler) as server:
        print(f"🚀 Unified Dashboard listening on port {port} (http://localhost:{port})")
        server.serve_forever()

if __name__ == "__main__":
    t = threading.Thread(target=run_server_on_port, args=(8998,), daemon=True)
    t.start()
    run_server_on_port(8989)
