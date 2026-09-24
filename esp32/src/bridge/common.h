// État partagé, réglages (NVS), journal et heure : utilisés par toutes les parties du bridge.
//
// Trois contextes d'exécution se partagent ces données :
//   - la boucle Arduino (loop) : client WebSocket vers l'enceinte, bouton BOOT ;
//   - la tâche « player » : tous les appels HTTP bloquants vers l'enceinte ;
//   - la tâche async_tcp : serveur web et API (ne doit jamais bloquer).
// Toute lecture/écriture de gCfg ou gSt se fait sous verrou (objet Lock).
#pragma once

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include "parse.h"   // NowPlaying, SpeakerPreset

#define BRIDGE_VERSION "esp32-1.0.1"

static const int MAX_URL = 512;     // longueur max d'une URL de flux ou de logo
static const int MAX_NAME = 64;     // longueur max d'un nom de station

// ---------------------------------------------------------------------------
// Verrou global (récursif) protégeant gCfg et gSt
// ---------------------------------------------------------------------------
extern SemaphoreHandle_t gLock;
struct Lock {
  Lock() { xSemaphoreTakeRecursive(gLock, portMAX_DELAY); }
  ~Lock() { xSemaphoreGiveRecursive(gLock); }
};

// ---------------------------------------------------------------------------
// Réglages, persistés en NVS (Preferences, espace "cfg")
// ---------------------------------------------------------------------------
struct PresetCfg {
  String name, url, logo;
};

struct Settings {
  PresetCfg presets[7];                 // indices 1 à 6
  String hostFallback;                  // IP de secours si la découverte échoue
  String speakerMac;                    // MAC préférée lors de la découverte SSDP
  String catalogUrl;
  String method;                        // upnp | lir_direct | none
  float debounceS;
  float eventDelayS;                    // attente après un appui avant le Play (0,7 s, cf. PROTOCOL.md)
  uint16_t watchdogTimeoutS;
  bool watchdog;
  bool autoReboot;
  uint32_t rebootMinIntervalS;
  bool rewriteOnStartup;
  bool resumeOnPowerOn;                 // cf. README : ESP32 alimenté par l'enceinte
  String otaPassword;
};
extern Settings gCfg;

void settingsLoad();                    // lit la NVS (valeurs par défaut = config.yaml)
void settingsSavePreset(int n);
void settingsSaveAll();
bool methodValid(const String &m);      // upnp ou lir_direct

// État persistant hors réglages (espace NVS "state")
namespace persist {
uint32_t getU(const char *key, uint32_t def = 0);
void putU(const char *key, uint32_t v);
String getS(const char *key, const String &def = "");
void putS(const char *key, const String &v);
void remove(const char *key);
}  // namespace persist

// ---------------------------------------------------------------------------
// État courant, exposé par /api/status
// ---------------------------------------------------------------------------
struct Status {
  String speakerHost;                   // IP utilisée actuellement
  String speakerName = "Bose";          // nom lu sur /info
  bool speakerDiscovered = false;       // trouvée par SSDP (sinon IP de secours / dernière connue)
  bool wsConnected = false;
  NowPlaying np;
  uint32_t npAtMs = 0;
  int lastPresetId = 0;
  String lastPresetName, lastPresetAt;
  bool lastPresetInferred = false;
  int playingId = -1;                   // -1 = rien ; 0 = test
  String playingName, playingLogo, playingUrl;
  String playerState = "idle";          // idle|starting|playing|retrying|rebooting|failed
  String playerMessage;
  String lastAutoReboot;                // horodatage ISO ou vide
  bool rebooting = false;
  uint8_t cloudPresetsMask = 0;         // bit n = preset n encore sur le cloud Bose
  String cloudLocations[7];             // location des presets cloud (repli nowPlayingUpdated)
  String lastRewriteJson;               // objet JSON du dernier résultat de réécriture
  uint32_t lastStatusRequestMs = 0;
  String resetReason;
  uint32_t bootCount = 0;
};
extern Status gSt;

// ---------------------------------------------------------------------------
// Journal : Serial + tampon circulaire (~200 lignes) lu par /api/log
// ---------------------------------------------------------------------------
void logBegin();                        // crée aussi gLock : à appeler en premier
void logf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
// Écrit les lignes du journal (de la plus ancienne à la plus récente) dans un tableau JSON.
class Print;
void logWriteJsonArray(Print &out);

// ---------------------------------------------------------------------------
// Heure (NTP) : l'horodatage n'est fiable qu'après synchronisation
// ---------------------------------------------------------------------------
bool timeValid();
uint32_t epochNow();                    // 0 si l'heure est inconnue
String isoNow();                        // "2026-09-24T13:05:22" ou "+123s" (uptime) sans NTP

String resetReasonText();
