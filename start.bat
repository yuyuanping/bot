@echo off
title QQ Bot Saver
set NAPCAT_DIR=D:\tool\NapCat.Shell.Windows.OneKey\NapCat.44498.Shell
set NAPCAT_EXE=NapCatWinBootMain.exe
set PY=D:\Python311\python.exe
set PYW=D:\Python311\pythonw.exe
set LOG=D:\QQBot_Data\startup.log

echo [%time%] start.bat begin >> %LOG%

tasklist.exe | findstr /I "%NAPCAT_EXE%" >nul
if errorlevel 1 (
    echo [%time%] NapCat not running, starting hidden >> %LOG%
    rem 隐藏启动：需要扫码/看 NapCat 输出时，手动运行 %NAPCAT_DIR%\napcat.bat（可见窗口）
    powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -WorkingDirectory '%NAPCAT_DIR%' -FilePath '%NAPCAT_DIR%\%NAPCAT_EXE%'"
    echo [%time%] waiting for NapCat WS port 9501 to listen >> %LOG%
    rem 轮询 9501 监听状态（最多 90 秒）：就绪即走，比固定睡 25 秒又快又稳
    for /l %%i in (1,1,45) do (
        netstat -ano | findstr ":9501" | findstr "LISTENING" >nul && goto :ws_up
        ping -n 3 127.0.0.1 >nul
    )
    echo [%time%] WARN port 9501 not listening after 90s, starting bot anyway >> %LOG%
) else (
    echo [%time%] NapCat already running, skip >> %LOG%
)
:ws_up
ping -n 3 127.0.0.1 >nul

"%PY%" -c "import aiohttp, openpyxl" 2>nul
if errorlevel 1 (
    echo [%time%] installing deps aiohttp/openpyxl >> %LOG%
    "%PY%" -m pip install --disable-pip-version-check aiohttp openpyxl >nul 2>&1
)

rem 查杀旧 bot：tasklist /FO LIST 不显示命令行，旧 findstr 匹配不到，
rem 导致每次重开都叠加进程。改用 PowerShell 按 CommandLine 精确匹配。
rem 无控制台运行用的是 pythonw.exe，两个名字都要匹配。
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object {$_.CommandLine -like '*bot.py*'} | ForEach-Object { Write-Host ('kill old bot ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }" >> %LOG% 2>&1

echo [%time%] starting bot.py (pythonw, no window) >> %LOG%
start "" "%PYW%" "%~dp0bot.py"

rem 没有窗口就没有崩溃回显，3 秒后确认 bot 进程存活；其它 pythonw 进程也在跑，必须按命令行匹配
ping -n 4 127.0.0.1 >nul
powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object {$_.CommandLine -like '*bot.py*'})) { Write-Host 'WARN no bot.py alive after launch' }" >> %LOG% 2>&1
echo [%time%] done >> %LOG%
