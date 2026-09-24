// Voir netsetup.h.
#include "netsetup.h"

#include <ESPmDNS.h>
#include <WiFi.h>
#include <WiFiManager.h>

#include "common.h"

static const char *HOSTNAME = "bose-bridge";
static const char *AP_NAME = "Bose-Bridge-Setup";
static const int BOOT_PIN = 0;          // bouton BOOT de la carte

static void portal() {
  WiFiManager wm;
  String pw, host;
  { Lock l; pw = gCfg.otaPassword; host = gCfg.hostFallback; }
  WiFiManagerParameter pOta("ota_pw", "Mot de passe des mises à jour (OTA)", pw.c_str(), 32, "type='password'");
  WiFiManagerParameter pHost("speaker", "IP de secours de l'enceinte", host.c_str(), 40);
  wm.addParameter(&pOta);
  wm.addParameter(&pHost);
  wm.setHostname(HOSTNAME);
  wm.setTitle("Bose Bridge");
  wm.setShowInfoUpdate(false);          // la mise à jour se fait depuis la page du bridge
  logf("Portail Wi-Fi : se connecter au réseau « %s » (192.168.4.1)", AP_NAME);
  bool ok = wm.autoConnect(AP_NAME);
  {
    Lock l;
    if (strlen(pOta.getValue())) gCfg.otaPassword = pOta.getValue();
    IPAddress ip;
    if (ip.fromString(pHost.getValue())) gCfg.hostFallback = pHost.getValue();
  }
  settingsSaveAll();
  if (!ok) {
    logf("Portail fermé sans connexion : redémarrage");
    ESP.restart();
  }
}

void netBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);                 // latence minimale pour le WebSocket
  pinMode(BOOT_PIN, INPUT_PULLUP);

  WiFiManager wm;
  bool saved = wm.getWiFiIsSaved();
  // NB : BOOT maintenu PENDANT la mise sous tension met l'ESP32 en mode programmation ;
  // pour rouvrir le portail, on maintient BOOT 5 s une fois la carte démarrée (netLoop).
  if (!saved) {
    portal();                           // premier démarrage, ou Wi-Fi effacé : portail
  } else {
    // Wi-Fi déjà configuré : on n'ouvre JAMAIS le portail tout seul (une box redémarrée ne
    // doit pas bloquer le bridge), on attend et la reconnexion automatique fait le reste.
    WiFi.begin();
    for (int i = 0; i < 200 && WiFi.status() != WL_CONNECTED; i++) delay(100);
  }
  if (WiFi.status() == WL_CONNECTED)
    logf("Wi-Fi « %s » connecté, IP %s, %d dBm", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());
  else
    logf("Wi-Fi pas encore connecté : nouvelle tentative en arrière-plan");

  MDNS.begin(HOSTNAME);
  MDNS.addService("http", "tcp", 80);
  configTzTime("CET-1CEST,M3.5.0,M10.5.0/3", "pool.ntp.org", "time.google.com");
  for (int i = 0; i < 30 && !timeValid(); i++) delay(100);   // 3 s max : l'heure n'est pas indispensable
}

void netLoop() {
  static bool wasConnected = true;
  static uint32_t pressedSince = 0;
  bool connected = WiFi.status() == WL_CONNECTED;
  if (connected != wasConnected) {
    if (connected) logf("Wi-Fi reconnecté, IP %s", WiFi.localIP().toString().c_str());
    else logf("Wi-Fi perdu");
    wasConnected = connected;
  }
  // BOOT maintenu 5 s : efface le Wi-Fi et redémarre sur le portail
  if (digitalRead(BOOT_PIN) == LOW) {
    if (!pressedSince) pressedSince = millis();
    if (millis() - pressedSince > 5000) {
      logf("Bouton BOOT maintenu 5 s : effacement du Wi-Fi, redémarrage sur le portail");
      WiFiManager wm;
      wm.resetSettings();
      delay(500);
      ESP.restart();
    }
  } else {
    pressedSince = 0;
  }
}
