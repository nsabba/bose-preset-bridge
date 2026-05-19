@echo off
echo === Bose Preset Bridge — Suppression du demarrage automatique ===
echo.

reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "BosePresetBridge" /f

echo OK — BosePresetBridge retire du demarrage automatique.
pause
