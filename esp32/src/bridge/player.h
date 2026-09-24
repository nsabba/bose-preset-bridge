// Tâche « player » : lecture UPnP + watchdog /now_playing + reboot automatique,
// réécriture des presets de l'enceinte, découverte. Reprend la classe Player de bridge.py.
//
// Les autres contextes (WebSocket, API web) ne font que déposer des commandes dans une file :
// seule cette tâche fait des requêtes HTTP bloquantes vers l'enceinte.
#pragma once
#include <Arduino.h>

void playerBegin();                                   // crée la tâche (après la connexion Wi-Fi)

// Joue le preset n (1 à 6) tel que configuré. delayMs : attente avant le Play (appui télécommande).
// Renvoie false si le preset est vide.
bool playerPlayPreset(int n, uint16_t delayMs, bool inferred, const char *origin);
// Bouton « Tester » : joue une URL tout de suite, sans reboot automatique.
void playerTest(const String &url, const String &name, const String &logo);
bool playerReboot();                                  // reboot manuel ; false si déjà en cours
bool playerRewrite(uint8_t mask);                     // mask = 0 : tous les presets configurés
void playerDiscover();
void playerOnSpeakerConnected();                      // appelé par le client WebSocket
bool playerFastReconnect();                           // true pendant un reboot de l'enceinte
