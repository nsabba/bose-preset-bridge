// Requêtes vers l'enceinte : API 8090, UPnP 8091, console TAP 17000, découverte SSDP.
#include "speaker.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_task_wdt.h>

#include "xmlmini.h"

namespace speaker {

static String gHost;

void setHost(const String &h) {
  Lock l;
  gHost = h;
  gSt.speakerHost = h;
}
String host() { Lock l; return gHost; }

static String base() { return "http://" + host() + ":8090"; }

// GET simple ; renvoie le code HTTP (<= 0 en cas d'erreur réseau) et le corps.
static int httpGet(const String &url, String &body, uint32_t timeoutMs) {
  esp_task_wdt_reset();                 // une requête peut durer jusqu'à timeoutMs
  HTTPClient http;
  http.setConnectTimeout(timeoutMs);
  http.setTimeout(timeoutMs);
  if (!http.begin(url)) return -1;
  int code = http.GET();
  if (code > 0) body = http.getString();
  http.end();
  return code;
}

static int httpPost(const String &url, const String &payload, const char *contentType,
                    const char *soapAction, String &body, uint32_t timeoutMs) {
  esp_task_wdt_reset();
  HTTPClient http;
  http.setConnectTimeout(timeoutMs);
  http.setTimeout(timeoutMs);
  if (!http.begin(url)) return -1;
  http.addHeader("Content-Type", contentType);
  if (soapAction) http.addHeader("SOAPAction", soapAction);
  int code = http.POST(payload);
  if (code > 0) body = http.getString();
  http.end();
  return code;
}

// ---------------------------------------------------------------------------
// API 8090
// ---------------------------------------------------------------------------

bool info(String &name, String &firmware, uint32_t timeoutMs) {
  String body;
  if (httpGet(base() + "/info", body, timeoutMs) != 200) return false;
  name = xml::text(body, "name");
  firmware = xml::text(body, "softwareVersion");
  int sp = firmware.indexOf(' ');
  if (sp > 0) firmware = firmware.substring(0, sp);
  return true;
}

bool nowPlaying(NowPlaying &np, uint32_t timeoutMs) {
  String body;
  np = NowPlaying();
  if (httpGet(base() + "/now_playing", body, timeoutMs) != 200) return false;
  return parse::nowPlaying(body, np);
}

bool presets(std::vector<SpeakerPreset> &out) {
  String body;
  out.clear();
  if (httpGet(base() + "/presets", body, 5000) != 200) return false;
  if (!parse::presets(body, out)) {
    logf("GET /presets : réponse incomplète (%u octets), ignorée", body.length());
    return false;
  }
  return true;
}

bool sendKey(const char *key) {
  // Appui court : ne jamais tenir un PRESET_n (un appui long l'enregistrerait).
  for (const char *state : {"press", "release"}) {
    String body;
    String payload = String("<key state=\"") + state + "\" sender=\"Gabbo\">" + key + "</key>";
    if (httpPost(base() + "/key", payload, "application/xml", nullptr, body, 5000) != 200) return false;
  }
  return true;
}

int storePreset(int id, const String &source, const String &type, const String &location,
                const String &account, const String &name) {
  String payload = "<preset id=\"" + String(id) + "\"><ContentItem source=\"" + xml::escape(source) + "\"";
  if (type.length()) payload += " type=\"" + xml::escape(type) + "\"";
  payload += " location=\"" + xml::escape(location) + "\"";
  if (account.length()) payload += " sourceAccount=\"" + xml::escape(account) + "\"";
  payload += " isPresetable=\"true\"><itemName>" + xml::escape(name) + "</itemName></ContentItem></preset>";
  String body;
  return httpPost(base() + "/storePreset", payload, "application/xml", nullptr, body, 10000);
}

// ---------------------------------------------------------------------------
// UPnP AVTransport (8091)
// ---------------------------------------------------------------------------

static const char *SOAP_HEAD =
    "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
    "<s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" "
    "s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body>";
static const char *SOAP_TAIL = "</s:Body></s:Envelope>";

static bool soap(const char *action, const String &inner) {
  String url = "http://" + host() + ":8091/AVTransport/Control";
  String soapAction = String("\"urn:schemas-upnp-org:service:AVTransport:1#") + action + "\"";
  String body;
  int code = httpPost(url, String(SOAP_HEAD) + inner + SOAP_TAIL, "text/xml; charset=\"utf-8\"",
                      soapAction.c_str(), body, 10000);
  if (code != 200) logf("SOAP %s : HTTP %d", action, code);
  return code == 200;
}

bool playUrl(const String &url, const String &title, const String &logo) {
  // Métadonnées DIDL-Lite (le titre devient le nom affiché par l'enceinte),
  // échappées une première fois pour le XML DIDL puis une seconde pour le SOAP.
  String didl =
      "<DIDL-Lite xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\" "
      "xmlns:dc=\"http://purl.org/dc/elements/1.1/\" "
      "xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\">"
      "<item id=\"1\" parentID=\"0\" restricted=\"1\">"
      "<dc:title>" + xml::escape(title) + "</dc:title>"
      "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
      "<upnp:albumArtURI>" + xml::escape(logo) + "</upnp:albumArtURI>"
      "<res protocolInfo=\"http-get:*:audio/mpeg:*\">" + xml::escape(url) + "</res>"
      "</item></DIDL-Lite>";
  String setUri =
      "<u:SetAVTransportURI xmlns:u=\"urn:schemas-upnp-org:service:AVTransport:1\">"
      "<InstanceID>0</InstanceID><CurrentURI>" + xml::escape(url) + "</CurrentURI>"
      "<CurrentURIMetaData>" + xml::escape(didl) + "</CurrentURIMetaData></u:SetAVTransportURI>";
  if (!soap("SetAVTransportURI", setUri)) return false;
  if (!soap("Play", "<u:Play xmlns:u=\"urn:schemas-upnp-org:service:AVTransport:1\">"
                    "<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>")) return false;
  logf("UPnP SetAVTransportURI + Play envoyés (%s)", title.c_str());
  return true;
}

// ---------------------------------------------------------------------------
// Console TAP (17000) : validé sur place le 23/09/2026
// ---------------------------------------------------------------------------

bool reboot() {
  WiFiClient c;
  if (!c.connect(host().c_str(), 17000, 5000)) {
    logf("Reboot : connexion au port 17000 impossible");
    return false;
  }
  delay(1000);                          // la console envoie une invite : attendre, la lire, l'ignorer
  while (c.available()) c.read();
  c.print("sys reboot\n");               // LF seul
  delay(500);
  c.stop();
  return true;
}

bool streamReachable(const String &url) {
  HTTPClient http;
  http.setConnectTimeout(6000);
  http.setTimeout(6000);
  http.setFollowRedirects(HTTPC_FORCE_FOLLOW_REDIRECTS);
  if (!http.begin(url)) return false;
  int code = http.GET();
  bool ok = false;
  if (code >= 200 && code < 300) {
    WiFiClient *s = http.getStreamPtr();
    uint32_t t0 = millis();
    while (s && s->connected() && millis() - t0 < 4000) {
      if (s->available()) { ok = true; break; }
      delay(50);
    }
  }
  http.end();
  return ok;
}

bool isCloud(const SpeakerPreset &p) { return parse::isCloud(p); }

bool isPlayingUpnp(const NowPlaying &np) {
  return np.valid && np.source == "UPNP" && np.playStatus == "PLAY_STATE";
}

// ---------------------------------------------------------------------------
// Découverte SSDP : l'enceinte répond avec
//   USN: uuid:BO5EBO5E-F00D-F00D-FEED-<MAC>::urn:schemas-upnp-org:device:MediaRenderer:1
// ---------------------------------------------------------------------------

bool discover(const String &preferredMac, String &ipOut, String &macOut, uint32_t waitMs) {
  WiFiUDP udp;
  if (!udp.begin(0)) return false;
  const char *msg =
      "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
      "MX: 2\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n";
  for (int i = 0; i < 2; i++) {         // deux envois : l'UDP multicast peut se perdre
    udp.beginPacket(IPAddress(239, 255, 255, 250), 1900);
    udp.write((const uint8_t *)msg, strlen(msg));
    udp.endPacket();
    delay(100);
  }
  String firstIp, firstMac;
  bool found = false;
  uint32_t t0 = millis();
  char buf[768];
  while (millis() - t0 < waitMs) {
    int len = udp.parsePacket();
    if (len <= 0) { delay(20); continue; }
    int n = udp.read(buf, sizeof buf - 1);
    buf[n > 0 ? n : 0] = 0;
    String mac = parse::ssdpBoseMac(String(buf));
    if (!mac.length()) continue;
    String ip = udp.remoteIP().toString();
    if (preferredMac.length() && mac.equalsIgnoreCase(preferredMac)) {
      ipOut = ip; macOut = mac;
      udp.stop();
      return true;
    }
    if (!found) { firstIp = ip; firstMac = mac; found = true; }
  }
  udp.stop();
  if (found) { ipOut = firstIp; macOut = firstMac; }
  return found;
}

}  // namespace speaker
