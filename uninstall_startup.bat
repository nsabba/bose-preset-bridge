@echo off
echo === Bose Preset Bridge - Suppression du demarrage automatique ===
echo.

rem install_startup.bat cree un raccourci dans le dossier Demarrage
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\BosePresetBridge.lnk" >nul 2>&1
rem ancienne methode (cle Run), au cas ou
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "BosePresetBridge" /f >nul 2>&1

echo OK - BosePresetBridge retire du demarrage automatique.
pause
