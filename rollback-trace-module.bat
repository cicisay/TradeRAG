@echo off
chcp 65001 >nul
setlocal

rem ===================================================================
rem  链路追踪模块（11-3）一键回退
rem  恢复 4 个被修改的文件，并删除新增的 traces.py。
rem  快照由改动前的备份生成，改完不满意直接双击本文件即可。
rem ===================================================================

set SNAP=%~dp0.snapshot\trace-module-20261117-093237

if not exist "%SNAP%" (
  echo [错误] 找不到快照目录：%SNAP%
  echo 可能已被删除，无法自动回退。
  pause
  exit /b 1
)

echo.
echo  即将从快照恢复以下文件：
echo    frontend\admin.html
echo    frontend\assets\admin.js
echo    frontend\assets\app.css
echo    backend\app\main.py
echo  并删除 backend\app\api\traces.py
echo.
set /p OK=确认回退？(Y/N):
if /i not "%OK%"=="Y" (
  echo 已取消。
  pause
  exit /b 0
)

echo.
echo [1/5] 恢复 admin.html ...
copy /y "%SNAP%\frontend\admin.html" "%~dp0frontend\admin.html" >nul
echo [2/5] 恢复 admin.js ...
copy /y "%SNAP%\frontend\assets\admin.js" "%~dp0frontend\assets\admin.js" >nul
echo [3/5] 恢复 app.css ...
copy /y "%SNAP%\frontend\assets\app.css" "%~dp0frontend\assets\app.css" >nul
echo [4/5] 恢复 main.py ...
copy /y "%SNAP%\backend\app\main.py" "%~dp0backend\app\main.py" >nul
echo [5/5] 删除 traces.py ...
if exist "%~dp0backend\app\api\traces.py" del /q "%~dp0backend\app\api\traces.py"

echo.
echo  已回退到改动前的状态。
echo  还需要重建并重启 api 容器才会生效：
echo    docker build -t traderag-api:latest backend
echo    docker compose --profile obs up -d --scale api=1
echo.
pause
