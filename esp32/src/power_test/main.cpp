/*
 * Jalon 0 - firmware de test d'alimentation.
 *
 * Question à trancher sur place : le port USB de service de la Bose alimente-t-il l'ESP32
 * quand l'enceinte est en veille ?
 *
 * Le firmware compte ses démarrages (NVS), note la cause du dernier reset et, toutes les
 * 30 s, enregistre en NVS sa durée de fonctionnement et l'état de l'enceinte
 * (/now_playing : STANDBY, UPNP...). Après une coupure, la page montre donc combien de
 * temps la session précédente a duré et dans quel état était l'enceinte juste avant.
 *
 *   http://bose-bridge.local/   (ou l'IP affichée sur le moniteur série)
 *   http://bose-bridge.local/api/status   même chose en JSON
 */

#include <Arduino.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <esp_system.h>
#include <time.h>

static const char *HOSTNAME = "bose-bridge";
static const char *AP_NAME = "Bose-Bridge-Setup";
static const int HISTORY = 10;          // nombre de démarrages gardés dans l'historique
static const uint32_t SAVE_EVERY_MS = 30000;

WebServer server(80);
Preferences prefs;

uint32_t bootCount = 0;
String resetReason;
String speakerHost = "192.168.1.17";
String speakerState = "?";               // dernier état lu sur /now_playing
uint32_t lastSave = 0, lastPoll = 0;

// Une ligne d'historique par démarrage : "boot|heure|cause|durée session précédente|état enceinte avant coupure"
String history[HISTORY];

// Cause du reset, en clair
static String resetReasonText(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:  return "POWERON (mise sous tension)";
    case ESP_RST_BROWNOUT: return "BROWNOUT (tension trop faible)";
    case ESP_RST_SW:       return "SW (redémarrage logiciel)";
    case ESP_RST_PANIC:    return "PANIC (plantage)";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:      return "WDT (chien de garde)";
    case ESP_RST_DEEPSLEEP:return "DEEPSLEEP";
    case ESP_RST_EXT:      return "EXT (broche reset)";
    default:               return "AUTRE (" + String((int)r) + ")";
  }
}

static String nowText() {
  time_t t = time(nullptr);
  if (t < 1700000000) return "heure inconnue";
  char buf[24];
  strftime(buf, sizeof buf, "%d/%m %H:%M:%S", localtime(&t));
  return buf;
}

static String duration(uint32_t s) {
  char buf[32];
  snprintf(buf, sizeof buf, "%uj %02uh%02um%02us", s / 86400, (s / 3600) % 24, (s / 60) % 60, s % 60);
  return buf;
}

// Lit la source de /now_playing (STANDBY, UPNP, INVALID_SOURCE...) ; "injoignable" sinon.
static String pollSpeaker() {
  HTTPClient http;
  http.setTimeout(3000);
  if (!http.begin("http://" + speakerHost + ":8090/now_playing")) return "injoignable";
  int code = http.GET();
  String out = "injoignable";
  if (code == 200) {
    String body = http.getString();
    int i = body.indexOf("source=\"");
    if (i >= 0) {
      int j = body.indexOf('"', i + 8);
      out = body.substring(i + 8, j);
      int p = body.indexOf("<playStatus>");
      if (p >= 0) out += " " + body.substring(p + 12, body.indexOf("</playStatus>", p));
    }
  } else if (code > 0) {
    out = "HTTP " + String(code);
  }
  http.end();
  return out;
}

static void loadHistory() {
  for (int i = 0; i < HISTORY; i++) history[i] = prefs.getString(("h" + String(i)).c_str(), "");
}

static void pushHistory(const String &line) {
  for (int i = HISTORY - 1; i > 0; i--) history[i] = history[i - 1];
  history[0] = line;
  for (int i = 0; i < HISTORY; i++) prefs.putString(("h" + String(i)).c_str(), history[i]);
}

static String json() {
  String s = "{";
  s += "\"uptime_s\":" + String(millis() / 1000);
  s += ",\"boot_count\":" + String(bootCount);
  s += ",\"reset_reason\":\"" + resetReason + "\"";
  s += ",\"speaker_host\":\"" + speakerHost + "\"";
  s += ",\"speaker_state\":\"" + speakerState + "\"";
  s += ",\"rssi\":" + String(WiFi.RSSI());
  s += ",\"free_heap\":" + String(ESP.getFreeHeap());
  s += ",\"history\":[";
  for (int i = 0; i < HISTORY; i++) {
    if (!history[i].length()) break;
    if (i) s += ",";
    s += "\"" + history[i] + "\"";
  }
  return s + "]}";
}

static void handleRoot() {
  String h = F("<!DOCTYPE html><html lang=fr><meta charset=utf-8>"
               "<meta name=viewport content='width=device-width,initial-scale=1'>"
               "<meta http-equiv=refresh content=10><title>Test alimentation</title>"
               "<style>body{font:17px -apple-system,sans-serif;margin:16px;max-width:640px}"
               "td,th{padding:4px 8px;border-bottom:1px solid #ccc;text-align:left;font-size:14px}"
               "b.big{font-size:2em}</style><h1>Test d'alimentation</h1>");
  h += "<p>Allumé depuis<br><b class=big>" + duration(millis() / 1000) + "</b></p>";
  h += "<p>Démarrages : <b>" + String(bootCount) + "</b><br>Dernier reset : <b>" + resetReason + "</b><br>";
  h += "Enceinte " + speakerHost + " : <b>" + speakerState + "</b><br>";
  h += "Wi-Fi : " + String(WiFi.RSSI()) + " dBm · heure : " + nowText() + "</p>";
  h += F("<h2>Historique des démarrages</h2><table><tr><th>#</th><th>Démarré le</th><th>Cause</th>"
         "<th>Durée de la session précédente</th><th>Enceinte juste avant</th></tr>");
  for (int i = 0; i < HISTORY; i++) {
    if (!history[i].length()) break;
    h += "<tr><td>" + history[i];
    h.replace("|", "</td><td>");
    h += "</td></tr>";
  }
  h += F("</table><p>La page se rafraîchit toutes les 10 s. Mettre l'enceinte en veille, attendre "
         "5 min, la rallumer : si le compteur de démarrages n'a pas bougé, l'ESP32 reste alimenté en veille.</p>");
  server.send(200, "text/html; charset=utf-8", h);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  prefs.begin("powertest", false);

  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  resetReason = resetReasonText(esp_reset_reason());
  speakerHost = prefs.getString("speaker", speakerHost);
  uint32_t prevUptime = prefs.getUInt("sess_up", 0);
  String prevSpeaker = prefs.getString("sess_spk", "?");
  loadHistory();
  Serial.printf("\n[power_test] démarrage n°%u, cause %s, session précédente %s, enceinte avant : %s\n",
                bootCount, resetReason.c_str(), duration(prevUptime).c_str(), prevSpeaker.c_str());

  // Wi-Fi : portail "Bose-Bridge-Setup" si aucun réseau n'est encore enregistré
  WiFiManager wm;
  WiFiManagerParameter pSpeaker("speaker", "IP de l'enceinte", speakerHost.c_str(), 40);
  wm.addParameter(&pSpeaker);
  wm.setConfigPortalTimeout(300);
  wm.setHostname(HOSTNAME);
  if (!wm.autoConnect(AP_NAME)) {
    Serial.println("[power_test] pas de Wi-Fi, redémarrage");
    ESP.restart();
  }
  if (strlen(pSpeaker.getValue()) && speakerHost != pSpeaker.getValue()) {
    speakerHost = pSpeaker.getValue();
    prefs.putString("speaker", speakerHost);
  }
  Serial.printf("[power_test] connecté, IP %s, RSSI %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());

  configTzTime("CET-1CEST,M3.5.0,M10.5.0/3", "pool.ntp.org", "time.google.com");
  for (int i = 0; i < 50 && time(nullptr) < 1700000000; i++) delay(100);

  pushHistory(String(bootCount) + "|" + nowText() + "|" + resetReason + "|" + duration(prevUptime) + "|" + prevSpeaker);
  prefs.putUInt("sess_up", 0);

  MDNS.begin(HOSTNAME);
  MDNS.addService("http", "tcp", 80);
  server.on("/", handleRoot);
  server.on("/api/status", [] { server.send(200, "application/json", json()); });
  server.on("/speaker", [] {   // /speaker?ip=192.168.1.10 pour changer l'IP de l'enceinte
    if (server.hasArg("ip")) { speakerHost = server.arg("ip"); prefs.putString("speaker", speakerHost); }
    server.sendHeader("Location", "/");
    server.send(302);
  });
  server.begin();
  speakerState = pollSpeaker();
}

void loop() {
  server.handleClient();
  uint32_t now = millis();
  if (now - lastPoll > 15000) {
    lastPoll = now;
    speakerState = pollSpeaker();
  }
  if (now - lastSave > SAVE_EVERY_MS) {
    lastSave = now;
    prefs.putUInt("sess_up", now / 1000);
    prefs.putString("sess_spk", speakerState + " (" + nowText() + ")");
  }
  delay(2);
}
