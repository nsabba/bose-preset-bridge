# PROTOCOL.md: every request of bose-preset-bridge

Référence pour le portage ESP32 : toutes les requêtes que le bridge échange avec l'enceinte,
et l'API HTTP servie à la page web. Les exemples de réponses ont été relevés le 24/09/2026 sur
une SoundTouch 20, firmware `27.0.6.46330.5043500` (même firmware que l'enceinte cible).

Notations : `IP` = adresse de l'enceinte, `BRIDGE` = adresse du PC ou de l'ESP32.

| Port | Protocole | Usage |
|---|---|---|
| 8080 | WebSocket, subprotocol `gabbo` | événements (appui preset…) |
| 8090 | HTTP, XML | API « webservices » : infos, presets, touches |
| 8091 | HTTP, SOAP | UPnP AVTransport : lecture d'un flux |
| 17000 | TCP brut | console TAP : `sys reboot` |
| 8888 | HTTP, JSON | **servi par le bridge** : page + API |

---

## 1. Événements : WebSocket `ws://IP:8080`

Handshake WebSocket standard (RFC 6455) avec l'en-tête `Sec-WebSocket-Protocol: gabbo`
(obligatoire, sinon l'enceinte refuse). L'enceinte envoie des trames texte XML ; le client
envoie seulement des pings (toutes les 30 s ; pong attendu en 10 s, sinon reconnexion).

Premier message à la connexion :

```xml
<SoundTouchSdkInfo serverVersion="4" serverBuild="..." />
```

Séquence observée pour **un appui sur le preset 5** (contenu `UPNP` stocké) :

```xml
<updates deviceID="0CAE7D5422F4"><nowSelectionUpdated><preset id="5"><ContentItem source="UPNP" location="http://..." sourceAccount="UPnPUserName" isPresetable="true">...</ContentItem></preset></nowSelectionUpdated></updates>
<errorUpdate deviceID="0CAE7D5422F4"><error value="1036" name="UNABLE_TO_PROCESS_NOT_LOGGED_IN" severity="Unrecoverable">UpnpRcvdContentItemInWrongState</error></errorUpdate>
<updates deviceID="0CAE7D5422F4"><nowSelectionUpdated><preset id="0"><ContentItem source="INVALID_SOURCE" .../></preset></nowSelectionUpdated></updates>
<updates deviceID="0CAE7D5422F4"><nowPlayingUpdated><nowPlaying source="INVALID_SOURCE">...</nowPlaying></nowPlayingUpdated></updates>
```

Règles du bridge :

* **Déclencheur** : `nowSelectionUpdated` avec `preset id` entre 1 et 6. Ignorer `id="0"`.
* **Délai avant lecture** : attendre 0,7 s après l'événement avant SetAVTransportURI. L'enceinte met
  ~0,2 s à traiter sa propre sélection (`errorUpdate` puis `nowSelectionUpdated preset id="0"`) ; un
  Play envoyé avant joue bien, mais sans métadonnées : `/now_playing` montre
  `location="unplayable location"` et un `<track>` vide, donc pas de nom sur l'écran. Si cela arrive
  malgré tout, renvoyer SetAVTransportURI + Play une fois (le watchdog le fait).
* **Debounce** : un même preset reçu moins de 3 s après le précédent est ignoré (l'enceinte
  envoie parfois deux événements identiques dans la même seconde).
* **Repli** (enceinte encore sur le cloud) : si un `nowPlayingUpdated` porte un `ContentItem`
  dont la `location` est exactement celle d'un preset cloud (`bose.io` ou `source="TUNEIN"`), on le
  traite comme un appui sur ce preset (fenêtre de debounce : 15 s). Jamais pour un contenu non cloud,
  sinon la lecture que le bridge lance redéclencherait l'événement.
* **Reconnexion** : délai 5 s, doublé à chaque échec jusqu'à 60 s, **remis à 5 s à chaque
  connexion réussie** ; plafonné à 5 s pendant un redémarrage de l'enceinte.
* À chaque connexion : `GET /info`, `GET /presets` (cache), réécriture des presets si besoin (§ 3.4).

---

## 2. Lecture d'un flux : UPnP AVTransport `http://IP:8091`

URL de contrôle : `http://IP:8091/AVTransport/Control` (découvrable dans
`http://IP:8091/XD/BO5EBO5E-F00D-F00D-FEED-<MAC sans :>.xml`, mais la valeur par défaut suffit).

### 2.1 SetAVTransportURI

```
POST /AVTransport/Control HTTP/1.1
Content-Type: text/xml; charset="utf-8"
SOAPAction: "urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"

<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
      <CurrentURI>http://icecast.radiofrance.fr/fip-midfi.mp3</CurrentURI>
      <CurrentURIMetaData>&lt;DIDL-Lite ...&gt;...&lt;/DIDL-Lite&gt;</CurrentURIMetaData>
    </u:SetAVTransportURI>
  </s:Body>
</s:Envelope>
```

`CurrentURI` : URL XML-échappée (`&` → `&amp;`). `CurrentURIMetaData` : le DIDL-Lite ci-dessous,
**lui-même XML-échappé** (c'est ce qui donne le nom affiché dans `/now_playing`) :

```xml
<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
           xmlns:dc="http://purl.org/dc/elements/1.1/"
           xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
  <item id="1" parentID="0" restricted="1">
    <dc:title>FIP</dc:title>
    <upnp:class>object.item.audioItem.audioBroadcast</upnp:class>
    <upnp:albumArtURI>https://.../logo.png</upnp:albumArtURI>
    <res protocolInfo="http-get:*:audio/mpeg:*">http://icecast.radiofrance.fr/fip-midfi.mp3</res>
  </item>
</DIDL-Lite>
```

Réponse attendue : `HTTP 200` (enveloppe SOAP `SetAVTransportURIResponse`).

Effet sur `/now_playing` : `dc:title` → `<itemName>` et `<track>` (texte affiché par l'écran de
l'enceinte) ; `upnp:artist` → `<artist>`, `upnp:album` → `<album>` ; `upnp:albumArtURI` →
`<art artImageStatus="IMAGE_PRESENT">url</art>` (pour les applis seulement : l'écran de la
SoundTouch 20 n'affiche que du texte).

### 2.2 Play

Même URL et en-têtes, `SOAPAction: "urn:schemas-upnp-org:service:AVTransport:1#Play"` :

```xml
<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
  <InstanceID>0</InstanceID>
  <Speed>1</Speed>
</u:Play>
```

Réponse attendue : `HTTP 200`. **Attention** : quand le moteur de lecture est bloqué (§ 5),
SetAVTransportURI et Play répondent quand même 200. Seul `/now_playing` fait foi.

### 2.3 GetTransportInfo (diagnostic uniquement)

`SOAPAction: "...#GetTransportInfo"`, corps `<u:GetTransportInfo ...><InstanceID>0</InstanceID></u:GetTransportInfo>`.
Réponse : `<CurrentTransportState>PLAYING | PAUSED_PLAYBACK | STOPPED | TRANSITIONING</CurrentTransportState>`.

Flux compatibles : MP3 en HTTP direct (`audio/mpeg`), sans redirection de préférence. Pas de HLS
(`.m3u8`). HTTPS à éviter (firmware de 2022). Une redirection 302 HTTP → HTTP est suivie
(vérifié avec RTL).

---

## 3. API webservices `http://IP:8090`

Toutes les réponses sont en XML (`Content-Type: text/xml`). Les corps POST sont envoyés avec
`Content-Type: application/xml`.

### 3.1 GET /info

```xml
<info deviceID="0CAE7D5422F4"><name>Valentine</name><type>SoundTouch 20</type>
  <components><component><componentCategory>SCM</componentCategory>
  <softwareVersion>27.0.6.46330.5043500 epdbuild.trunk...</softwareVersion>...</component></components>
  <networkInfo type="SCM"><macAddress>0CAE7D5422F4</macAddress><ipAddress>192.168.1.10</ipAddress></networkInfo>...
</info>
```

Sert de test « l'enceinte répond » (timeout 3 s).

### 3.2 GET /now_playing: vérité sur la lecture

Lecture OK (la seule condition de succès du watchdog) :

```xml
<nowPlaying deviceID="..." source="UPNP" sourceAccount="UPnPUserName">
  <ContentItem source="UPNP" location="http://..." sourceAccount="UPnPUserName" isPresetable="false"><itemName>FIP</itemName></ContentItem>
  ...<playStatus>PLAY_STATE</playStatus>
</nowPlaying>
```

Autres états observés :

| `source` | `playStatus` | Sens |
|---|---|---|
| `UPNP` | `BUFFERING_STATE` | démarrage en cours (1 à 2 s) |
| `STANDBY` | (absent) | en veille |
| `INVALID_SOURCE` | (absent) | rien ne joue ; aussi l'état « moteur bloqué » |
| `SETUP` | (absent) | juste après un démarrage de l'enceinte |

**Ne pas utiliser `/sources`** : `UPNP status="UNAVAILABLE"` y est affiché même quand la lecture
UPnP fonctionne.

### 3.3 POST /key: touche de la télécommande

```xml
<key state="press" sender="Gabbo">POWER</key>
<key state="release" sender="Gabbo">POWER</key>
```

Deux requêtes (press puis release immédiat). Réponse `200`. Touches utilisées : `POWER`
(bascule marche/veille), `PRESET_1` … `PRESET_6` (tests seulement). **Ne jamais laisser plus
d'une seconde entre press et release sur un `PRESET_n`** : un appui long enregistre le contenu
courant dans le preset.

### 3.4 GET /presets, POST /storePreset, POST /removePreset

`GET /presets` :

```xml
<presets>
  <preset id="1"><ContentItem source="UPNP" location="http://icecast.radiofrance.fr/franceinter-midfi.mp3" sourceAccount="UPnPUserName" isPresetable="true" /></preset>
  <preset id="5" createdOn="1790246894" updatedOn="1790246894"><ContentItem source="UPNP" location="http://..." sourceAccount="UPnPUserName" isPresetable="true"><itemName>FIP</itemName></ContentItem></preset>
  ...
</presets>
```

Contenus « cloud » (à réécrire) : `location` contenant `bose.io`
(`LOCAL_INTERNET_RADIO`, adaptateur `orion`, `data=` = base64 de `{name,imageUrl,streamUrl}`) ou
`source="TUNEIN"` (`location="/v1/playback/station/sXXXX"`).

`POST /storePreset` (non documenté par Bose, **validé le 24/09/2026**) :

```xml
<preset id="5"><ContentItem source="UPNP" location="http://icecast.radiofrance.fr/fip-midfi.mp3" sourceAccount="UPnPUserName" isPresetable="true"><itemName>FIP</itemName></ContentItem></preset>
```

Réponse `200` avec la liste complète des presets (même format que `GET /presets`). Toujours
relire `GET /presets` pour vérifier `source` et `location` du preset écrit.

Méthodes testées par `probe_presets.py` (toutes stockées correctement) :

| Méthode | ContentItem | Appui sur le preset |
|---|---|---|
| `upnp` (**retenue**) | `source="UPNP" location=<flux> sourceAccount="UPnPUserName"` | événement `nowSelectionUpdated` émis, pas de lecture autonome (`INVALID_SOURCE`), **le Play UPnP qui suit fonctionne** |
| `lir_direct` | `source="LOCAL_INTERNET_RADIO" type="stationurl" location=<flux>` | idem |
| `lir_local` | `source="LOCAL_INTERNET_RADIO" type="stationurl" location=http://BRIDGE:8888/orion/station?data=<base64url>` | idem ; l'enceinte n'appelle même pas l'URL |

Conclusion : l'enceinte ne joue jamais seule un preset local ; le bridge reste nécessaire. Mais
avec un contenu local, l'appui ne déclenche plus aucun appel cloud et ne bloque plus le moteur.

Ordre de réécriture : presets 2 à 6 d'abord, chacun vérifié ; **preset 1 en dernier et seulement
si tous les autres ont réussi**.

`POST /removePreset` : `<preset id="5"></preset>`. Réponse : la liste des presets. Non utilisé.

---

## 4. Redémarrage : console TAP `IP:17000`

1. connexion TCP ;
2. attendre ~1 s (la console envoie une invite, à lire et ignorer) ;
3. envoyer exactement `sys reboot\n` (LF seul) ;
4. attendre 0,5 s, fermer.

Un envoi immédiat avec `\r\n` sans attente n'a rien fait. Après l'envoi : l'enceinte coupe en
~3 s (WebSocket perdu, `/info` ne répond plus), revient en **~110 s** (mesuré : 108 s). Retour =
WebSocket reconnecté **et** `/info` répond ; attendre encore 5 s avant de rejouer.

---

## 5. Watchdog de lecture (algorithme)

```
play(item):
  essai = 1 ; power_envoye = non
  répéter:
    SetAVTransportURI + Play
    toutes les 2 s pendant 10 s : GET /now_playing
        source == UPNP et playStatus == PLAY_STATE  -> SUCCÈS
        2 lectures de suite source == STANDBY       -> sortir « veille »
    si « veille » et pas power_envoye : POST /key POWER, attendre 3 s, recommencer (ne compte pas)
    si essai == 1 : essai = 2, recommencer
    sinon : ÉCHEC
  si ÉCHEC :
    /now_playing n'a jamais répondu           -> abandon (enceinte injoignable)
    requête venant du bouton « Tester »       -> abandon (pas de reboot pour un test)
    le flux ne répond pas depuis le bridge    -> abandon (flux mort, rebooter ne sert à rien)
    dernier reboot auto il y a < 15 min       -> abandon (anti-boucle)
    sinon : reboot TAP (§ 4), attendre le retour, rejouer le dernier preset demandé
```

Une nouvelle demande (appui ou page web) annule la vérification en cours. Pendant un reboot,
les demandes sont mémorisées et la dernière est jouée au retour.

---

## 6. API HTTP du bridge (port 8888), servie aussi par l'ESP32

JSON UTF-8 partout. Pas d'authentification (réseau local). Erreurs :
`{"ok": false, "error": "<code>", "message": "<texte>"}` avec un statut 4xx/5xx.

| Méthode | Chemin | Corps | Réponse |
|---|---|---|---|
| GET | `/` | | la page (un seul fichier HTML) |
| GET | `/api/status` | | voir ci-dessous |
| GET | `/api/presets` | | `{"presets":[{"id":1,"name":"France Inter","stream_url":"http://…","logo_url":"https://…"}, … 6 entrées]}` (preset vide : chaînes vides) |
| PUT | `/api/presets/{n}` | `{"name","stream_url","logo_url"}` | `200 {"ok":true,"preset":n,"config":"saved","speaker":"written" \| "skipped (…)" \| "failed: …"}` |
| POST | `/api/play/{n}` | | `202 {"ok":true,"preset":n}` ; `409 preset_empty` |
| POST | `/api/test` | `{"stream_url","name"?,"logo_url"?}` | `202 {"ok":true}` (joue tout de suite, sans reboot automatique) |
| POST | `/api/reboot-speaker` | | `202 {"ok":true}` ; `409 busy` |
| POST | `/api/rewrite-speaker-presets` | | `200 {"ok":true,"method":"upnp","results":{"1":{"ok":true,"message":"written"},…}}` ; `409 method_not_validated \| busy` ; `502` si un preset a échoué |
| GET | `/catalog/radios.json` | | copie embarquée du catalogue (repli si `catalog_url` injoignable ; l'ESP32 peut répondre 404) |
| GET | `/orion/station?data=…` | | JSON station pour la méthode `lir_local` (inutile avec `upnp`) |

Validation de `PUT` : `n` de 1 à 6 ; `stream_url` vide (= vider le preset) ou `http(s)://…`
(≤ 1024 car.) ; `name` obligatoire si `stream_url`, 64 car. max ; `logo_url` vide ou `http(s)://…`.
Corps ≤ 8 Ko.

`GET /api/status` :

```json
{
  "version": "2.0.0",
  "speaker": {"name": "Valentine", "host": "192.168.1.10"},
  "ws_connected": true,
  "now_playing": {"source": "UPNP", "play_status": "PLAY_STATE", "item_name": "FIP", "location": "http://…"},
  "last_preset": {"id": 5, "name": "FIP", "at": "2026-09-24T13:01:42", "inferred": false},
  "playing": {"id": 5, "name": "FIP"},
  "player": {"state": "playing", "message": "FIP is playing (checked on /now_playing, attempt 1)"},
  "last_auto_reboot": null,
  "rebooting": false,
  "preset_write_method": "upnp",
  "speaker_presets_cloud": [],
  "last_rewrite": null,
  "catalog_url": "https://raw.githubusercontent.com/nsabba/bose-preset-bridge/main/catalog/radios.json",
  "page_url": "http://192.168.1.25:8888/"
}
```

`now_playing` vaut `null` si l'enceinte ne répond pas (mis en cache 2 s). `player.state` ∈
`idle | starting | playing | retrying | rebooting | failed`.

---

## 7. Catalogue (`catalog/radios.json`)

Chargé **par le navigateur** depuis `catalog_url` (GitHub raw envoie
`Access-Control-Allow-Origin: *`), puis `/catalog/radios.json` en repli.

```json
{"version": 1, "updated": "2026-09-24", "radios": [
  {"id": "france-inter", "name": "France Inter",
   "stream_url": "http://icecast.radiofrance.fr/franceinter-midfi.mp3",
   "logo_url": "https://thumb.wikimedia.org/…/250px-France_Inter_logo_2021.svg.png",
   "tags": ["radio-france", "generaliste"]}
]}
```

La page accepte aussi un tableau nu. `python catalog/check.py` vérifie chaque flux (HEAD ou GET
partiel → 200 + `audio/mpeg`, signale redirections, HTTPS, HLS) et chaque logo.

---

## 8. Notes pour l'ESP32

* Stockage NVS : les 6 presets `{name, stream_url, logo_url}` + `bose_host` + les réglages de la
  config (`preset_write_method`, `watchdog_timeout`, `auto_reboot_min_interval`, `debounce_seconds`).
* `web/index.html` peut être servi tel quel (21 Ko, 7 Ko en gzip avec `Content-Encoding: gzip`).
* Horloge : l'anti-boucle n'a besoin que d'un compteur monotone (`millis()`), pas de l'heure.
* `tools/fake_soundtouch.py` simule l'enceinte (y compris le blocage et le reboot) pour tester sans
  matériel ; il écoute sur 127.0.0.1 : lancer le firmware en simulation ou adapter `HOST`.
