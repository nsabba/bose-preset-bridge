// API HTTP : mêmes routes et mêmes formats JSON que webui.py (V2).
// Différences, toutes rétrocompatibles avec la page :
//   - PUT /api/presets/{n} répond "speaker": "pending" : l'écriture dans l'enceinte se fait
//     ensuite dans la tâche player ; la page suit le résultat via /api/status (last_rewrite) ;
//   - POST /api/rewrite-speaker-presets répond 202 {"ok":true,"pending":true} pour la même raison ;
//   - /api/status contient en plus "device" et "features" ;
//   - routes en plus : /api/log, /api/settings, /api/discover, /api/ota.
// Aucun gestionnaire ne fait de requête vers l'enceinte (ils tournent dans async_tcp).
#include "web.h"

#include <ArduinoJson.h>
#include <AsyncJson.h>
#include <ESPAsyncWebServer.h>
#include <Update.h>
#include <WiFi.h>

#include "common.h"
#include "events.h"
#include "page_html.h"
#include "player.h"
#include "speaker.h"

volatile uint32_t gRestartAtMs = 0;

static AsyncWebServer server(80);
static String otaError;

// ---------------------------------------------------------------------------
// Réponses
// ---------------------------------------------------------------------------

static void sendJson(AsyncWebServerRequest *r, int code, JsonDocument &doc) {
  AsyncResponseStream *res = r->beginResponseStream("application/json; charset=utf-8");
  res->setCode(code);
  res->addHeader("Cache-Control", "no-store");
  serializeJson(doc, *res);
  r->send(res);
}

static void sendError(AsyncWebServerRequest *r, int code, const char *error, const String &message = "") {
  JsonDocument doc;
  doc["ok"] = false;
  doc["error"] = error;
  doc["message"] = message.length() ? message : String(error);
  sendJson(r, code, doc);
}

static bool validUrl(const String &u) {
  return (u.startsWith("http://") || u.startsWith("https://")) && u.length() > 8 && u.length() < MAX_URL &&
         u.indexOf(' ') < 0;
}

static String cleanName(String s) {
  s.trim();
  while (s.indexOf("  ") >= 0) s.replace("  ", " ");
  return s.substring(0, MAX_NAME);
}

// ---------------------------------------------------------------------------
// GET /api/status
// ---------------------------------------------------------------------------

static void handleStatus(AsyncWebServerRequest *r) {
  JsonDocument doc;
  {
    Lock l;
    gSt.lastStatusRequestMs = millis();     // demande à la tâche player de rafraîchir /now_playing
    doc["version"] = BRIDGE_VERSION;
    JsonObject sp = doc["speaker"].to<JsonObject>();
    sp["name"] = gSt.speakerName;
    sp["host"] = gSt.speakerHost;
    doc["ws_connected"] = gSt.wsConnected;
    if (gSt.np.valid && millis() - gSt.npAtMs < 30000) {
      JsonObject np = doc["now_playing"].to<JsonObject>();
      np["source"] = gSt.np.source;
      np["play_status"] = gSt.np.playStatus;
      np["item_name"] = gSt.np.itemName;
      np["location"] = gSt.np.location;
    } else {
      doc["now_playing"] = nullptr;
    }
    if (gSt.lastPresetId) {
      JsonObject lp = doc["last_preset"].to<JsonObject>();
      lp["id"] = gSt.lastPresetId;
      lp["name"] = gSt.lastPresetName;
      lp["at"] = gSt.lastPresetAt;
      lp["inferred"] = gSt.lastPresetInferred;
    } else {
      doc["last_preset"] = nullptr;
    }
    if (gSt.playingId >= 0) {
      JsonObject pl = doc["playing"].to<JsonObject>();
      pl["id"] = gSt.playingId;
      pl["name"] = gSt.playingName;
    } else {
      doc["playing"] = nullptr;
    }
    JsonObject pl = doc["player"].to<JsonObject>();
    pl["state"] = gSt.playerState;
    pl["message"] = gSt.playerMessage;
    if (gSt.lastAutoReboot.length()) doc["last_auto_reboot"] = gSt.lastAutoReboot;
    else doc["last_auto_reboot"] = nullptr;
    doc["rebooting"] = gSt.rebooting;
    doc["preset_write_method"] = methodValid(gCfg.method) ? gCfg.method : String("none");
    JsonArray cloud = doc["speaker_presets_cloud"].to<JsonArray>();
    for (int n = 1; n <= 6; n++)
      if (gSt.cloudPresetsMask & (1 << n)) cloud.add(n);
    if (gSt.lastRewriteJson.length()) doc["last_rewrite"] = serialized(gSt.lastRewriteJson);
    else doc["last_rewrite"] = nullptr;
    doc["catalog_url"] = gCfg.catalogUrl;
    doc["page_url"] = "http://" + WiFi.localIP().toString() + "/";

    JsonObject dev = doc["device"].to<JsonObject>();
    dev["uptime_s"] = millis() / 1000;
    dev["rssi"] = WiFi.RSSI();
    dev["ssid"] = WiFi.SSID();
    dev["ip"] = WiFi.localIP().toString();
    dev["hostname"] = "bose-bridge.local";
    dev["free_heap"] = ESP.getFreeHeap();
    dev["min_free_heap"] = ESP.getMinFreeHeap();
    dev["reset_reason"] = gSt.resetReason;
    dev["boot_count"] = gSt.bootCount;
    dev["time_synced"] = timeValid();
    dev["speaker_discovered"] = gSt.speakerDiscovered;
  }
  JsonArray f = doc["features"].to<JsonArray>();
  f.add("device");
  f.add("log");
  f.add("settings");
  f.add("ota");
  sendJson(r, 200, doc);
}

// ---------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------

static void handlePresets(AsyncWebServerRequest *r) {
  JsonDocument doc;
  JsonArray arr = doc["presets"].to<JsonArray>();
  {
    Lock l;
    for (int n = 1; n <= 6; n++) {
      JsonObject p = arr.add<JsonObject>();
      p["id"] = n;
      p["name"] = gCfg.presets[n].name;
      p["stream_url"] = gCfg.presets[n].url;
      p["logo_url"] = gCfg.presets[n].logo;
    }
  }
  sendJson(r, 200, doc);
}

static void handlePutPreset(AsyncWebServerRequest *r, JsonVariant &json, int n) {
  if (!json.is<JsonObject>()) return sendError(r, 400, "bad_json", "JSON object expected");
  String name = cleanName(json["name"] | "");
  String url = String(json["stream_url"] | "");
  String logo = String(json["logo_url"] | "");
  url.trim();
  logo.trim();
  if (url.length() && !validUrl(url))
    return sendError(r, 400, "bad_stream_url", "stream_url must start with http:// or https:// (511 characters max)");
  if (logo.length() && !validUrl(logo))
    return sendError(r, 400, "bad_logo_url", "logo_url must start with http:// or https:// (511 characters max)");
  if (url.length() && !name.length()) return sendError(r, 400, "missing_name", "name is required");
  String method;
  {
    Lock l;
    gCfg.presets[n].name = name;
    gCfg.presets[n].url = url;
    gCfg.presets[n].logo = logo;
    method = gCfg.method;
  }
  settingsSavePreset(n);
  logf("Preset %d enregistré : %s - %s", n, name.c_str(), url.c_str());
  JsonDocument doc;
  doc["ok"] = true;
  doc["preset"] = n;
  doc["config"] = "saved";
  if (!url.length()) doc["speaker"] = "skipped (empty preset)";
  else if (!methodValid(method)) doc["speaker"] = "skipped (preset_write_method = none)";
  else {
    playerRewrite(1 << n);
    doc["speaker"] = "pending";
  }
  sendJson(r, 200, doc);
}

// ---------------------------------------------------------------------------
// Réglages (propres à l'ESP32 : pas de fichier de configuration à éditer)
// ---------------------------------------------------------------------------

static void handleGetSettings(AsyncWebServerRequest *r) {
  JsonDocument doc;
  {
    Lock l;
    doc["speaker_host"] = gSt.speakerHost;
    doc["speaker_discovered"] = gSt.speakerDiscovered;
    doc["speaker_host_fallback"] = gCfg.hostFallback;
    doc["speaker_mac"] = gCfg.speakerMac;
    doc["catalog_url"] = gCfg.catalogUrl;
    doc["preset_write_method"] = gCfg.method;
    doc["debounce_seconds"] = gCfg.debounceS;
    doc["event_play_delay_seconds"] = gCfg.eventDelayS;
    doc["watchdog"] = gCfg.watchdog;
    doc["watchdog_timeout_seconds"] = gCfg.watchdogTimeoutS;
    doc["auto_reboot"] = gCfg.autoReboot;
    doc["auto_reboot_min_interval_seconds"] = gCfg.rebootMinIntervalS;
    doc["rewrite_presets_on_startup"] = gCfg.rewriteOnStartup;
    doc["resume_on_power_on"] = gCfg.resumeOnPowerOn;
    doc["ota_password_set"] = gCfg.otaPassword.length() > 0;
  }
  sendJson(r, 200, doc);
}

static void handlePutSettings(AsyncWebServerRequest *r, JsonVariant &json) {
  if (!json.is<JsonObject>()) return sendError(r, 400, "bad_json", "JSON object expected");
  JsonObject o = json.as<JsonObject>();
  String newFallback;
  {
    Lock l;
    if (o["speaker_host_fallback"].is<const char *>()) {
      String h = o["speaker_host_fallback"].as<String>();
      h.trim();
      IPAddress ip;
      if (!ip.fromString(h)) return sendError(r, 400, "bad_host", "speaker_host_fallback must be an IPv4 address");
      if (h != gCfg.hostFallback) newFallback = h;
      gCfg.hostFallback = h;
    }
    if (o["speaker_mac"].is<const char *>()) {
      String m = o["speaker_mac"].as<String>();
      m.replace(":", "");
      m.toUpperCase();
      if (m.length() && m.length() != 12) return sendError(r, 400, "bad_mac", "speaker_mac: 12 hex digits");
      gCfg.speakerMac = m;
    }
    if (o["catalog_url"].is<const char *>()) {
      String u = o["catalog_url"].as<String>();
      if (!validUrl(u)) return sendError(r, 400, "bad_catalog_url");
      gCfg.catalogUrl = u;
    }
    if (o["preset_write_method"].is<const char *>()) {
      String m = o["preset_write_method"].as<String>();
      if (!methodValid(m) && m != "none") return sendError(r, 400, "bad_method", "upnp, lir_direct or none");
      gCfg.method = m;
    }
    if (o["debounce_seconds"].is<float>()) gCfg.debounceS = constrain(o["debounce_seconds"].as<float>(), 0.0f, 30.0f);
    if (o["event_play_delay_seconds"].is<float>())
      gCfg.eventDelayS = constrain(o["event_play_delay_seconds"].as<float>(), 0.0f, 5.0f);
    if (o["watchdog"].is<bool>()) gCfg.watchdog = o["watchdog"];
    if (o["watchdog_timeout_seconds"].is<int>())
      gCfg.watchdogTimeoutS = constrain(o["watchdog_timeout_seconds"].as<int>(), 4, 60);
    if (o["auto_reboot"].is<bool>()) gCfg.autoReboot = o["auto_reboot"];
    if (o["auto_reboot_min_interval_seconds"].is<int>())
      gCfg.rebootMinIntervalS = max(300, o["auto_reboot_min_interval_seconds"].as<int>());
    if (o["rewrite_presets_on_startup"].is<bool>()) gCfg.rewriteOnStartup = o["rewrite_presets_on_startup"];
    if (o["resume_on_power_on"].is<bool>()) gCfg.resumeOnPowerOn = o["resume_on_power_on"];
    if (o["ota_password"].is<const char *>()) {
      // Défini au portail Wi-Fi ; modifiable ici seulement en donnant l'ancien.
      String cur = o["ota_password_current"] | "";
      if (gCfg.otaPassword.length() && cur != gCfg.otaPassword)
        return sendError(r, 403, "bad_password", "ota_password_current is wrong");
      gCfg.otaPassword = o["ota_password"].as<String>();
    }
  }
  settingsSaveAll();
  logf("Réglages enregistrés depuis la page web");
  bool discovered;
  { Lock l; discovered = gSt.speakerDiscovered; }
  if (newFallback.length() && !discovered) {   // pas trouvée par SSDP : on prend la nouvelle IP tout de suite
    speaker::setHost(newFallback);
    eventsChangeHost(newFallback);
  }
  handleGetSettings(r);
}

// ---------------------------------------------------------------------------
// Mise à jour OTA : POST /api/ota, fichier firmware.bin en multipart, en-tête X-OTA-Password
// ---------------------------------------------------------------------------

static void otaUpload(AsyncWebServerRequest *r, const String &filename, size_t index, uint8_t *data, size_t len,
                      bool final) {
  if (index == 0) {
    otaError = "";
    String pw;
    { Lock l; pw = gCfg.otaPassword; }
    if (!pw.length()) otaError = "ota_disabled";
    else if (!r->hasHeader("X-OTA-Password") || r->header("X-OTA-Password") != pw) otaError = "bad_password";
    else if (!Update.begin(UPDATE_SIZE_UNKNOWN, U_FLASH)) otaError = Update.errorString();
    else logf("OTA : réception de %s", filename.c_str());
    if (otaError.length()) logf("OTA refusée : %s", otaError.c_str());
  }
  if (otaError.length()) return;
  if (len && Update.write(data, len) != len) {
    otaError = Update.errorString();
    Update.abort();
    logf("OTA : erreur d'écriture %s", otaError.c_str());
    return;
  }
  if (final) {
    if (!Update.end(true)) otaError = Update.errorString();
    else logf("OTA : %u octets écrits, redémarrage", (unsigned)(index + len));
  }
}

static void otaDone(AsyncWebServerRequest *r) {
  if (otaError == "ota_disabled")
    return sendError(r, 403, "ota_disabled", "no OTA password: set one in the Wi-Fi portal or the settings");
  if (otaError == "bad_password") return sendError(r, 403, "bad_password", "wrong OTA password");
  if (otaError.length() || !Update.isFinished()) return sendError(r, 500, "ota_failed", otaError);
  JsonDocument doc;
  doc["ok"] = true;
  doc["message"] = "update installed, restarting";
  sendJson(r, 200, doc);
  gRestartAtMs = millis() + 1500;
}

// ---------------------------------------------------------------------------

void webBegin() {
  server.on("/", HTTP_GET, [](AsyncWebServerRequest *r) {
    AsyncWebServerResponse *res = r->beginResponse(200, "text/html; charset=utf-8", PAGE_HTML_GZ, PAGE_HTML_GZ_LEN);
    res->addHeader("Content-Encoding", "gzip");
    res->addHeader("Cache-Control", "no-store");
    r->send(res);
  });
  server.on("/api/status", HTTP_GET, handleStatus);
  server.on("/api/presets", HTTP_GET, handlePresets);
  server.on("/api/log", HTTP_GET, [](AsyncWebServerRequest *r) {
    AsyncResponseStream *res = r->beginResponseStream("application/json; charset=utf-8");
    res->print("{\"lines\":");
    logWriteJsonArray(*res);
    res->print("}");
    r->send(res);
  });
  server.on("/api/settings", HTTP_GET, handleGetSettings);

  for (int n = 1; n <= 6; n++) {
    auto *h = new AsyncCallbackJsonWebHandler("/api/presets/" + String(n),
                                              [n](AsyncWebServerRequest *r, JsonVariant &j) { handlePutPreset(r, j, n); });
    h->setMethod(HTTP_PUT);
    server.addHandler(h);
    server.on(("/api/play/" + String(n)).c_str(), HTTP_POST, [n](AsyncWebServerRequest *r) {
      if (!playerPlayPreset(n, 0, false, "page web"))
        return sendError(r, 409, "preset_empty", "preset " + String(n) + " is not configured");
      JsonDocument doc;
      doc["ok"] = true;
      doc["preset"] = n;
      sendJson(r, 202, doc);
    });
  }

  auto *test = new AsyncCallbackJsonWebHandler("/api/test", [](AsyncWebServerRequest *r, JsonVariant &j) {
    String url = String(j["stream_url"] | "");
    url.trim();
    if (!validUrl(url)) return sendError(r, 400, "bad_stream_url", "stream_url must start with http:// or https://");
    playerTest(url, cleanName(j["name"] | ""), String(j["logo_url"] | ""));
    JsonDocument doc;
    doc["ok"] = true;
    sendJson(r, 202, doc);
  });
  test->setMethod(HTTP_POST);
  server.addHandler(test);

  auto *settings = new AsyncCallbackJsonWebHandler("/api/settings", handlePutSettings);
  settings->setMethod(HTTP_PUT);
  server.addHandler(settings);

  server.on("/api/reboot-speaker", HTTP_POST, [](AsyncWebServerRequest *r) {
    if (!playerReboot()) return sendError(r, 409, "busy", "a reboot is already running");
    JsonDocument doc;
    doc["ok"] = true;
    doc["message"] = "reboot sent, about 2 minutes";
    sendJson(r, 202, doc);
  });
  server.on("/api/rewrite-speaker-presets", HTTP_POST, [](AsyncWebServerRequest *r) {
    String m;
    { Lock l; m = gCfg.method; }
    if (!methodValid(m)) return sendError(r, 409, "method_not_validated", "preset_write_method = none");
    playerRewrite(0);
    JsonDocument doc;
    doc["ok"] = true;
    doc["pending"] = true;
    doc["method"] = m;
    sendJson(r, 202, doc);
  });
  server.on("/api/discover", HTTP_POST, [](AsyncWebServerRequest *r) {
    playerDiscover();
    JsonDocument doc;
    doc["ok"] = true;
    doc["pending"] = true;
    sendJson(r, 202, doc);
  });
  server.on("/api/ota", HTTP_POST, otaDone, otaUpload);

  server.onNotFound([](AsyncWebServerRequest *r) { sendError(r, 404, "not_found"); });
  server.begin();
  logf("Page web : http://bose-bridge.local/ (http://%s/)", WiFi.localIP().toString().c_str());
}
