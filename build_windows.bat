@echo off
echo === Bose Preset Bridge — Build .exe standalone ===
echo.
echo Ce script tourne sur un PC avec Python installe.
echo Le .exe produit ne necessite AUCUNE installation sur le PC cible.
echo.

cd /d "%~dp0"

echo [1/3] Installation des dependances de build...
pip install -r requirements.txt --quiet
pip install pyinstaller pystray pillow --quiet

echo [2/3] Compilation en .exe standalone (inclut Python + toutes les libs)...
pyinstaller ^
  --onefile ^
  --noconsole ^
  --name BosePresetBridge ^
  bridge.py

echo [3/3] Copie de config.ini a cote du .exe...
copy /Y config.ini dist\config.ini

echo.
echo === Termine ===
echo.
echo Contenu du dossier dist\ a livrer au PC cible :
echo   BosePresetBridge.exe   <- double-cliquer pour lancer
echo   config.ini             <- ouvrir avec Notepad pour changer l'IP
echo.
echo Sur le PC cible (sans Python) :
echo   1. Copier ces 2 fichiers dans un dossier (ex: C:\BoseBridge\)
echo   2. Editer config.ini avec Notepad : changer bose_host si necessaire
echo   3. Double-cliquer BosePresetBridge.exe
echo   4. Une icone apparait dans la barre des taches
echo.
echo Pour demarrage automatique avec Windows :
echo   Copier un raccourci de BosePresetBridge.exe dans :
echo   %%APPDATA%%\Microsoft\Windows\Start Menu\Programs\Startup
echo.
pause
