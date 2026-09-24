// Réglages en NVS, journal circulaire, heure NTP.
#include "common.h"

#include <Preferences.h>
#include <esp_system.h>
#include <stdarg.h>
#include <time.h>

#include "defaults_gen.h"

SemaphoreHandle_t gLock = nullptr;   // créé par logBegin(), au tout début de setup()
Settings gCfg;
Status gSt;

static const char *DEFAULT_CATALOG =
    "https://raw.githubusercontent.com/nsabba/bose-preset-bridge/main/catalog/radios.json";

// ---------------------------------------------------------------------------
// Réglages
// ---------------------------------------------------------------------------

static String key(const char *kind, int n) { return String("p") + n + kind; }  // "p1u", "p1n", "p1l"

bool methodValid(const String &m) { return m == "upnp" || m == "lir_direct"; }

void settingsLoad() {
  Lock l;
  Preferences p;
  p.begin("cfg", true);
  for (int n = 1; n <= 6; n++) {
    const DefaultPreset &d = DEFAULT_PRESETS[n - 1];
    gCfg.presets[n].url = p.getString(key("u", n).c_str(), d.url);
    gCfg.presets[n].name = p.getString(key("n", n).c_str(), d.name);
    gCfg.presets[n].logo = p.getString(key("l", n).c_str(), d.logo);
  }
  gCfg.hostFallback = p.getString("host_fb", "192.168.1.17");
  gCfg.speakerMac = p.getString("mac", "A81B6A51E435");
  gCfg.catalogUrl = p.getString("catalog", DEFAULT_CATALOG);
  gCfg.method = p.getString("method", "upnp");
  gCfg.debounceS = p.getFloat("debounce", 3.0f);
  gCfg.eventDelayS = p.getFloat("evdelay", 0.7f);
  gCfg.watchdogTimeoutS = p.getUShort("wdtimeout", 10);
  gCfg.watchdog = p.getBool("watchdog", true);
  gCfg.autoReboot = p.getBool("autoreboot", true);
  gCfg.rebootMinIntervalS = p.getUInt("rbinterval", 900);
  gCfg.rewriteOnStartup = p.getBool("rewrite", true);
  gCfg.resumeOnPowerOn = p.getBool("resume", true);
  gCfg.otaPassword = p.getString("ota_pw", "");
  p.end();
}

void settingsSavePreset(int n) {
  Lock l;
  Preferences p;
  p.begin("cfg", false);
  p.putString(key("u", n).c_str(), gCfg.presets[n].url);
  p.putString(key("n", n).c_str(), gCfg.presets[n].name);
  p.putString(key("l", n).c_str(), gCfg.presets[n].logo);
  p.end();
}

void settingsSaveAll() {
  Lock l;
  for (int n = 1; n <= 6; n++) settingsSavePreset(n);
  Preferences p;
  p.begin("cfg", false);
  p.putString("host_fb", gCfg.hostFallback);
  p.putString("mac", gCfg.speakerMac);
  p.putString("catalog", gCfg.catalogUrl);
  p.putString("method", gCfg.method);
  p.putFloat("debounce", gCfg.debounceS);
  p.putFloat("evdelay", gCfg.eventDelayS);
  p.putUShort("wdtimeout", gCfg.watchdogTimeoutS);
  p.putBool("watchdog", gCfg.watchdog);
  p.putBool("autoreboot", gCfg.autoReboot);
  p.putUInt("rbinterval", gCfg.rebootMinIntervalS);
  p.putBool("rewrite", gCfg.rewriteOnStartup);
  p.putBool("resume", gCfg.resumeOnPowerOn);
  p.putString("ota_pw", gCfg.otaPassword);
  p.end();
}

namespace persist {
static Preferences &st() {
  static Preferences p;
  static bool open = false;
  if (!open) open = p.begin("state", false);
  return p;
}
uint32_t getU(const char *k, uint32_t def) { Lock l; return st().getUInt(k, def); }
void putU(const char *k, uint32_t v) { Lock l; st().putUInt(k, v); }
String getS(const char *k, const String &def) { Lock l; return st().getString(k, def); }
void putS(const char *k, const String &v) { Lock l; st().putString(k, v); }
void remove(const char *k) { Lock l; st().remove(k); }
}  // namespace persist

// ---------------------------------------------------------------------------
// Journal circulaire
// ---------------------------------------------------------------------------

static const int LOG_LINES = 200;
static const int LOG_WIDTH = 160;
static char logBuf[LOG_LINES][LOG_WIDTH];   // 32 Ko en RAM statique
static int logHead = 0, logCount = 0;
static SemaphoreHandle_t logLock;

void logBegin() {
  gLock = xSemaphoreCreateRecursiveMutex();
  logLock = xSemaphoreCreateMutex();
}

void logf(const char *fmt, ...) {
  char msg[LOG_WIDTH];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(msg, sizeof msg, fmt, ap);
  va_end(ap);
  String stamp = isoNow();
  int t = stamp.indexOf('T');
  if (t >= 0) stamp = stamp.substring(t + 1);   // l'heure suffit dans le journal
  Serial.printf("[%s] %s\n", stamp.c_str(), msg);
  if (!logLock) return;
  xSemaphoreTake(logLock, portMAX_DELAY);
  snprintf(logBuf[logHead], LOG_WIDTH, "[%s] %s", stamp.c_str(), msg);
  logHead = (logHead + 1) % LOG_LINES;
  if (logCount < LOG_LINES) logCount++;
  xSemaphoreGive(logLock);
}

static void jsonString(Print &out, const char *s) {
  out.print('"');
  for (; *s; s++) {
    unsigned char c = *s;
    if (c == '"' || c == '\\') { out.print('\\'); out.print((char)c); }
    else if (c < 0x20) out.printf("\\u%04x", c);
    else out.print((char)c);
  }
  out.print('"');
}

void logWriteJsonArray(Print &out) {
  xSemaphoreTake(logLock, portMAX_DELAY);
  out.print('[');
  int start = (logHead - logCount + LOG_LINES) % LOG_LINES;
  for (int i = 0; i < logCount; i++) {
    if (i) out.print(',');
    jsonString(out, logBuf[(start + i) % LOG_LINES]);
  }
  out.print(']');
  xSemaphoreGive(logLock);
}

// ---------------------------------------------------------------------------
// Heure
// ---------------------------------------------------------------------------

bool timeValid() { return time(nullptr) > 1700000000; }

uint32_t epochNow() { return timeValid() ? (uint32_t)time(nullptr) : 0; }

String isoNow() {
  if (!timeValid()) return "+" + String(millis() / 1000) + "s";
  time_t t = time(nullptr);
  char buf[24];
  strftime(buf, sizeof buf, "%Y-%m-%dT%H:%M:%S", localtime(&t));
  return buf;
}

String resetReasonText() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_SW: return "SW";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INT_WDT";
    case ESP_RST_TASK_WDT: return "TASK_WDT";
    case ESP_RST_WDT: return "WDT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    case ESP_RST_EXT: return "EXT";
    default: return "UNKNOWN";
  }
}
