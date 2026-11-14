@echo off
chcp 65001 >nul
echo ========================================================
echo   Pixel 4 / 5 無限 Google 相簿中繼控制台 (Windows)
echo ========================================================
cd /d "%~dp0"
if not exist config.json (
    copy config.example.json config.json >nul
    echo [Info] 已自動建立 config.json，請於網頁右上角設定帳號與路徑。
)
start "" "http://localhost:8989"
python server.py
pause
