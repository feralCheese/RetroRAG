@echo off
rem Command-line access, e.g.:  retrorag-cli search "SID filter" --system C64
rem                             retrorag-cli api            (REST + MCP HTTP servers, no GUI)
setlocal
set "PYTHONPATH=%~dp0."
"%~dp0.venv\Scripts\python.exe" -m retrorag %*
