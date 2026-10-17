@echo off
chcp 65001 >nul
title BOSS Dashboard
cd /d "%~dp0"

rem ===== AI 配置 =====
rem 不要把密钥写进本文件。推荐做法：在同目录新建 secrets.env（已被 .gitignore 忽略），
rem 内容形如：
rem   BDS_AI_BASE_URL=https://your-relay.example.com/v1
rem   BDS_AI_API_KEY=你的密钥
rem   BDS_AI_MODEL=你的模型名
rem 本脚本会自动加载。也可以改用系统环境变量。
if exist "secrets.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in ("secrets.env") do (
    if not "%%a"=="" set "%%a=%%b"
  )
)
rem ====================

echo.
echo   BOSS Dashboard  -^>  http://127.0.0.1:8765
if defined BDS_AI_MODEL (echo   AI: %BDS_AI_MODEL%) else (echo   AI: 未配置，仅岗位看板可用)
echo   Close this window to stop.
echo.

start "" http://127.0.0.1:8765
python server.py

pause
