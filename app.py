#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local Web Dashboard & REST API Server for Pixel 4 Google Photos Relay.
Zero external pip dependencies (Python 3.9+ standard library only).
"""

import argparse
import collections
import json
import os
import shutil
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, Optional
from urllib.parse import parse_qs, urlparse

from relay_engine import PixelRelayEngine, SyncDatabase, load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")
DB_PATH = os.path.join(BASE_DIR, "relay_history.db")
LOG_PATH = os.path.join(BASE_DIR, "relay.log")
WEB_INDEX = os.path.join(BASE_DIR, "web", "index.html")


class RelayState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.engine: Optional[PixelRelayEngine] = None
        self.last_result: Optional[Dict[str, Any]] = None
        self.recent_logs: Deque[str] = collections.deque(maxlen=250)
        self._load_existing_logs()

    def _load_existing_logs(self) -> None:
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    for line in f.readlines()[-200:]:
                        self.recent_logs.append(line.rstrip("\n"))
            except OSError:
                pass

    def append_log(self, line: str) -> None:
        with self.lock:
            self.recent_logs.append(line)


STATE = RelayState()


def ensure_config_exists() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH) and os.path.exists(EXAMPLE_CONFIG_PATH):
        shutil.copy2(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
    return load_config(CONFIG_PATH)


def start_relay_job() -> Dict[str, Any]:
    with STATE.lock:
        if STATE.running:
            return {"ok": False, "message": "任務已在執行中"}
        cfg = ensure_config_exists()
        engine = PixelRelayEngine(
            cfg,
            db_path=DB_PATH,
            log_file=LOG_PATH,
            log_callback=STATE.append_log,
        )
        STATE.engine = engine
        STATE.running = True
        STATE.last_result = None

        def worker() -> None:
            try:
                res = engine.run()
                STATE.last_result = res
            except Exception as e:
                STATE.append_log(f"❌ 執行發生未預期錯誤: {e}")
                STATE.last_result = {"status": "error", "error": str(e)}
            finally:
                with STATE.lock:
                    STATE.running = False

        t = threading.Thread(target=worker, daemon=True)
        STATE.thread = t
        t.start()
        return {"ok": True, "message": "已啟動 Pixel 中繼任務"}


def stop_relay_job() -> Dict[str, Any]:
    with STATE.lock:
        if not STATE.running or not STATE.engine:
            return {"ok": False, "message": "目前沒有執行中的任務"}
        STATE.engine.stop_event.set()
        return {"ok": True, "message": "已發送停止訊號，將在目前檔案完成後安全中止"}


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return  # Keep console clean

    def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not os.path.exists(WEB_INDEX):
                self.send_error(HTTPStatus.NOT_FOUND, "web/index.html not found")
                return
            with open(WEB_INDEX, "rb") as f:
                content = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/api/config":
            self._send_json(ensure_config_exists())
            return

        if path == "/api/status":
            cfg = ensure_config_exists()
            engine = PixelRelayEngine(cfg, db_path=DB_PATH, log_file="")
            device_info = engine.inspect_device()
            acct = cfg.get("account", {}).get("google_account_label", "default")
            stats = SyncDatabase(DB_PATH).get_stats(acct)
            self._send_json(
                {
                    "running": STATE.running,
                    "last_result": STATE.last_result,
                    "device": device_info,
                    "stats": stats,
                    "account_label": acct,
                }
            )
            return

        if path == "/api/logs":
            with STATE.lock:
                logs = list(STATE.recent_logs)
            self._send_json({"logs": logs})
            return

        if path == "/api/history":
            qs = parse_qs(parsed.query)
            acct = qs.get("account", [None])[0]
            db = SyncDatabase(DB_PATH)
            self._send_json({"items": db.get_history(acct, limit=80), "stats": db.get_stats(acct)})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            try:
                new_cfg = self._read_json_body()
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(new_cfg, f, indent=2, ensure_ascii=False)
                self._send_json({"ok": True, "message": "設定已儲存至 config.json"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/scan":
            cfg = ensure_config_exists()
            engine = PixelRelayEngine(cfg, db_path=DB_PATH, log_file="")
            scan = engine.scan_pending_files(limit_preview=40)
            scan_out = {k: v for k, v in scan.items() if k != "files"}
            self._send_json(scan_out)
            return

        if path == "/api/start":
            self._send_json(start_relay_job())
            return

        if path == "/api/stop":
            self._send_json(stop_relay_job())
            return

        if path == "/api/history/clear":
            body = self._read_json_body()
            acct = body.get("account_label")
            removed = SyncDatabase(DB_PATH).clear_history(acct)
            self._send_json({"ok": True, "removed": removed})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pixel 4 Google Photos Relay Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (use 0.0.0.0 for LAN access)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    args = parser.parse_args()

    ensure_config_exists()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port}"
    print(f"Pixel 4 Google Photos Relay Dashboard running at {url}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
        server.shutdown()


if __name__ == "__main__":
    main()
