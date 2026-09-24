// Client WebSocket vers l'enceinte (ws://IP:8080, sous-protocole "gabbo") : détecte les
// appuis sur les presets. Tourne dans la boucle Arduino (eventsLoop), ne bloque jamais.
#pragma once
#include <Arduino.h>

void eventsBegin();
void eventsLoop();
void eventsChangeHost(const String &host);   // appelable depuis une autre tâche
