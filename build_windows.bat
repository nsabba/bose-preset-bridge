@echo off
echo === Bose Preset Bridge - Build .exe standalone ===
echo.
echo Ce script tourne sur un PC avec Python installe.
echo Le .exe produit ne necessite AUCUNE installation sur le PC cible.
echo (Le meme build est fait automatiquement par GitHub Actions a chaque push.)
echo.

cd /d "%~dp0"

echo [1/4] Installation des dependances de build...
pip install -r requirements.txt --quiet
pip install pyinstaller pystray pillow --quiet

echo [2/4] Compilation de BosePresetBridge.exe (page web et catalogue embarques)...
pyinstaller ^
  --onefile ^
  --noconsole ^
  --name BosePresetBridge ^
  --add-data "web/index.html;web" ^
  --add-data "catalog/radios.json;catalog" ^
  bridge.py

echo [3/4] Compilation de ProbePresets.exe (outil de test, console)...
pyinstaller --onefile --console --name ProbePresets probe_presets.py

echo [4/4] Copie des fichiers annexes...
copy /Y config.ini dist\config.example.ini
copy /Y install_startup.bat dist\
copy /Y uninstall_startup.bat dist\
copy /Y install_firewall.bat dist\

echo.
echo === Termine ===
echo.
echo Contenu du dossier dist\ :
echo   BosePresetBridge.exe   ^<- le bridge (icone dans la barre des taches)
echo   ProbePresets.exe       ^<- outil de diagnostic des presets
echo   config.example.ini     ^<- a renommer en config.ini pour une NOUVELLE installation
echo.
echo Mise a jour d'une installation existante : remplacer uniquement BosePresetBridge.exe,
echo NE PAS ecraser le config.ini deja en place.
echo.
pause
