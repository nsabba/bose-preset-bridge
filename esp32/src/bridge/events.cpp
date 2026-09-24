// Client WebSocket "gabbo" : même règles que BoseListener dans bridge.py
//   - déclencheur : <nowSelectionUpdated> avec <preset id="1..6"> (id="0" ignoré) ;
//   - anti-rebond : même preset < debounce_seconds (3 s) après le précédent -> ignoré ;
//   - repli : <nowPlayingUpdated> portant la location d'un preset encore sur le cloud Bose ;
//   - reconnexion : 5 s, doublé jusqu'à 60 s, remis à 5 s après chaque connexion réussie,
//     plafonné à 5 s pendant un reboot de l'enceinte.
#include "events.h"

#include <WebSocketsClient.h>

#include "common.h"
#include "player.h"
#include "speaker.h"
#include "xmlmini.h"

static WebSocketsClient ws;
static const uint32_t DELAY_MIN = 5000, DELAY_MAX = 60000;
static uint32_t delayMs = DELAY_MIN;
static uint32_t lastPressMs[7] = {0};
static String pendingHost;              // changement d'IP demandé par la tâche player
static bool wasConnected = false;
static uint32_t failures = 0;

static void press(int id, bool inferred) {
  uint32_t now = millis();
  uint32_t window;
  { Lock l; window = inferred ? 15000 : (uint32_t)(gCfg.debounceS * 1000); }
  if (lastPressMs[id] && now - lastPressMs[id] < window) {
    logf("Preset %d : événement en double %.1f s après le précédent -> ignoré", id, (now - lastPressMs[id]) / 1000.0);
    return;
  }
  lastPressMs[id] = now;
  uint16_t delay;
  { Lock l; delay = (uint16_t)(gCfg.eventDelayS * 1000); }
  if (!playerPlayPreset(id, delay, inferred, inferred ? "déduit du contenu cloud lancé" : "télécommande"))
    logf("Preset %d appuyé mais non configuré : ignoré", id);
}

static void onMessage(const String &msg) {
  parse::Event e = parse::event(msg);
  const String &tag = e.tag;
  if (tag == "nowSelectionUpdated") {
    int id = e.presetId;
    logf("WS nowSelectionUpdated preset=%d", id);
    if (id >= 1 && id <= 6) press(id, false);
  } else if (tag == "nowPlayingUpdated") {
    const String &loc = e.location;
    if (!loc.length()) return;
    int found = 0;
    {
      Lock l;
      for (int n = 1; n <= 6; n++)
        if (gSt.cloudLocations[n].length() && gSt.cloudLocations[n] == loc) found = n;
    }
    if (found) press(found, true);   // jamais pour un contenu non cloud (boucle sinon)
  } else if (tag == "errorUpdate") {
    logf("WS errorUpdate %s", e.error.c_str());
  } else if (tag != "volumeUpdated" && tag != "userActivityUpdate" && tag != "SoundTouchSdkInfo" &&
             tag != "connectionStateUpdated") {
    Serial.printf("WS %s\n", tag.c_str());   // événements bavards : console série seulement
  }
}

static void setDelay(uint32_t ms) {
  delayMs = ms;
  uint32_t effective = playerFastReconnect() ? min(ms, DELAY_MIN) : ms;
  ws.setReconnectInterval(effective);
}

static void onEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      logf("Connecté à l'enceinte (%s)", speaker::host().c_str());
      { Lock l; gSt.wsConnected = true; }
      wasConnected = true;
      failures = 0;
      setDelay(DELAY_MIN);               // délai remis à zéro après une connexion réussie
      playerOnSpeakerConnected();
      break;
    case WStype_DISCONNECTED:
      { Lock l; gSt.wsConnected = false; }
      if (wasConnected) logf("WebSocket fermé");
      wasConnected = false;
      setDelay(min(delayMs * 2, DELAY_MAX));
      if (++failures <= 6 || failures % 30 == 0)
        logf("WebSocket : nouvel essai dans %lu s", (unsigned long)(playerFastReconnect() ? min(delayMs, DELAY_MIN) : delayMs) / 1000);
      break;
    case WStype_TEXT:
      onMessage(String((const char *)payload, length));
      break;
    default:
      break;
  }
}

static void connect(const String &host) {
  ws.begin(host, 8080, "/", "gabbo");
  ws.onEvent(onEvent);
  ws.setReconnectInterval(delayMs);
  ws.enableHeartbeat(30000, 10000, 1);   // ping toutes les 30 s, pong attendu en 10 s
}

void eventsBegin() {
  logf("Connexion à ws://%s:8080 ...", speaker::host().c_str());
  connect(speaker::host());
}

void eventsChangeHost(const String &host) {
  Lock l;
  pendingHost = host;
}

void eventsLoop() {
  String h;
  {
    Lock l;
    h = pendingHost;
    pendingHost = "";
  }
  if (h.length()) {
    logf("WebSocket : changement d'adresse de l'enceinte -> %s", h.c_str());
    ws.disconnect();
    delayMs = DELAY_MIN;
    connect(h);
  }
  ws.loop();
}
