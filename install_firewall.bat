@echo off
rem === Bose Preset Bridge - autorise la page de reglages (port 8888) sur le reseau prive ===
rem A lancer une fois depuis le dossier contenant BosePresetBridge.exe (demande les droits admin).
rem Supprime d'abord toutes les regles de l'exe : si la fenetre du pare-feu a ete fermee au
rem premier lancement, Windows a cree des regles de BLOCAGE, qui passent avant les autorisations.

net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set EXE=%~dp0BosePresetBridge.exe
if not exist "%EXE%" (
    echo ERREUR : BosePresetBridge.exe introuvable dans %~dp0
    pause
    exit /b 1
)

netsh advfirewall firewall delete rule name=all program="%EXE%" >nul 2>&1
netsh advfirewall firewall add rule name="BosePresetBridge" dir=in action=allow program="%EXE%" enable=yes profile=private,domain
echo.
echo OK - regle "BosePresetBridge" creee pour :
echo   %EXE%
echo.
echo Si la page n'est toujours pas accessible depuis le telephone, verifier que le reseau
echo Wi-Fi/Ethernet du PC est en profil "Prive" (Parametres ^> Reseau ^> Proprietes).
pause
