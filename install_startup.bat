@echo off
echo === Bose Preset Bridge — Demarrage automatique avec Windows ===
echo.
echo Ce script ajoute BosePresetBridge.exe au demarrage de Windows.
echo A lancer UNE SEULE FOIS depuis le dossier contenant le .exe
echo.

cd /d "%~dp0"

set EXE=%~dp0BosePresetBridge.exe
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT=%STARTUP%\BosePresetBridge.lnk

if not exist "%EXE%" (
    echo ERREUR : BosePresetBridge.exe introuvable ici.
    pause
    exit /b 1
)

powershell -NoProfile -Command ^
  "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SHORTCUT%');" ^
  "$s.TargetPath='%EXE%';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.Save()"

echo OK — BosePresetBridge demarrera automatiquement a l'ouverture de session.
echo Raccourci cree dans : %STARTUP%
echo.
pause
