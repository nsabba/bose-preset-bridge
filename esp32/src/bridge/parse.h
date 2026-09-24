// Lecture des réponses de l'enceinte : fonctions pures (aucun accès réseau ni FreeRTOS),
// utilisées par le firmware et testées sur ordinateur (test/host) avec de vraies réponses.
#pragma once
#include <Arduino.h>
#include <vector>

#include "xmlmini.h"

struct NowPlaying {
  bool valid = false;                   // false = l'enceinte n'a pas répondu
  String source, playStatus, itemName, location;
};

struct SpeakerPreset {
  int id = 0;
  String source, type, location, name;
};

namespace parse {

// GET /now_playing
inline bool nowPlaying(const String &body, NowPlaying &np) {
  np = NowPlaying();
  int root = xml::findTag(body, "nowPlaying");
  if (root < 0) return false;
  np.valid = true;
  np.source = xml::attr(body, root, "source");
  np.playStatus = xml::text(body, "playStatus");
  np.location = xml::attr(body, xml::findTag(body, "ContentItem"), "location");
  np.itemName = xml::text(body, "itemName");
  if (!np.itemName.length()) np.itemName = xml::text(body, "stationName");
  return true;
}

// GET /presets (et réponse de POST /storePreset)
inline void presets(const String &body, std::vector<SpeakerPreset> &out) {
  out.clear();
  int pos = 0;
  while ((pos = xml::findTag(body, "preset", pos)) >= 0) {   // "<presets>" n'est pas pris
    int next = xml::findTag(body, "preset", pos + 7);
    int stop = next >= 0 ? next : body.length();
    String block = body.substring(pos, stop);
    SpeakerPreset p;
    p.id = xml::attr(block, 0, "id").toInt();
    int ci = xml::findTag(block, "ContentItem");
    p.source = xml::attr(block, ci, "source");
    p.type = xml::attr(block, ci, "type");
    p.location = xml::attr(block, ci, "location");
    p.name = xml::text(block, "itemName");
    if (p.id >= 1 && p.id <= 6) out.push_back(p);
    pos = stop;
  }
}

inline bool isCloud(const SpeakerPreset &p) {
  return p.source == "TUNEIN" || p.location.indexOf("bose.io") >= 0;
}

// Message WebSocket "gabbo" : nom de l'événement (premier élément dans <updates>, sinon la
// racine), id du preset, location du ContentItem, texte d'erreur.
struct Event {
  String tag, location, error;
  int presetId = -1;
};

inline Event event(const String &msg) {
  Event e;
  int start = msg.indexOf('<');
  if (msg.startsWith("<?")) start = msg.indexOf('<', 2);
  if (start >= 0 && msg.indexOf("<updates", start) == start) start = msg.indexOf('<', start + 1);
  if (start < 0) return e;
  int end = start + 1;
  while (end < (int)msg.length() && msg[end] != ' ' && msg[end] != '>' && msg[end] != '/') end++;
  e.tag = msg.substring(start + 1, end);
  int p = xml::findTag(msg, "preset");
  if (p >= 0) e.presetId = xml::attr(msg, p, "id").toInt();
  e.location = xml::attr(msg, xml::findTag(msg, "ContentItem"), "location");
  if (e.tag == "errorUpdate") e.error = xml::text(msg, "error");
  return e;
}

// Réponse SSDP : MAC contenue dans l'UDN "uuid:BO5EBO5E-F00D-F00D-FEED-<MAC>" ; "" si ce n'est
// pas une enceinte Bose.
inline String ssdpBoseMac(const String &response) {
  static const char *PREFIX = "BO5EBO5E-F00D-F00D-FEED-";
  String up = response;
  up.toUpperCase();
  int i = up.indexOf(PREFIX);
  if (i < 0) return "";
  String mac = up.substring(i + strlen(PREFIX), i + strlen(PREFIX) + 12);
  return mac.length() == 12 ? mac : "";
}

}  // namespace parse
