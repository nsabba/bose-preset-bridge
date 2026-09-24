// Serveur web asynchrone (port 80) : la page de la V2 et la même API HTTP (cf. PROTOCOL.md § 6).
#pragma once
#include <Arduino.h>

void webBegin();
extern volatile uint32_t gRestartAtMs;   // redémarrage différé (après une mise à jour OTA)
