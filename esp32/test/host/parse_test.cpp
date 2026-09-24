// Test sur ordinateur du parsing des réponses de l'enceinte (src/bridge/parse.h).
// Les fichiers fixtures/*.xml sont de vraies réponses de la SoundTouch 20 « Valentine »
// (firmware 27.0.6), relevées le 24/09/2026. Lancer : test/host/run.sh
#include <cstdio>
#include <fstream>
#include <sstream>

#include "../../src/bridge/parse.h"

static int failures = 0;
#define CHECK(cond)                                                          \
  do {                                                                       \
    if (!(cond)) { printf("ECHEC ligne %d : %s\n", __LINE__, #cond); failures++; } \
  } while (0)

static String load(const char *name) {
  std::ifstream f(std::string(FIXTURES) + "/" + name);
  std::stringstream ss;
  ss << f.rdbuf();
  return String(ss.str());
}

int main() {
  // --- /now_playing réel
  NowPlaying np;
  CHECK(parse::nowPlaying(load("now_playing.xml"), np));
  CHECK(np.valid);
  CHECK(np.source == "UPNP");
  CHECK(np.playStatus == "PLAY_STATE");
  CHECK(np.itemName == "France Inter");
  CHECK(np.location == "http://icecast.radiofrance.fr/franceinter-midfi.mp3");

  // --- veille et état « moteur bloqué »
  CHECK(parse::nowPlaying("<nowPlaying deviceID=\"X\" source=\"STANDBY\"><ContentItem source=\"STANDBY\" isPresetable=\"false\" /></nowPlaying>", np));
  CHECK(np.source == "STANDBY" && np.playStatus == "" && np.location == "");
  CHECK(parse::nowPlaying("<nowPlaying deviceID=\"X\" source=\"UPNP\" sourceAccount=\"UPnPUserName\"><ContentItem source=\"UPNP\" type=\"DO_NOT_RESUME\" location=\"unplayable location\" sourceAccount=\"UPnPUserName\" isPresetable=\"false\" /><track></track><stationName></stationName><playStatus>PLAY_STATE</playStatus></nowPlaying>", np));
  CHECK(np.location == "unplayable location" && np.itemName == "" && np.playStatus == "PLAY_STATE");
  CHECK(!parse::nowPlaying("<html>erreur</html>", np));

  // --- /presets réel (6 presets UPNP ; le 1 sans itemName, le 5 avec)
  std::vector<SpeakerPreset> ps;
  parse::presets(load("presets.xml"), ps);
  CHECK(ps.size() == 6);
  CHECK(ps[0].id == 1 && ps[0].source == "UPNP" && ps[0].location == "http://icecast.radiofrance.fr/franceinter-midfi.mp3");
  CHECK(ps[4].id == 5 && ps[4].source == "UPNP" && ps[4].name == "FIP Cultes");
  for (auto &p : ps) CHECK(!parse::isCloud(p));

  // --- preset LOCAL_INTERNET_RADIO (méthode lir_direct, format relevé le 24/09)
  parse::presets("<presets><preset id=\"5\" createdOn=\"1\" updatedOn=\"1\"><ContentItem source=\"LOCAL_INTERNET_RADIO\" "
                 "type=\"stationurl\" location=\"http://icecast.radiofrance.fr/fipcultes-midfi.mp3\" isPresetable=\"true\">"
                 "<itemName>FIP Cultes</itemName></ContentItem></preset></presets>", ps);
  CHECK(ps.size() == 1 && ps[0].source == "LOCAL_INTERNET_RADIO" && ps[0].type == "stationurl" && !parse::isCloud(ps[0]));

  // --- presets cloud, comme sur l'enceinte cible le 23/09
  parse::presets(
      "<presets><preset id=\"2\" createdOn=\"1\" updatedOn=\"2\"><ContentItem source=\"LOCAL_INTERNET_RADIO\" type=\"stationurl\" "
      "location=\"https://content.api.bose.io/core02/svc-bmx-adapter-orion/prod/orion/station?data=eyJhIjoxfQ%3D%3D&amp;x=1\" "
      "isPresetable=\"true\"><itemName>franceinfo</itemName></ContentItem></preset>"
      "<preset id=\"4\"><ContentItem source=\"TUNEIN\" type=\"stationurl\" location=\"/v1/playback/station/s15200\" "
      "sourceAccount=\"\" isPresetable=\"true\"><itemName>La Musique d&apos;Inter</itemName></ContentItem></preset></presets>",
      ps);
  CHECK(ps.size() == 2);
  CHECK(ps[0].id == 2 && parse::isCloud(ps[0]) && ps[0].location.indexOf("&x=1") > 0);   // &amp; décodé
  CHECK(ps[1].id == 4 && parse::isCloud(ps[1]) && ps[1].name == "La Musique d'Inter");

  // --- événements WebSocket réels
  parse::Event e = parse::event(load("ws_selection.xml"));
  CHECK(e.tag == "nowSelectionUpdated" && e.presetId == 5);
  e = parse::event(load("ws_error.xml"));
  CHECK(e.tag == "errorUpdate" && e.error == "UpnpRcvdContentItemInWrongState");
  e = parse::event("<updates deviceID=\"X\"><nowSelectionUpdated><preset id=\"0\"><ContentItem source=\"INVALID_SOURCE\" sourceAccount=\"\" isPresetable=\"true\" /></preset></nowSelectionUpdated></updates>");
  CHECK(e.tag == "nowSelectionUpdated" && e.presetId == 0);
  e = parse::event("<updates deviceID=\"X\"><nowPlayingUpdated><nowPlaying source=\"TUNEIN\"><ContentItem source=\"TUNEIN\" location=\"/v1/playback/station/s15200\" /></nowPlaying></nowPlayingUpdated></updates>");
  CHECK(e.tag == "nowPlayingUpdated" && e.location == "/v1/playback/station/s15200");
  e = parse::event("<SoundTouchSdkInfo serverVersion=\"4\" serverBuild=\"x\" />");
  CHECK(e.tag == "SoundTouchSdkInfo" && e.presetId == -1);
  e = parse::event("<updates deviceID=\"X\"><volumeUpdated><volume><targetvolume>20</targetvolume></volume></volumeUpdated></updates>");
  CHECK(e.tag == "volumeUpdated");

  // --- réponse SSDP réelle
  String ssdp = "HTTP/1.1 200 OK\r\nST:urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
                "USN:uuid:BO5EBO5E-F00D-F00D-FEED-0CAE7D5422F4::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
                "Location: http://192.168.1.10:8091/XD/BO5EBO5E-F00D-F00D-FEED-0CAE7D5422F4.xml\r\n\r\n";
  CHECK(parse::ssdpBoseMac(ssdp) == "0CAE7D5422F4");
  CHECK(parse::ssdpBoseMac("HTTP/1.1 200 OK\r\nUSN:uuid:1234::upnp:rootdevice\r\n\r\n") == "");

  // --- échappement aller-retour
  CHECK(xml::decode(xml::escape("a&b<c>\"d'e")) == "a&b<c>\"d'e");
  CHECK(xml::escape("La Musique d'Inter") == "La Musique d&apos;Inter");

  printf(failures ? "%d échec(s)\n" : "parse_test : tout est OK\n", failures);
  return failures ? 1 : 0;
}
