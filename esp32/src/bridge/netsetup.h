// Wi-Fi (portail WiFiManager "Bose-Bridge-Setup"), mDNS bose-bridge.local, heure NTP.
// Isolé dans son propre fichier : les en-têtes de WiFiManager (WebServer synchrone) et
// d'ESPAsyncWebServer définissent tous deux HTTP_GET & co. et ne doivent pas se croiser.
#pragma once
#include <Arduino.h>

void netBegin();          // bloquant : portail au premier démarrage, sinon connexion au Wi-Fi enregistré
void netLoop();           // surveille la connexion et le bouton BOOT
