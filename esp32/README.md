# bose-preset-bridge : version ESP32

Une carte ESP32 remplace le PC Windows. Elle fait la même chose que la V2 Python (`bridge.py`) : écouter les appuis preset de la SoundTouch 20, jouer le flux par UPnP, vérifier la lecture, redémarrer l'enceinte si elle se bloque, réécrire ses presets et servir la même page web.

Référence des requêtes : [`../PROTOCOL.md`](../PROTOCOL.md). La page est `../web/index.html`, le même fichier que la V2 : elle est compressée et embarquée dans le firmware au moment de la compilation.

> **Un seul bridge à la fois.** Quand l'ESP32 est en service, quitter le bridge du PC (icône > Quitter, et retirer son lancement automatique avec `uninstall_startup.bat`). Sinon, chaque appui lance deux lectures.

## Où on en est

| | État |
|---|---|
| Compilation (`power_test` et `bridge`) | OK, vérifiée en CI (GitHub Actions) |
| Lecture des réponses de l'enceinte | testée sur ordinateur avec de vraies réponses de Valentine (`test/host/run.sh`) |
| Page web et API | page testée dans un navigateur contre une maquette de l'API ESP32 |
| **Sur une vraie carte** | **pas encore testé** : c'est l'objet des vérifications de chaque jalon ci-dessous |

## Matériel

- ELEGOO ESP-32 (ESP-WROOM-32, 30 broches, 4 Mo de flash, USB-C, puce USB CP2102 ou CH340).
- Un câble USB-C **qui transporte les données** : beaucoup de câbles fournis avec des chargeurs ne font que la charge, et la carte n'apparaît alors pas sur le Mac.
- En service : alimentation par le port micro-USB de service de la Bose, avec un adaptateur OTG (voir le jalon 0).

---

## Installer les outils sur l'iMac (une fois)

```bash
brew install pipx
```

```bash
pipx install platformio
```

```bash
pipx ensurepath
```

Fermer puis rouvrir le Terminal, puis vérifier :

```bash
pio --version
```

**Pilote USB** : sur macOS 13 (Ventura), les puces CP2102 et CH340 sont reconnues sans pilote. Brancher la carte et lister les ports :

```bash
ls /dev/cu.*
```

Un nouveau port doit apparaître, de la forme `/dev/cu.usbserial-XXXX` ou `/dev/cu.wchusbserial-XXXX` (comparer avec la liste obtenue carte débranchée). S'il n'apparaît pas :

1. changer de câble (cause n° 1) ;
2. regarder le nom de la puce sur la carte, près du connecteur USB, et installer le pilote du fabricant : WCH « CH34xVCPDriver » pour un CH340, Silicon Labs « CP210x VCP Driver » pour un CP2102. Autoriser l'extension dans Réglages Système > Confidentialité et sécurité, puis redémarrer le Mac.

`pio device list` affiche aussi les ports avec leur description.

## Flasher

Depuis le dossier `esp32/` du dépôt :

```bash
cd ~/bose-preset-bridge/esp32
```

```bash
pio run -e power_test -t upload
```

(`-e bridge` pour le bridge.) PlatformIO trouve le port tout seul ; sinon, ajouter `--upload-port /dev/cu.usbserial-XXXX`. Si l'envoi échoue sur `Connecting....` (« Failed to connect »), maintenir le bouton **BOOT** de la carte pendant que les points s'affichent, puis le relâcher.

Voir ce que la carte écrit (journal au démarrage, adresse IP) :

```bash
pio device monitor
```

Ctrl+C pour quitter. Le moniteur doit être fermé avant de reflasher.

**Sans installer PlatformIO** : dans Chrome, ouvrir https://espressif.github.io/esptool-js/, cliquer « Connect », choisir le port, puis flasher le fichier `bridge-full.bin` (ou `power_test-full.bin`) à l'adresse `0x0`. Ces fichiers sont dans l'artefact **bose-bridge-esp32** du workflow « Build ESP32 firmware » (onglet Actions du dépôt GitHub).

Tout effacer (Wi-Fi, réglages, presets, compteurs) :

```bash
pio run -e bridge -t erase
```

---

## Jalon 0 : test d'alimentation

**But** : savoir si le port USB de service de la Bose alimente l'ESP32 quand l'enceinte est en veille. La réponse décide du comportement au réveil (voir le jalon 3).

**Ce que fait le firmware `power_test`** : il compte ses démarrages (en NVS), note la cause du dernier reset et, toutes les 30 s, enregistre depuis combien de temps il tourne et l'état de l'enceinte (`/now_playing`). Après une coupure, la page montre combien de temps la session précédente a duré et dans quel état était l'enceinte juste avant.

**Flasher et configurer**
1. `pio run -e power_test -t upload`
2. Au premier démarrage, la carte crée le réseau Wi-Fi **Bose-Bridge-Setup**. S'y connecter avec l'iPhone ; la page de configuration s'ouvre seule (sinon, aller sur http://192.168.4.1). Choisir le Wi-Fi de la maison, taper son mot de passe et vérifier l'IP de l'enceinte (192.168.1.17 par défaut).
3. Ouvrir http://bose-bridge.local/ (ou l'IP affichée par `pio device monitor`).

**À vérifier sur place**
1. Brancher l'ESP32 sur le port USB de la Bose (adaptateur OTG), enceinte allumée. Noter le nombre de démarrages.
2. Mettre l'enceinte en veille avec la télécommande. Attendre 10 min (elle peut couper l'USB après un délai).
3. La rallumer et recharger la page :
   - **« Démarrages » n'a pas bougé et « Allumé depuis » continue de compter** : l'ESP32 reste alimenté en veille. C'est le cas simple.
   - **Le compteur a augmenté** : l'USB est coupé en veille. La ligne d'historique indique la durée de la session coupée et « Enceinte juste avant » (probablement `STANDBY`).
4. Refaire le test après une nuit en veille (certaines enceintes coupent l'USB en veille profonde, au bout de plusieurs heures).
5. Débrancher l'enceinte du secteur 10 s : le compteur doit augmenter de 1, avec la cause `POWERON`. Cela vérifie la mesure elle-même.

Me donner : alimenté en veille (oui ou non, et après combien de temps), la cause affichée, l'historique.

---

## Jalon 1 : réseau et configuration

Firmware `bridge` : `pio run -e bridge -t upload`. Le Wi-Fi enregistré au jalon 0 est conservé.

**Ce qui est fait**
- **Portail Wi-Fi** « Bose-Bridge-Setup » (WiFiManager) au premier démarrage. Il demande aussi :
  - le **mot de passe des mises à jour** (OTA) ;
  - l'**IP de secours** de l'enceinte.

  Si le Wi-Fi est déjà configuré, le portail ne s'ouvre jamais tout seul : une box qui redémarre ne bloque pas le bridge, il se reconnecte en arrière-plan.
- **Rouvrir le portail** : carte démarrée, maintenir BOOT 5 s. Cela efface le Wi-Fi et redémarre sur le portail. Ne pas maintenir BOOT *pendant* le branchement : la carte démarrerait en mode programmation, sans lancer le firmware.
- **Nom** : `bose-bridge.local` (mDNS). La page est sur le port **80** (http://bose-bridge.local/), pas 8888 comme sur le PC.
- **IP de l'enceinte**, dans l'ordre :
  1. découverte SSDP au démarrage (UDN `uuid:BO5EBO5E-F00D-F00D-FEED-<MAC>`, en préférant la MAC configurée, par défaut `A81B6A51E435`, sinon la première enceinte Bose qui répond) ;
  2. à défaut, dernière IP connue ;
  3. à défaut, IP de secours (192.168.1.17), modifiable depuis la page, section « Réglages du boîtier ».

  Si l'enceinte ne répond plus pendant 90 s hors redémarrage, la découverte est relancée (au plus une fois par minute) : un changement d'IP par la box est pris en compte tout seul.
- **Réglages en NVS** : presets, noms et logos par défaut identiques à `config.yaml` (extraits automatiquement à la compilation par `scripts/embed_page.py`).

**À vérifier sur place**
1. http://bose-bridge.local/ s'ouvre depuis l'iPhone.
2. Section « Boîtier » : « Enceinte : 192.168.1.17 (trouvée automatiquement) ».
3. Section « Journal » : `Découverte SSDP (démarrage) : enceinte A81B6A51E435 trouvée en 192.168.1.17`.
4. Si « (IP de secours) » s'affiche à la place, la découverte a échoué : me donner le journal.

## Jalon 2 : bridge

**Ce qui est fait** : c'est la même logique que la V2.
- **WebSocket** `ws://IP:8080`, sous-protocole `gabbo` (bibliothèque links2004). Ping toutes les 30 s. Reconnexion au bout de 5 s, délai doublé jusqu'à 60 s, remis à 5 s après chaque connexion réussie.
- **Événements** : lecture de `<nowSelectionUpdated>` (preset 1 à 6, `id="0"` ignoré), anti-rebond de 3 s. Repli sur `<nowPlayingUpdated>` quand un preset pointe encore vers le cloud.
- **Lecture** : attente de 0,7 s après l'appui (sinon l'enceinte n'affiche pas le nom), puis `SetAVTransportURI`, avec les métadonnées DIDL-Lite doublement échappées, puis `Play`.
- **Réécriture des presets de l'enceinte** avec la méthode validée en V2 (`storePreset`, source `UPNP`), à chaque connexion pour ceux qui pointent vers le cloud ou diffèrent de la configuration. Les presets 2 à 6 sont écrits d'abord, chacun vérifié ; le **preset 1 en dernier, et seulement si tous les autres ont réussi**.

**À vérifier sur place** (bridge du PC arrêté)
1. Journal : `Connecté à l'enceinte`, la liste des 6 presets de l'enceinte, puis éventuellement `Preset N de l'enceinte <- ... : written`.
2. Appuyer sur chaque bouton de la télécommande. Le journal montre `WS nowSelectionUpdated preset=N`, puis `... joue (vérifié sur /now_playing, essai 1)`. L'écran de l'enceinte affiche le nom de la station.
3. Appuyer deux fois vite sur le même bouton : `événement en double ... ignoré`.

## Jalon 3 : watchdog et reboot

**Ce qui est fait**
- **Vérification de la lecture** : après chaque Play, `/now_playing` est lu toutes les 2 s pendant 10 s, jusqu'à obtenir `source="UPNP"` et `PLAY_STATE`. Sinon, un 2e essai, avec POWER envoyé d'abord si l'enceinte est en veille.
- **Reboot de l'enceinte** si le 2e essai échoue aussi : `sys reboot` sur le port 17000, puis attente de l'arrêt (90 s max) et du retour (WebSocket reconnecté et `/info` qui répond, 6 min max), puis relecture du dernier preset.
- **Cas où l'on ne redémarre pas** : flux lui-même injoignable, bouton « Tester » de la page, enceinte injoignable.
- **Si l'enceinte coupe l'ESP32 en redémarrant** : avant d'envoyer le reboot, l'ESP32 note en NVS le preset à rejouer et l'heure. À son redémarrage, si cette note a moins de 5 min, il attend que l'enceinte réponde, rejoue le preset, puis efface la note. Sans heure NTP, le critère devient « démarrage suivant immédiat ».
- **Anti-boucle** : au plus 1 reboot automatique par 15 min, retenu en NVS (heure NTP si disponible, sinon compteur de démarrages et durée de fonctionnement).
- **Chien de garde matériel** (`esp_task_wdt`, 30 s) sur la boucle principale et sur la tâche de lecture : si le firmware se fige, l'ESP32 redémarre seul.
- **Reprise à la mise sous tension** (`resume_on_power_on`, activée par défaut) : ne sert que si le jalon 0 montre que l'ESP32 est coupé en veille. Dans ce cas, l'appui qui réveille l'enceinte est perdu, car l'ESP32 démarre après lui. Au démarrage (cause `POWERON`), si l'enceinte est allumée mais ne joue rien (`INVALID_SOURCE`), l'ESP32 joue le dernier preset utilisé. Juste après un flashage par USB, cela peut aussi lancer une lecture si l'enceinte est allumée et muette.

**À vérifier sur place**
1. Page > « Redémarrer l'enceinte ». Le journal montre `REDÉMARRAGE ... via le port TAP 17000`, puis `enceinte arrêtée`, puis `enceinte revenue après ~110 s`. Si l'ESP32 est alimenté par l'enceinte, il redémarre aussi : la section « Boîtier » indique alors `POWERON` et un compteur de démarrages +1.
2. Le blocage réel (preset cloud) ne se provoque pas sur commande. Quand il arrivera, le journal doit montrer : `pas de lecture après 10 s`, puis le 2e essai, puis `REDÉMARRAGE ... moteur de lecture bloqué`, puis la relecture (`relecture après reboot` si l'ESP32 a été coupé).
3. Si l'ESP32 est coupé en veille : enceinte en veille, appuyer sur un preset. Le son doit arriver en ~10 s (démarrage de l'ESP32 + Wi-Fi), avec le dernier preset joué.

## Jalon 4 : page web et API

**Ce qui est fait**
- **Même page** que la V2 (`web/index.html`, 21 Ko, 7 Ko gzippé, en PROGMEM).
- **Même API** : `/api/status`, `/api/presets`, `PUT /api/presets/{n}`, `/api/play/{n}`, `/api/test`, `/api/reboot-speaker`, `/api/rewrite-speaker-presets`. Seule différence : l'écriture dans l'enceinte se fait en tâche de fond. `PUT` répond donc `"speaker": "pending"` et la réécriture répond `202 {"pending": true}` ; la page attend le résultat dans `status.last_rewrite` (voir PROTOCOL.md § 6).
- **`/api/status` en plus** : `device` (uptime, RSSI, mémoire libre, cause du dernier reset, compteur de démarrages) et `features`. La page n'affiche les sections « Boîtier », « Réglages du boîtier », « Mise à jour du boîtier » et « Journal » que si ces fonctions sont annoncées : le bridge du PC sert donc la même page sans ces sections.
- **Journal** : les 200 dernières lignes en RAM, sur `/api/log` et dans la page.
- **Réglages** : `GET` et `PUT /api/settings` (IP de secours, MAC, etc.) ; `POST /api/discover` relance la recherche de l'enceinte.
- **Mise à jour OTA** : section « Mise à jour du boîtier » de la page, fichier `bridge-ota.bin`, mot de passe défini au portail Wi-Fi. Sans mot de passe, l'OTA est refusée ; on peut en définir un une fois par `PUT /api/settings {"ota_password": "..."}`.
- **Catalogue** : chargé par le navigateur depuis `catalog_url`, jamais par l'ESP32.

**À vérifier sur place**
1. Changer un preset depuis l'iPhone. Le message « écriture dans l'enceinte… » s'affiche, puis « enceinte à jour ✓ ». Appuyer ensuite sur ce bouton de la télécommande.
2. Bouton « Tester » : la radio joue tout de suite.
3. Mise à jour OTA avec le fichier `bridge-ota.bin` de l'artefact CI. La page affiche « le boîtier redémarre » et la version reste affichée en bas de la section « Enceinte ».

---

## Procédure de secours

| Symptôme | Action |
|---|---|
| Un bouton ne fait rien | Réappuyer après 3 s. En cas de blocage, l'ESP32 redémarre l'enceinte tout seul (1 fois par 15 min) : attendre 2 à 3 min. |
| Toujours rien | Page > « Redémarrer l'enceinte », attendre 2 min. |
| Page injoignable | Débrancher et rebrancher l'ESP32 (ou l'enceinte, s'il est alimenté par elle). Si le problème persiste, brancher la carte sur le Mac et lire `pio device monitor`. |
| Changement de box ou de mot de passe Wi-Fi | Carte démarrée, maintenir BOOT 5 s, puis se connecter au réseau « Bose-Bridge-Setup ». |
| Mise à jour OTA ratée | L'ancienne version reste active (deux partitions). Au pire, reflasher par USB. |
| Rien ne marche | Revenir au PC : relancer `BosePresetBridge.exe` et `install_startup.bat`. Les deux bridges ont la même page et les mêmes réglages. |

## Différences avec la V2 (PC)

- Port 80 au lieu de 8888 ; adresse `bose-bridge.local`.
- URL de flux ou de logo : 511 caractères maximum (1024 sur PC).
- Méthodes d'écriture des presets : `upnp` (défaut) et `lir_direct` ; `lir_local` n'est pas proposée (inutile, voir PROTOCOL.md § 3.4).
- Pas de copie locale du catalogue : sans Internet côté navigateur, utiliser « URL personnalisée ».
- Réglages en NVS au lieu de `config.ini`, modifiables depuis la page.

## Organisation du code

| Fichier | Rôle |
|---|---|
| `src/power_test/main.cpp` | jalon 0 |
| `src/bridge/main.cpp` | démarrage, boucle, chien de garde |
| `src/bridge/netsetup.cpp` | Wi-Fi (portail), mDNS, NTP, bouton BOOT |
| `src/bridge/events.cpp` | WebSocket, anti-rebond, reconnexion |
| `src/bridge/player.cpp` | tâche de lecture : watchdog, reboot, réécriture des presets, découverte |
| `src/bridge/speaker.cpp` | requêtes vers l'enceinte (HTTP, SOAP, TAP, SSDP) |
| `src/bridge/parse.h`, `xmlmini.h` | lecture des réponses XML (testée sur ordinateur) |
| `src/bridge/web.cpp` | serveur web asynchrone et API |
| `src/bridge/common.*` | réglages NVS, état partagé, journal, heure |
| `scripts/embed_page.py` | embarque la page et les presets par défaut à la compilation |
| `test/host/` | test du parsing sur ordinateur : `test/host/run.sh` |

Trois contextes d'exécution : la boucle Arduino (WebSocket), la tâche `player` (seule à faire des requêtes bloquantes vers l'enceinte) et `async_tcp` (serveur web, qui ne bloque jamais : il dépose une commande dans une file et répond tout de suite).
