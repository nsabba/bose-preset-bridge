# bose-preset-bridge

Service Python léger qui intercepte les appuis sur les boutons preset d'une enceinte **Bose SoundTouch** et joue les flux configurés via **UPnP AVTransport** — entièrement en local, sans dépendance au cloud Bose (arrêté en mai 2026).

## Prérequis

- Python 3.8+
- Enceinte Bose SoundTouch sur le même réseau local (firmware 27.x testé)
- Ubuntu 20.04 / Debian (ou tout Linux avec systemd)

---

## Installation rapide

### 1. Copier les fichiers

```bash
sudo cp -r bose-preset-bridge/ /opt/bose-preset-bridge
sudo chown -R root:root /opt/bose-preset-bridge
```

### 2. Éditer la configuration

```bash
sudo nano /opt/bose-preset-bridge/config.yaml
```

Modifier a minima :

| Clé | Valeur |
|---|---|
| `bose_host` | IP locale de l'enceinte (ex. `192.168.1.38`) |
| `bose_mac` | Adresse MAC sans `:` (ex. `0CAE7D5422F4`) |
| `bose_name` | Nom affiché dans les logs |
| `presets.1` | URL du flux à jouer pour le preset 1 |

Flux France Inter : `http://icecast.radiofrance.fr/franceinter-midfi.mp3`

### 3. Installer les dépendances Python

```bash
cd /opt/bose-preset-bridge
pip3 install -r requirements.txt
```

### 4. Installer le service systemd

```bash
sudo cp /opt/bose-preset-bridge/bose-preset-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bose-preset-bridge
sudo systemctl start bose-preset-bridge
```

### 5. Vérifier

```bash
sudo systemctl status bose-preset-bridge
sudo journalctl -u bose-preset-bridge -f
```

Sortie attendue :

```
[2026-05-19 20:00:01] INFO  Connecting to ws://192.168.1.38:8080 ...
[2026-05-19 20:00:01] INFO  Connected to Valentine (192.168.1.38)
```

---

## Test manuel (sans lancer le service)

Envoie directement une commande UPnP à l'enceinte pour vérifier que le flux joue :

```bash
python3 bridge.py --test 192.168.1.38
```

Cela utilise l'URL du preset 1 définie dans `config.yaml`.

---

## Fonctionnement technique

```
Bouton preset   →   WebSocket ws://<IP>:8080 (subprotocol gabbo)
                        ↓ XML event
                    bridge.py parse <nowSelectionUpdated>
                        ↓
                    UPnP SOAP POST http://<IP>:8091/AVTransport/control
                        SetAVTransportURI  (charge l'URL)
                        Play               (lance la lecture)
```

- **WebSocket gabbo** : protocole propriétaire Bose, émet des fragments XML lors de chaque changement d'état (sélection de preset, volume, etc.).
- **UPnP AVTransport** : standard UPnP Media Renderer, port 8091 sur le firmware Bose 27.x.
- **Reconnexion automatique** : backoff exponentiel de `reconnect_delay_seconds` jusqu'à `reconnect_max_delay_seconds` en cas de coupure réseau ou redémarrage de l'enceinte.

---

## Déploiement sur une deuxième enceinte

Copier le répertoire, modifier uniquement `config.yaml` (`bose_host`, `bose_mac`, `bose_name`) et créer un second service systemd avec un nom différent :

```bash
sudo cp /etc/systemd/system/bose-preset-bridge.service \
        /etc/systemd/system/bose-preset-bridge-salon.service
# Éditer WorkingDirectory et ExecStart dans le nouveau fichier
sudo systemctl daemon-reload
sudo systemctl enable --now bose-preset-bridge-salon
```

---

## Dépendances

| Package | Usage |
|---|---|
| `websocket-client` | Connexion WebSocket subprotocol gabbo |
| `requests` | Requêtes SOAP HTTP vers UPnP |
| `pyyaml` | Lecture de `config.yaml` |

Toutes disponibles sur PyPI, aucune dépendance système exotique.
