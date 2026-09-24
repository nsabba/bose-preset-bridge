// Client des interfaces locales de l'enceinte (cf. PROTOCOL.md, mêmes requêtes que speaker.py).
// Toutes ces fonctions sont BLOQUANTES : à n'appeler que depuis la tâche player.
#pragma once
#include <Arduino.h>
#include <vector>

#include "common.h"

namespace speaker {

void setHost(const String &host);       // IP utilisée par toutes les requêtes
String host();

bool info(String &name, String &firmware, uint32_t timeoutMs = 3000);
bool nowPlaying(NowPlaying &np, uint32_t timeoutMs = 3000);
bool presets(std::vector<SpeakerPreset> &out);
bool sendKey(const char *key);          // appui court (press + release)
int storePreset(int id, const String &source, const String &type, const String &location,
                const String &account, const String &name);   // code HTTP
bool playUrl(const String &url, const String &title, const String &logo);
bool reboot();                          // console TAP, port 17000
bool streamReachable(const String &url);

bool isCloud(const SpeakerPreset &p);
bool isPlayingUpnp(const NowPlaying &np);

// Découverte SSDP (M-SEARCH MediaRenderer). Renvoie true si une enceinte Bose répond ;
// préfère celle dont la MAC vaut preferredMac, sinon la première trouvée.
bool discover(const String &preferredMac, String &ipOut, String &macOut, uint32_t waitMs = 3000);

}  // namespace speaker
