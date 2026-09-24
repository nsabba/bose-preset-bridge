/*
 * bose-preset-bridge - version ESP32 (jalons 1 à 4).
 *
 * Même comportement que la V2 Python (bridge.py) : écoute les appuis preset d'une Bose
 * SoundTouch (WebSocket 8080), joue le flux par UPnP (8091), vérifie la lecture sur
 * /now_playing, redémarre l'enceinte si son moteur de lecture est bloqué (TAP 17000),
 * réécrit les presets de l'enceinte et sert la même page web et la même API.
 * Détail de chaque requête : PROTOCOL.md à la racine du dépôt.
 *
 * Organisation :
 *   loop()        client WebSocket (events.cpp), Wi-Fi, bouton BOOT
 *   tâche player  requêtes bloquantes vers l'enceinte (player.cpp, speaker.cpp)
 *   async_tcp     serveur web et API (web.cpp)
 */
#include <Arduino.h>
#include <esp_task_wdt.h>

#include "common.h"
#include "events.h"
#include "netsetup.h"
#include "player.h"
#include "speaker.h"
#include "web.h"

static const int WDT_TIMEOUT_S = 30;    // l'ESP32 redémarre seul s'il se fige plus de 30 s

void setup() {
  Serial.begin(115200);
  delay(100);
  logBegin();
  settingsLoad();

  gSt.bootCount = persist::getU("boots") + 1;
  persist::putU("boots", gSt.bootCount);
  gSt.resetReason = resetReasonText();
  logf("bose-preset-bridge %s, démarrage n°%lu, cause du reset : %s", BRIDGE_VERSION,
       (unsigned long)gSt.bootCount, gSt.resetReason.c_str());

  netBegin();                           // peut bloquer (portail) : avant le chien de garde

  // Enceinte : dernière IP connue, sinon IP de secours ; la tâche player lance la découverte SSDP.
  String host = persist::getS("last_host", "");
  if (!host.length()) { Lock l; host = gCfg.hostFallback; }
  speaker::setHost(host);

  esp_task_wdt_init(WDT_TIMEOUT_S, true);
  esp_task_wdt_add(nullptr);            // tâche loop
  webBegin();
  playerBegin();
  eventsBegin();
}

void loop() {
  esp_task_wdt_reset();
  eventsLoop();
  netLoop();
  if (gRestartAtMs && (int32_t)(millis() - gRestartAtMs) > 0) {
    logf("Redémarrage de l'ESP32");
    delay(100);
    ESP.restart();
  }
  delay(1);
}
