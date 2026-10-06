@echo off
cd /d "%~dp0"
python hellbender_viewer_v2.pyw --demo
if errorlevel 1 pause
