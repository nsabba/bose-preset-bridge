# bose-preset-bridge

Service Python léger qui intercepte les appuis sur les boutons preset d'une enceinte **Bose SoundTouch** et joue les flux configurés via **UPnP AVTransport**, entièrement en local, sans dépendance au cloud Bose (arrêté en mai 2026).

Version 2 (stabilité) :

- **presets réécrits dans l'enceinte** : plus aucun appel au cloud Bose quand on appuie sur un bouton ;
- **watchdog de lecture** : vérifie que le son démarre vraiment, relance, et redémarre l'enceinte si son moteur de lecture est bloqué ;
- **page web** (téléphone ou PC) pour choisir les 6 radios dans un catalogue, sans toucher aux fichiers ;
- anti-rebond, reconnexion plus rapide, logs lisibles sous Windows.

Le détail de chaque requête (enceinte et API) est dans [PROTOCOL.md](PROTOCOL.md).

---

## Diagnostic (septembre 2026)

Constaté sur place le 23/09/2026 (SoundTouch 20, firmware 27.0.6) puis vérifié le 24/09 sur une deuxième enceinte :

1. **Les presets stockés dans l'enceinte pointaient vers le cloud Bose** (`content.api.bose.io/...orion/station` ou `TUNEIN /v1/playback/station/...`). Appuyer sur un preset faisait tenter à l'enceinte la lecture de ce contenu mort.
2. Le preset 1 marchait seulement parce que le bridge envoyait l'UPnP dans la même seconde et gagnait la course. Pour d'autres presets, l'enceinte partait vers le cloud et **son moteur de lecture se bloquait** : ensuite l'UPnP répondait `200` mais rien ne jouait (`/now_playing` = `INVALID_SOURCE`) jusqu'au redémarrage de l'enceinte.
3. `/sources` affiche `UPNP UNAVAILABLE` même quand l'UPnP marche : ne pas s'y fier. **Seul `/now_playing` (`source="UPNP"` + `PLAY_STATE`) fait foi.**
4. **L'endpoint non documenté `POST /storePreset` fonctionne** (testé sur le preset 5) : on peut écrire dans un preset un contenu `UPNP` pointant sur le flux. À l'appui, l'enceinte ne joue pas seule, mais elle envoie l'événement au bridge, ne contacte plus le cloud et ne se bloque plus ; le bridge lance alors la lecture.
5. L'ancienne fonction « Configurer les presets » (lecture UPnP + appui long) ne marchait pas : les contenus UPnP en cours de lecture ne sont pas enregistrables (`isPresetable="false"`). Elle est remplacée par `storePreset`.
6. Redémarrage à distance possible par la console TAP (port 17000, `sys reboot`), environ 2 minutes.
7. Chaque appui envoie parfois deux événements identiques, d'où l'anti-rebond de 3 s.

---

## Installation Windows (PC du beau-père)

L'exe est construit par GitHub Actions à chaque push (onglet **Actions** > dernier run « Build Windows .exe » > artefact **BosePresetBridge-Windows**). Contenu :

| Fichier | Rôle |
|---|---|
| `BosePresetBridge.exe` | le bridge (icône près de l'horloge) |
| `ProbePresets.exe` | outil de diagnostic des presets (console) |
| `config.example.ini` | modèle de configuration (**nouvelle installation seulement**) |
| `install_startup.bat` | lancement automatique à l'ouverture de session |
| `install_firewall.bat` | autorise la page web depuis le téléphone (admin) |
| `uninstall_startup.bat` | retire le lancement automatique |

### Mise à jour depuis la V1 (garder `config.ini`)

1. Clic droit sur l'icône du bridge > **Quitter**.
2. Dans le dossier du bridge (ex. `C:\BoseBridge\`), remplacer **uniquement** `BosePresetBridge.exe` par le nouveau. **Ne pas toucher à `config.ini`** : aucune clé n'est obligatoire en V2.
3. Copier aussi `ProbePresets.exe` et `install_firewall.bat` dans le même dossier.
4. Double-cliquer `BosePresetBridge.exe`. Au premier lancement, Windows demande l'autorisation administrateur pour le pare-feu : accepter (sinon lancer `install_firewall.bat`).
5. Vérifier dans `bose-bridge.log` (même dossier) :
   - `Connected to ...` puis la liste `Speaker preset 1..6` ;
   - `Speaker presets [...] differ from the configuration -> rewriting` puis `Speaker preset N <- ...: written` pour chaque preset : les presets cloud sont remplacés ;
   - ensuite, pour chaque appui : `Preset N pressed` puis `... is playing (checked on /now_playing ...)`.
6. Tester les 6 boutons de la télécommande.
7. Sur le téléphone (même Wi-Fi), ouvrir l'adresse affichée dans l'infobulle de l'icône, par exemple `http://192.168.1.20:8888/`.

Le raccourci de démarrage existant pointe toujours vers le même exe, rien à refaire. Pour une nouvelle installation : renommer `config.example.ini` en `config.ini`, y mettre l'IP et la MAC de l'enceinte, lancer `install_startup.bat`.

Lire le log sous PowerShell : `Get-Content .\bose-bridge.log -Tail 50 -Wait` (le log est en ASCII, tourne sur 3 fichiers de 1 Mo).

### Menu de l'icône

- **Ouvrir la page de réglages** (aussi par un clic sur l'icône) ;
- **Réécrire les presets de l'enceinte** : réécrit les 6 presets depuis la config ;
- **Redémarrer l'enceinte** ;
- **Autoriser dans le pare-feu** (visible seulement si la règle manque) ;
- **Quitter**.

---

## Page web (port 8888)

Depuis un téléphone ou le PC : les 6 presets en haut ; toucher un preset, puis une radio du catalogue (ou **URL personnalisée** : nom + adresse du flux MP3). **Tester** la joue tout de suite sur l'enceinte, **Enregistrer** l'écrit dans `config.ini` (seules les sections `[presets]`, `[labels]`, `[logos]` sont réécrites, une copie `config.ini.bak` est gardée) et dans le preset de l'enceinte.

Le catalogue est `catalog/radios.json` (Radio France et ses webradios, RTL, Europe 1, RMC, BFM, Chante France, Nostalgie, Chérie FM, RFM, Radio Classique). Il est chargé par le navigateur depuis `catalog_url` (par défaut ce dépôt sur GitHub) ; sans Internet côté navigateur, la copie embarquée dans l'exe est utilisée. Pour ajouter une radio : l'ajouter au JSON, lancer `python catalog/check.py` (vérifie flux et logos), pousser.

---

## Procédure de secours

Dans l'ordre, du plus simple au plus radical :

| Symptôme | Action |
|---|---|
| Un bouton ne fait rien | Appuyer à nouveau après 3 s. Le bridge relance seul et, si le moteur de lecture est bloqué, redémarre l'enceinte (1 fois par 15 min max) puis rejoue le preset : attendre 2 à 3 min. |
| Toujours rien | Icône > **Redémarrer l'enceinte** (ou bouton sur la page web), attendre 2 min, réappuyer. |
| Le bridge ne répond plus | Vérifier que l'icône est là ; sinon relancer `BosePresetBridge.exe`. Lire la fin de `bose-bridge.log`. |
| L'enceinte a changé d'IP | Mettre la nouvelle IP dans `config.ini` (`[bose] host`), relancer le bridge. Idéalement, réserver l'IP dans la box (bail DHCP fixe sur la MAC). |
| Des presets repointent vers le cloud (ex. après une réinitialisation) | Icône > **Réécrire les presets de l'enceinte** (le bridge le fait aussi seul à chaque connexion). |
| Doute sur l'écriture des presets | Quitter le bridge, lancer `ProbePresets.exe` (teste le preset 5 seulement, sauvegarde tous les presets avant, écrit un log `probe_presets_<date>.log`). |
| Enceinte figée, rien ne répond | Débrancher l'enceinte 10 s puis rebrancher. |

Le preset 1 n'est jamais réécrit si l'écriture d'un autre preset a échoué pendant la même opération.

---

## Réglages (`config.ini` ou `config.yaml`)

`config.ini` à côté de l'exe est prioritaire sur `config.yaml`. Toutes les clés V2 sont facultatives :

| `[bridge]` (ini) | yaml | Défaut | Rôle |
|---|---|---|---|
| `catalog_url` | `catalog_url` | GitHub raw de ce dépôt | catalogue de la page web |
| `debounce_seconds` | `debounce_seconds` | 3 | anti-rebond |
| `event_play_delay` | `event_play_delay_seconds` | 0.7 | attente après un appui avant la lecture (sinon le nom de la station ne s'affiche pas) |
| `watchdog` | `watchdog` | true | vérification de lecture |
| `watchdog_timeout` | `watchdog_timeout_seconds` | 10 | attente de `PLAY_STATE` |
| `auto_reboot` | `auto_reboot` | true | reboot si moteur bloqué |
| `auto_reboot_min_interval` | `auto_reboot_min_interval_seconds` | 900 | anti-boucle |
| `preset_write_method` | `preset_write_method` | upnp | `upnp`, `lir_direct`, `lir_local` ou `none` (jamais d'écriture) |
| `rewrite_presets_on_startup` | `rewrite_presets_on_startup` | true | réécrit les presets cloud à chaque connexion |
| `firewall_check` | `firewall_check` | true | Windows : règle de pare-feu au 1er lancement |

---

## Outils

- `probe_presets.py` / `ProbePresets.exe` : teste les façons d'écrire un preset (preset 5 par défaut, jamais le 1), affiche les réponses et les événements, peut restaurer depuis la sauvegarde (`--restore presets_backup_<date>.xml`). Bibliothèque standard uniquement.
- `catalog/check.py` : vérifie les flux et les logos du catalogue.
- `tools/fake_soundtouch.py` : simulateur d'enceinte (WebSocket, API, UPnP, TAP), y compris le blocage du moteur et le reboot, pour tester sans matériel.
- Tests : `python -m unittest discover -s tests`.

---

## Installation Linux (systemd)

```bash
sudo cp -r bose-preset-bridge/ /opt/bose-preset-bridge
cd /opt/bose-preset-bridge
pip3 install -r requirements.txt
sudo nano config.yaml          # bose_host, bose_mac, bose_name, presets
sudo cp bose-preset-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bose-preset-bridge
sudo journalctl -u bose-preset-bridge -f
```

Test manuel sans le service : `python3 bridge.py --test 192.168.1.38` (joue le preset 1 et affiche `/now_playing`).

---

## Fonctionnement technique

```
Bouton preset   ->  WebSocket ws://<IP>:8080 (gabbo) : <nowSelectionUpdated><preset id="N">
                        |  anti-rebond 3 s
                        v
                    UPnP http://<IP>:8091/AVTransport/Control : SetAVTransportURI + Play
                        |
                        v
                    GET http://<IP>:8090/now_playing toutes les 2 s pendant 10 s
                        |  pas PLAY_STATE -> 2e essai (POWER d'abord si STANDBY)
                        |  toujours pas   -> sys reboot (TCP 17000), attente du retour, relecture
```

## Dépendances

| Package | Usage |
|---|---|
| `websocket-client` | WebSocket subprotocol gabbo |
| `requests` | HTTP / SOAP vers l'enceinte |
| `pyyaml` | lecture de `config.yaml` |
| `pystray`, `pillow` | icône Windows |

Version ESP32 (remplace le PC, même logique, même page, même API) : voir [esp32/README.md](esp32/README.md).
