@echo off
REM 爱国者屏冷启动录制：断电重启后，右键「以管理员身份运行」本文件。
REM 它会用 --inspect-brk 启动官方 AIGO，先把 node-hid 钩子打好再放行，
REM 录下从打开设备到第一帧的全部字节 → D:\Code\lcdmuse-verify\hid_boot.log
setlocal
set PY=D:\Code\lcd-venv\Scripts\python.exe
set HOOK=D:\Code\lcdmuse-driver-aigo\hook_brk.py

taskkill /IM AIGO.exe /F >nul 2>&1
timeout /t 2 >nul
start "" "C:\Program Files\AIGO\AIGO.exe" --inspect-brk=9229
"%PY%" "%HOOK%" --seconds 60
echo.
echo 录完了：D:\Code\lcdmuse-verify\hid_boot.log
pause
