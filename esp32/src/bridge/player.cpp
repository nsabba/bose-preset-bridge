// Tâche player : voir player.h. Logique identique à bridge.py (classe Player et Bridge),
// plus ce qu'impose l'alimentation par l'enceinte : l'ESP32 peut s'éteindre pendant un
// reboot de l'enceinte, donc le preset à rejouer est noté en NVS avant le reboot.
#include "player.h"

#include <WiFi.h>
#include <atomic>
#include <esp_task_wdt.h>
#include <vector>

#include "common.h"
#include "events.h"
#include "speaker.h"

// ---------------------------------------------------------------------------
// File de commandes
// ---------------------------------------------------------------------------

enum CmdType : uint8_t { C_PLAY, C_REWRITE, C_REBOOT, C_DISCOVER, C_CONNECTED };

struct Cmd {
  CmdType type;
  uint32_t seq;          // numéro de demande de lecture (une demande plus récente annule la courante)
  int8_t id;             // preset 1 à 6, 0 = test
  bool allowReboot;
  uint16_t delayMs;
  uint8_t mask;          // C_REWRITE : presets à réécrire
  char name[MAX_NAME + 1];
  char url[MAX_URL];
  char logo[MAX_URL];
};

static QueueHandle_t queue;
static std::atomic<uint32_t> playSeq{0};
static std::atomic<bool> fastReconnect{false};
static Cmd lastItem, pendingItem;             // protégés par gLock
static bool hasLastItem = false, hasPending = false;
static uint32_t lastAutoRebootMs = 0;          // 0 = pas de reboot auto pendant ce démarrage
static uint32_t wsDownSinceMs = 0, lastDiscoverMs = 0;

// NVS (espace "state") :
//   rp_id / rp_epoch / rp_boot : preset à rejouer après un reboot de l'enceinte
//   ar_epoch / ar_boot         : dernier reboot automatique (anti-boucle)
//   last_pid                   : dernier preset joué (reprise à la mise sous tension)
//   last_host                  : dernière IP de l'enceinte

static void setState(const char *state, const String &msg) {
  {
    Lock l;
    gSt.playerState = state;
    gSt.playerMessage = msg;
  }
  logf("[player] %s", msg.c_str());
}

static bool current(const Cmd &c) { return c.seq == playSeq.load(); }

// Attente découpée : réarme le chien de garde et s'arrête si la demande est remplacée.
static bool pause(uint32_t ms, const Cmd *c = nullptr) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    esp_task_wdt_reset();
    if (c && !current(*c)) return false;
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  return !c || current(*c);
}

static void fill(Cmd &c, int id, const String &name, const String &url, const String &logo) {
  c.id = id;
  strlcpy(c.name, name.c_str(), sizeof c.name);
  strlcpy(c.url, url.c_str(), sizeof c.url);
  strlcpy(c.logo, logo.c_str(), sizeof c.logo);
}

static void enqueue(const Cmd &c) {
  if (xQueueSend(queue, &c, 0) != pdTRUE) logf("File de commandes pleine : commande %d ignorée", c.type);
}

// ---------------------------------------------------------------------------
// Demandes (appelées depuis la boucle WebSocket ou l'API web : jamais bloquantes)
// ---------------------------------------------------------------------------

static void requestPlay(Cmd &c, bool remember) {   // c est modifié (seq)
  Lock l;
  c.type = C_PLAY;
  c.seq = ++playSeq;
  if (remember) { lastItem = c; hasLastItem = true; }
  if (gSt.rebooting) {
    pendingItem = c;
    hasPending = true;
    gSt.playerMessage = String("redémarrage en cours : ") + c.name + " sera joué au retour de l'enceinte";
    logf("[player] %s", gSt.playerMessage.c_str());
    return;
  }
  enqueue(c);
}

bool playerPlayPreset(int n, uint16_t delayMs, bool inferred, const char *origin) {
  Cmd c = {};
  {
    Lock l;
    const PresetCfg &p = gCfg.presets[n];
    if (!p.url.length()) return false;
    fill(c, n, p.name.length() ? p.name : "Preset " + String(n), p.url, p.logo);
    gSt.lastPresetId = n;
    gSt.lastPresetName = c.name;
    gSt.lastPresetAt = isoNow();
    gSt.lastPresetInferred = inferred;
  }
  logf("Preset %d (%s) -> %s", n, origin, c.name);
  persist::putU("last_pid", n);
  c.allowReboot = true;
  c.delayMs = delayMs;
  requestPlay(c, true);
  return true;
}

void playerTest(const String &url, const String &name, const String &logo) {
  Cmd c = {};
  fill(c, 0, name.length() ? name : "Test", url, logo);
  c.allowReboot = false;                // on ne redémarre pas l'enceinte pour une URL de test
  logf("Page web : test de %s", url.c_str());
  requestPlay(c, false);
}

bool playerReboot() {
  Lock l;
  if (gSt.rebooting) return false;
  gSt.rebooting = true;                 // tout de suite : un 2e clic est refusé
  Cmd c = {};
  c.type = C_REBOOT;
  ++playSeq;                            // annule la vérification de lecture en cours
  enqueue(c);
  return true;
}

bool playerRewrite(uint8_t mask) {
  Cmd c = {};
  c.type = C_REWRITE;
  c.mask = mask;
  enqueue(c);
  return true;
}

void playerDiscover() {
  Cmd c = {};
  c.type = C_DISCOVER;
  enqueue(c);
}

void playerOnSpeakerConnected() {
  Cmd c = {};
  c.type = C_CONNECTED;
  enqueue(c);
}

bool playerFastReconnect() { return fastReconnect.load(); }

// ---------------------------------------------------------------------------
// Lecture et watchdog
// ---------------------------------------------------------------------------

static void cacheNowPlaying(const NowPlaying &np) {
  Lock l;
  gSt.np = np;
  gSt.npAtMs = millis();
}

enum WaitResult { WAIT_OK, WAIT_STANDBY, WAIT_FAILED, WAIT_UNREACHABLE, WAIT_SUPERSEDED };

// Interroge /now_playing toutes les 2 s pendant watchdogTimeoutS.
static WaitResult waitPlaying(const Cmd &c, NowPlaying &np) {
  uint32_t timeoutMs;
  { Lock l; timeoutMs = gCfg.watchdogTimeoutS * 1000UL; }
  uint32_t t0 = millis();
  int standby = 0;
  bool answered = false;
  while (millis() - t0 < timeoutMs) {
    if (!pause(2000, &c)) return WAIT_SUPERSEDED;
    NowPlaying cur;
    if (!speaker::nowPlaying(cur)) continue;
    answered = true;
    np = cur;
    cacheNowPlaying(cur);
    if (speaker::isPlayingUpnp(cur)) return WAIT_OK;
    standby = cur.source == "STANDBY" ? standby + 1 : 0;
    if (standby >= 2) return WAIT_STANDBY;
  }
  return answered ? WAIT_FAILED : WAIT_UNREACHABLE;
}

static void markPlaying(const Cmd &c) {
  Lock l;
  gSt.playingId = c.id;
  gSt.playingName = c.name;
  gSt.playingLogo = c.logo;
  gSt.playingUrl = c.url;
}

// Play, vérification, 2e essai (POWER d'abord si l'enceinte est en veille).
static WaitResult startAndVerify(const Cmd &c) {
  bool powerSent = false, metadataFixed = false;
  int attempt = 0;
  uint16_t timeoutS;
  { Lock l; timeoutS = gCfg.watchdogTimeoutS; }
  while (attempt < 2) {
    attempt++;
    if (!current(c)) return WAIT_SUPERSEDED;
    if (!speaker::playUrl(c.url, c.name, c.logo))
      logf("[player] %s : Play UPnP refusé (essai %d)", c.name, attempt);
    NowPlaying np;
    WaitResult r = waitPlaying(c, np);
    if (r == WAIT_OK && np.location == "unplayable location" && !metadataFixed) {
      // Joue, mais l'enceinte a perdu nos métadonnées (pas de nom à l'écran) : on renvoie.
      logf("[player] %s joue sans son nom sur l'enceinte -> nouvel envoi", c.name);
      metadataFixed = true;
      attempt--;
      continue;
    }
    if (r == WAIT_OK) {
      markPlaying(c);
      setState("playing", String(c.name) + " joue (vérifié sur /now_playing, essai " + attempt + ")");
      return WAIT_OK;
    }
    if (r == WAIT_SUPERSEDED || r == WAIT_UNREACHABLE) return r;
    if (r == WAIT_STANDBY && !powerSent) {
      logf("[player] enceinte en veille après le Play -> POWER puis nouveau Play");
      if (!speaker::sendKey("POWER")) logf("[player] touche POWER refusée");
      powerSent = true;
      attempt--;                        // ce tour ne compte pas comme le 2e essai
      if (!pause(3000, &c)) return WAIT_SUPERSEDED;
      continue;
    }
    String st = np.valid ? "source=" + np.source + " playStatus=" + np.playStatus : "pas de réponse";
    if (attempt < 2)
      setState("retrying", String(c.name) + " : pas de lecture après " + timeoutS + " s (" + st +
                               ") -> SetAVTransportURI + Play à nouveau");
    else
      setState("failed", String(c.name) + " : toujours pas de lecture après le 2e essai (" + st + ")");
  }
  return WAIT_FAILED;
}

// Anti-boucle : au plus 1 reboot automatique par rebootMinIntervalS, même à travers un
// redémarrage de l'ESP32 (horodatage NTP si disponible, sinon compteur de démarrages + uptime).
static bool rebootAllowed(uint32_t &agoS) {
  uint32_t minS;
  { Lock l; minS = gCfg.rebootMinIntervalS; }
  uint32_t arEpoch = persist::getU("ar_epoch"), arBoot = persist::getU("ar_boot");
  uint32_t now = epochNow();
  if (lastAutoRebootMs) { agoS = (millis() - lastAutoRebootMs) / 1000; return agoS >= minS; }
  if (arEpoch && now) { agoS = now - arEpoch; return agoS >= minS; }
  if (arBoot && gSt.bootCount - arBoot <= 1) { agoS = millis() / 1000; return agoS >= minS; }
  agoS = 0;
  return true;
}

static bool infoOk() {
  String name, fw;
  return speaker::info(name, fw, 3000);
}

static void rebootAndWait(const char *reason, bool replay) {
  {
    Lock l;
    gSt.rebooting = true;               // les demandes reçues pendant le reboot sont mises de côté
  }
  fastReconnect = true;
  bool back = false;
  setState("rebooting", String("REDÉMARRAGE de l'enceinte (") + reason + ") via le port TAP 17000");
  if (!speaker::reboot()) {
    setState("failed", "commande de redémarrage impossible à envoyer");
  } else {
    uint32_t t0 = millis();
    bool down = false;
    while (millis() - t0 < 90000) {     // 1. l'enceinte doit tomber (~3 s)
      pause(3000);
      bool ws;
      { Lock l; ws = gSt.wsConnected; }
      if (!ws || !infoOk()) { down = true; break; }
    }
    if (!down) {
      setState("failed", "l'enceinte n'a pas redémarré en 90 s : commande ignorée ?");
    } else {
      logf("[player] enceinte arrêtée, attente de son retour (environ 2 min)");
      while (millis() - t0 < 360000) {  // 2. retour : WebSocket reconnecté + /info répond
        pause(5000);
        bool ws;
        { Lock l; ws = gSt.wsConnected; }
        if (ws && infoOk()) { back = true; break; }
      }
      if (back) {
        logf("[player] enceinte revenue après %lu s", (unsigned long)((millis() - t0) / 1000));
        pause(5000);                    // laisser le firmware se stabiliser
      } else {
        setState("failed", "enceinte absente 6 min après le redémarrage");
      }
    }
  }
  fastReconnect = false;
  persist::remove("rp_id");             // l'ESP32 a survécu : il rejoue lui-même
  Cmd target;
  bool play = false;
  {
    Lock l;
    gSt.rebooting = false;
    if (hasPending) { target = pendingItem; play = true; }
    else if (replay && hasLastItem) { target = lastItem; play = true; }
    hasPending = false;
  }
  if (back && play) {
    logf("[player] relecture de %s après le redémarrage", target.name);
    requestPlay(target, false);
  } else if (back) {
    setState("idle", "enceinte revenue après le redémarrage");
  }
}

static void runPlay(const Cmd &c) {
  setState("starting", String("lecture de ") + c.name + " (" + c.url + ")");
  // Après un appui, l'enceinte met ~0,2 s à traiter sa propre sélection : un Play envoyé
  // avant joue sans métadonnées (pas de nom à l'écran). Cf. PROTOCOL.md.
  if (c.delayMs && !pause(c.delayMs, &c)) return;
  bool watchdog, autoReboot;
  { Lock l; watchdog = gCfg.watchdog; autoReboot = gCfg.autoReboot; }
  if (!watchdog) {
    if (speaker::playUrl(c.url, c.name, c.logo)) {
      markPlaying(c);
      setState("playing", String(c.name) + " : Play envoyé (watchdog désactivé)");
    } else {
      setState("failed", String(c.name) + " : Play UPnP refusé");
    }
    return;
  }
  WaitResult r = startAndVerify(c);
  if (r == WAIT_OK || r == WAIT_SUPERSEDED) return;
  if (r == WAIT_UNREACHABLE) { setState("failed", String(c.name) + " : enceinte injoignable"); return; }
  if (!c.allowReboot) { setState("failed", String(c.name) + " : pas de lecture (pas de reboot pour un test)"); return; }
  if (!autoReboot) { setState("failed", String(c.name) + " : pas de lecture (auto_reboot désactivé)"); return; }
  if (!speaker::streamReachable(c.url)) {
    setState("failed", String(c.name) + " : le flux lui-même ne répond pas -> flux mort, pas de reboot");
    return;
  }
  uint32_t ago;
  if (!rebootAllowed(ago)) {
    setState("failed", String(c.name) + " : pas de lecture, mais dernier reboot automatique il y a " +
                           ago + " s : anti-boucle, pas de reboot");
    return;
  }
  if (!current(c)) return;
  // Noté AVANT le reboot : si l'enceinte coupe l'alimentation de l'ESP32, il rejouera au démarrage.
  lastAutoRebootMs = millis() ? millis() : 1;
  persist::putU("ar_epoch", epochNow());
  persist::putU("ar_boot", gSt.bootCount);
  if (c.id >= 1 && c.id <= 6) {
    persist::putU("rp_id", c.id);
    persist::putU("rp_epoch", epochNow());
    persist::putU("rp_boot", gSt.bootCount);
  }
  { Lock l; gSt.lastAutoReboot = isoNow(); }
  rebootAndWait("watchdog : moteur de lecture bloqué", true);
}

// ---------------------------------------------------------------------------
// Presets de l'enceinte
// ---------------------------------------------------------------------------

struct Wanted { String source, type, location, account; };

static Wanted wanted(const String &method, const PresetCfg &p) {
  if (method == "lir_direct") return {"LOCAL_INTERNET_RADIO", "stationurl", p.url, ""};
  return {"UPNP", "", p.url, "UPnPUserName"};
}

static void updateCloudCache(const std::vector<SpeakerPreset> &ps) {
  Lock l;
  gSt.cloudPresetsMask = 0;
  for (int n = 1; n <= 6; n++) gSt.cloudLocations[n] = "";
  for (auto &p : ps)
    if (speaker::isCloud(p)) {
      gSt.cloudPresetsMask |= 1 << p.id;
      gSt.cloudLocations[p.id] = p.location;
    }
}

static String jsonEscape(const String &s) {
  String o;
  for (unsigned i = 0; i < s.length(); i++) {
    char ch = s[i];
    if (ch == '"' || ch == '\\') { o += '\\'; o += ch; }
    else if ((uint8_t)ch < 0x20) o += ' ';
    else o += ch;
  }
  return o;
}

// Réécrit les presets du masque. 2 à 6 d'abord, chacun vérifié par GET /presets ;
// le preset 1 en dernier et seulement si tous les autres ont réussi.
static void doRewrite(uint8_t mask, const char *reason) {
  String method;
  PresetCfg cfg[7];
  {
    Lock l;
    method = gCfg.method;
    for (int n = 1; n <= 6; n++) cfg[n] = gCfg.presets[n];
  }
  String results;
  bool allOk = true, any = false;
  if (!methodValid(method)) {
    logf("Réécriture impossible : preset_write_method = %s", method.c_str());
    Lock l;
    gSt.lastRewriteJson = "{\"at\":\"" + isoNow() + "\",\"ok\":false,\"reason\":\"" + reason +
                          "\",\"error\":\"method_not_validated\",\"results\":{}}";
    return;
  }
  if (!mask) for (int n = 1; n <= 6; n++) if (cfg[n].url.length()) mask |= 1 << n;
  logf("Réécriture des presets de l'enceinte (masque %02x, méthode %s, %s)", mask, method.c_str(), reason);
  int order[6] = {2, 3, 4, 5, 6, 1};
  for (int n : order) {
    if (!(mask & (1 << n))) continue;
    esp_task_wdt_reset();
    any = true;
    bool ok = false;
    String msg;
    if (!cfg[n].url.length()) {
      msg = "not configured";
    } else if (n == 1 && !allOk) {
      msg = "skipped: another preset failed, preset 1 left untouched";
      logf("Preset 1 NON réécrit : un autre preset a échoué");
    } else {
      Wanted w = wanted(method, cfg[n]);
      String name = cfg[n].name.length() ? cfg[n].name : "Preset " + String(n);
      int code = speaker::storePreset(n, w.source, w.type, w.location, w.account, name);
      std::vector<SpeakerPreset> ps;
      bool read = speaker::presets(ps);
      if (read) updateCloudCache(ps);
      for (auto &p : ps)
        if (p.id == n) ok = code == 200 && p.source == w.source && p.location == w.location;
      msg = ok ? "written" : "HTTP " + String(code) + (read ? ", contenu non vérifié" : ", /presets illisible");
    }
    logf("Preset %d de l'enceinte <- %s : %s", n, cfg[n].name.c_str(), msg.c_str());
    if (!ok) allOk = false;
    if (results.length()) results += ",";
    results += "\"" + String(n) + "\":{\"ok\":" + (ok ? "true" : "false") + ",\"message\":\"" + jsonEscape(msg) + "\"}";
  }
  Lock l;
  gSt.lastRewriteJson = "{\"at\":\"" + isoNow() + "\",\"ok\":" + (allOk && any ? "true" : "false") +
                        ",\"reason\":\"" + reason + "\",\"method\":\"" + method + "\",\"results\":{" + results + "}}";
}

// Après chaque connexion WebSocket : nom de l'enceinte, cache des presets, réécriture si besoin.
static void onConnected() {
  String name, fw;
  if (speaker::info(name, fw, 5000)) {
    logf("Enceinte : %s, firmware %s", name.c_str(), fw.c_str());
    Lock l;
    gSt.speakerName = name;
  }
  std::vector<SpeakerPreset> ps;
  if (!speaker::presets(ps)) { logf("Presets de l'enceinte illisibles"); return; }
  updateCloudCache(ps);
  uint8_t present = 0, todo = 0;
  String method;
  bool rewrite;
  PresetCfg cfg[7];
  {
    Lock l;
    method = gCfg.method;
    rewrite = gCfg.rewriteOnStartup;
    for (int n = 1; n <= 6; n++) cfg[n] = gCfg.presets[n];
  }
  for (auto &p : ps) {
    logf("Preset %d de l'enceinte : %s %s", p.id, p.source.c_str(), p.location.substring(0, 80).c_str());
    present |= 1 << p.id;
    if (!cfg[p.id].url.length()) continue;
    if (speaker::isCloud(p)) { todo |= 1 << p.id; continue; }
    if (methodValid(method)) {
      Wanted w = wanted(method, cfg[p.id]);
      if (p.source != w.source || p.location != w.location) todo |= 1 << p.id;
    }
  }
  for (int n = 1; n <= 6; n++)
    if (!(present & (1 << n)) && cfg[n].url.length()) todo |= 1 << n;
  if (!rewrite || !todo) return;
  if (!methodValid(method)) {
    logf("Presets encore sur le cloud Bose, mais preset_write_method = %s : non réécrits", method.c_str());
    return;
  }
  doRewrite(todo, "startup check");
}

// ---------------------------------------------------------------------------
// Découverte de l'enceinte
// ---------------------------------------------------------------------------

static void discoverNow(const char *why) {
  lastDiscoverMs = millis();
  String mac, ip, found;
  { Lock l; mac = gCfg.speakerMac; }
  if (speaker::discover(mac, ip, found)) {
    bool changed = ip != speaker::host();
    logf("Découverte SSDP (%s) : enceinte %s trouvée en %s%s", why, found.c_str(), ip.c_str(),
         changed ? " (nouvelle adresse)" : "");
    if (mac.length() && !found.equalsIgnoreCase(mac))
      logf("Attention : MAC %s différente de celle configurée (%s)", found.c_str(), mac.c_str());
    { Lock l; gSt.speakerDiscovered = true; gSt.speakerHost = ip; }
    persist::putS("last_host", ip);
    if (changed) {
      speaker::setHost(ip);
      eventsChangeHost(ip);
    }
  } else {
    logf("Découverte SSDP (%s) : aucune enceinte Bose n'a répondu, on garde %s", why, speaker::host().c_str());
  }
}

// ---------------------------------------------------------------------------
// Démarrage de l'ESP32 : rejouer après un reboot de l'enceinte, ou reprise à la mise sous tension
// ---------------------------------------------------------------------------

static bool waitSpeaker(uint32_t maxMs) {
  uint32_t t0 = millis();
  while (millis() - t0 < maxMs) {
    if (infoOk()) return true;
    pause(5000);
  }
  return false;
}

static void bootActions() {
  // 1. Un preset était à rejouer (l'ESP32 a été coupé par le reboot de l'enceinte) ?
  uint32_t rpId = persist::getU("rp_id");
  if (rpId >= 1 && rpId <= 6) {
    uint32_t rpEpoch = persist::getU("rp_epoch"), rpBoot = persist::getU("rp_boot");
    uint32_t now = epochNow();
    bool fresh = (rpEpoch && now) ? now - rpEpoch < 300 : gSt.bootCount - rpBoot == 1;
    persist::remove("rp_id");
    if (fresh) {
      logf("Démarrage après un reboot de l'enceinte : attente de l'enceinte puis relecture du preset %lu",
           (unsigned long)rpId);
      { Lock l; gSt.rebooting = true; hasPending = false; }
      bool ok = waitSpeaker(300000);
      if (ok) pause(5000);
      Cmd pending;
      bool usePending;
      {
        Lock l;
        gSt.rebooting = false;
        usePending = hasPending;        // un appui reçu pendant l'attente passe avant
        pending = pendingItem;
        hasPending = false;
      }
      if (!ok) logf("Enceinte toujours absente après 5 min : pas de relecture");
      else if (usePending) requestPlay(pending, false);
      else playerPlayPreset(rpId, 0, false, "relecture après reboot");
      return;
    }
    logf("Preset %lu à rejouer trop ancien : ignoré", (unsigned long)rpId);
  }
  // 2. Mise sous tension de l'ESP32 alors que l'enceinte est allumée mais ne joue rien :
  //    l'enceinte vient d'être réveillée (appui sur un preset ou POWER) et l'a allumé en même
  //    temps ; l'événement de l'appui a été perdu. On joue le dernier preset connu.
  bool resume;
  { Lock l; resume = gCfg.resumeOnPowerOn; }
  if (resume && esp_reset_reason() == ESP_RST_POWERON) {
    NowPlaying np;
    if (speaker::nowPlaying(np, 3000)) {
      cacheNowPlaying(np);
      if (np.source == "INVALID_SOURCE") {
        int pid = persist::getU("last_pid", 1);
        logf("Mise sous tension avec l'enceinte allumée et muette : reprise du preset %d", pid);
        playerPlayPreset(pid, 0, false, "reprise à la mise sous tension");
      } else {
        logf("Mise sous tension : enceinte en %s, rien à reprendre", np.source.c_str());
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Boucle de la tâche
// ---------------------------------------------------------------------------

static void idleWork() {
  uint32_t now = millis();
  bool ws, rebooting;
  uint32_t lastReq, npAt;
  {
    Lock l;
    ws = gSt.wsConnected;
    rebooting = gSt.rebooting;
    lastReq = gSt.lastStatusRequestMs;
    npAt = gSt.npAtMs;
  }
  // Cache /now_playing rafraîchi seulement si la page web est ouverte
  if (lastReq && now - lastReq < 15000 && now - npAt > 3000) {
    NowPlaying np;
    speaker::nowPlaying(np, 2000);
    cacheNowPlaying(np);
  }
  // Enceinte injoignable depuis 90 s hors reboot : elle a peut-être changé d'IP
  if (ws || rebooting) wsDownSinceMs = 0;
  else if (!wsDownSinceMs) wsDownSinceMs = now;
  else if (now - wsDownSinceMs > 90000 && now - lastDiscoverMs > 60000) discoverNow("enceinte injoignable");
}

static void playerTask(void *) {
  esp_task_wdt_add(nullptr);
  discoverNow("démarrage");
  bootActions();
  Cmd c;
  for (;;) {
    esp_task_wdt_reset();
    if (xQueueReceive(queue, &c, pdMS_TO_TICKS(1000)) != pdTRUE) {
      idleWork();
      continue;
    }
    switch (c.type) {
      case C_PLAY: if (current(c)) runPlay(c); break;
      case C_REBOOT: rebootAndWait("manuel", false); break;
      case C_REWRITE: doRewrite(c.mask, c.mask ? "preset edited" : "web page"); break;
      case C_DISCOVER: discoverNow("demande"); break;
      case C_CONNECTED: onConnected(); break;
    }
  }
}

void playerBegin() {
  queue = xQueueCreate(6, sizeof(Cmd));
  xTaskCreatePinnedToCore(playerTask, "player", 12288, nullptr, 1, nullptr, 1);
}
