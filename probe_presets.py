#!/usr/bin/env python3
"""
probe_presets.py - finds out how to write a preset into a Bose SoundTouch without the cloud.

Standalone: Python 3.8+ standard library only (websocket-client is used if installed,
to show the speaker events). Also built as ProbePresets.exe by the GitHub workflow.

Safety rules:
  * only ONE preset is touched (5 by default); preset 1 is refused;
  * all presets are saved to presets_backup_<date>.xml before anything is written;
  * every write asks for confirmation (unless --yes).

Tested variants (in this order, stops at the first validated one):
  upnp        source="UPNP" location=<stream> sourceAccount="UPnPUserName"
  lir_direct  source="LOCAL_INTERNET_RADIO" type="stationurl" location=<stream>
  lir_local   source="LOCAL_INTERNET_RADIO" location=http://<this PC>:<port>/orion/station?data=...
              (this script serves that URL while it runs)
  remove      POST /removePreset (only with --variant remove)

A variant is VALIDATED when:
  1. GET /presets shows the new content in the preset, and
  2. after a short press on the preset (POST /key) the speaker plays it by itself,
     or a UPnP Play sent right after (what the bridge does) plays.

Usage:
  python probe_presets.py                      (host read from config.ini / config.yaml)
  python probe_presets.py --host 192.168.1.17
  python probe_presets.py --variant lir_direct --preset 4
  python probe_presets.py --restore presets_backup_20260924-120000.xml
"""

import argparse
import base64
import configparser
import datetime
import html
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape, quoteattr

VARIANTS = ("upnp", "lir_direct", "lir_local", "remove")
DEFAULT_STREAM = "http://icecast.radiofrance.fr/fip-midfi.mp3"
DEFAULT_NAME = "FIP"

LOG_LINES = []


def out(msg=""):
    msg = str(msg).encode("ascii", "replace").decode("ascii")
    print(msg, flush=True)
    LOG_LINES.append(msg)


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def http(method, url, body=None, headers=None, timeout=6):
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


class Speaker:
    def __init__(self, host):
        self.host = host
        self.base = f"http://{host}:8090"

    def get(self, path, timeout=6):
        return http("GET", self.base + path, timeout=timeout)

    def post(self, path, xml, timeout=10):
        return http("POST", self.base + path, xml, {"Content-Type": "application/xml"}, timeout)

    def presets(self):
        code, text = self.get("/presets")
        if code != 200:
            raise RuntimeError(f"GET /presets -> {code} {text[:200]}")
        res = {}
        for p in ET.fromstring(text).iter("preset"):
            ci = p.find("ContentItem")
            res[int(p.get("id"))] = {
                "source": ci.get("source", "") if ci is not None else "",
                "type": ci.get("type", "") if ci is not None else "",
                "location": ci.get("location", "") if ci is not None else "",
                "account": ci.get("sourceAccount", "") if ci is not None else "",
                "name": (ci.findtext("itemName") or "") if ci is not None else "",
            }
        return text, res

    def now_playing(self):
        code, text = self.get("/now_playing", timeout=4)
        if code != 200:
            return None
        root = ET.fromstring(text)
        ci = root.find("ContentItem")
        return {"source": root.get("source", ""),
                "status": (root.findtext("playStatus") or "").strip(),
                "name": (ci.findtext("itemName") or "") if ci is not None else "",
                "location": ci.get("location", "") if ci is not None else ""}

    def key(self, key):
        for state in ("press", "release"):
            self.post("/key", f'<key state="{state}" sender="Gabbo">{key}</key>', timeout=5)

    def upnp_play(self, url, title):
        ctrl = f"http://{self.host}:8091/AVTransport/Control"
        didl = (f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                f'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                f'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                f'<item id="1" parentID="0" restricted="1"><dc:title>{html.escape(title)}</dc:title>'
                f'<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
                f'<res protocolInfo="http-get:*:audio/mpeg:*">{html.escape(url)}</res></item></DIDL-Lite>')
        env = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>{}</s:Body></s:Envelope>')
        set_uri = ('<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID>'
                   f'<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>'
                   '</u:SetAVTransportURI>')
        play = ('<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID>'
                '<Speed>1</Speed></u:Play>')
        for action, body in (("SetAVTransportURI", set_uri), ("Play", play)):
            code, text = http("POST", ctrl, env.format(body), {
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"'})
            out(f"      UPnP {action} -> HTTP {code}")
            if code != 200:
                return False
        return True

    def reboot(self):
        with socket.create_connection((self.host, 17000), timeout=5) as s:
            time.sleep(1.0)
            s.settimeout(0.5)
            try:
                s.recv(1024)
            except OSError:
                pass
            s.sendall(b"sys reboot\n")
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# WebSocket event listener (optional)
# ---------------------------------------------------------------------------

class Events:
    def __init__(self, host):
        self.host = host
        self.messages = []
        self._stop = threading.Event()
        self._thread = None
        self.available = True

    def start(self):
        try:
            import websocket  # noqa: F401
        except ImportError:
            self.available = False
            return
        self.messages = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        time.sleep(1.0)

    def _run(self):
        import websocket
        try:
            ws = websocket.create_connection(f"ws://{self.host}:8080", subprotocols=["gabbo"], timeout=1)
        except Exception as e:
            self.messages.append(f"(websocket connection failed: {e})")
            return
        while not self._stop.is_set():
            try:
                msg = ws.recv()
                if isinstance(msg, str):
                    self.messages.append(msg)
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
        try:
            ws.close()
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)

    def summary(self):
        lines = []
        for m in self.messages:
            try:
                root = ET.fromstring(m)
                child = root[0] if root.tag == "updates" and len(root) else root
                extra = ""
                p = child.find(".//preset")
                if p is not None:
                    extra = f" preset={p.get('id')}"
                ci = child.find(".//ContentItem")
                if ci is not None:
                    extra += f" source={ci.get('source')}"
                if child.tag == "errorUpdate":
                    extra += " " + ET.tostring(child, encoding="unicode")[:200]
                lines.append(child.tag + extra)
            except ET.ParseError:
                lines.append(m[:120])
        return lines


# ---------------------------------------------------------------------------
# Local station server (variant lir_local)
# ---------------------------------------------------------------------------

STATION_HITS = []


def station_json(info):
    url = info.get("streamUrl", "")
    stream = {"streamUrl": url, "hasPlaylist": False, "isRealtime": True}
    return {"name": info.get("name", ""), "imageUrl": info.get("imageUrl", ""),
            "streamType": "liveRadio", "isFavorite": False,
            "audio": dict(stream, maxTimeout=60, streams=[stream]),
            "streamUrl": url, "streams": [stream]}


class StationHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        STATION_HITS.append(f"{self.client_address[0]} GET {self.path} UA={self.headers.get('User-Agent')}")
        u = urlparse(self.path)
        data = parse_qs(u.query).get("data", [""])[0]
        try:
            padded = data + "=" * (-len(data) % 4)
            info = json.loads(base64.urlsafe_b64decode(padded))
            body = json.dumps(station_json(info)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        except Exception:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def local_ip_towards(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def content_for(variant, stream, name, station_base):
    if variant == "upnp":
        return {"source": "UPNP", "location": stream, "sourceAccount": "UPnPUserName"}
    if variant == "lir_direct":
        return {"source": "LOCAL_INTERNET_RADIO", "type": "stationurl", "location": stream}
    if variant == "lir_local":
        raw = json.dumps({"name": name, "imageUrl": "", "streamUrl": stream}, separators=(",", ":"))
        data = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        return {"source": "LOCAL_INTERNET_RADIO", "type": "stationurl",
                "location": f"{station_base}/orion/station?data={data}"}
    raise ValueError(variant)


def preset_body(pid, content, name):
    attrs = "".join(f" {k}={quoteattr(v)}" for k, v in content.items())
    return (f'<preset id="{pid}"><ContentItem{attrs} isPresetable="true">'
            f"<itemName>{escape(name)}</itemName></ContentItem></preset>")


def ask(question, assume_yes):
    if assume_yes:
        out(f"{question} [o/n] o (--yes)")
        return True
    try:
        ans = input(f"{question} [o/n] ").strip().lower()
    except EOFError:
        ans = ""
    LOG_LINES.append(f"{question} -> {ans}")
    return ans in ("o", "oui", "y", "yes")


def print_presets(presets):
    for pid in sorted(presets):
        p = presets[pid]
        loc = p["location"]
        if len(loc) > 80:
            loc = loc[:77] + "..."
        out(f"   {pid}: {p['source']:<22} {p['name'][:20]:<20} {loc}")


def wait_playing(sp, seconds, label):
    """Poll /now_playing every 2 s. Returns the last state."""
    np = None
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(2)
        np = sp.now_playing()
        if np is None:
            out(f"      [{label}] /now_playing: no answer")
            continue
        out(f"      [{label}] source={np['source']} playStatus={np['status']} item={np['name']!r}")
        if np["status"] == "PLAY_STATE":
            return np
    return np


def wait_back(sp, timeout=300):
    out("   Waiting for the speaker to go down...")
    t0 = time.time()
    while time.time() - t0 < 90:
        if sp.get("/info", timeout=2)[0] != 200:
            break
        time.sleep(3)
    else:
        out("   !! The speaker never went down: reboot command ignored?")
        return False
    out("   Speaker is down, waiting for it to come back (about 2 min)...")
    while time.time() - t0 < timeout:
        if sp.get("/info", timeout=2)[0] == 200:
            out(f"   Speaker back after {int(time.time() - t0)} s")
            time.sleep(10)
            return True
        time.sleep(5)
    out("   !! Speaker not back after 5 min")
    return False


def probe_variant(sp, events, variant, pid, stream, name, station_base, args):
    out("")
    out(f"=== Variant {variant} on preset {pid} ===")
    result = {"variant": variant, "stored": False, "event": False, "native": False,
              "upnp_after": None, "verdict": "FAILED"}

    if variant == "remove":
        body = f'<preset id="{pid}"></preset>'
        path = "/removePreset"
    else:
        content = content_for(variant, stream, name, station_base)
        body = preset_body(pid, content, name)
        path = "/storePreset"
    out(f"   POST {path}")
    out(f"   body: {body}")
    if not ask(f"   Send this to preset {pid}?", args.yes):
        result["verdict"] = "SKIPPED"
        return result
    code, text = sp.post(path, body)
    out(f"   -> HTTP {code}: {text[:400]}")

    time.sleep(1)
    _, presets = sp.presets()
    now = presets.get(pid)
    out(f"   GET /presets, preset {pid} is now: {now}")
    if variant == "remove":
        result["stored"] = now is None or not now["location"]
    else:
        result["stored"] = bool(now) and now["source"] == content["source"] \
            and now["location"] == content["location"]
    if not result["stored"]:
        out("   -> NOT stored")
        return result
    out("   -> stored OK")

    if args.no_press:
        result["verdict"] = "STORED (press not tested)"
        return result

    out(f"   Short press on PRESET_{pid} (POST /key press+release)...")
    events.start()
    sp.key(f"PRESET_{pid}")
    np = wait_playing(sp, 12, "after press")
    events.stop()
    if events.available:
        ev = events.summary()
        out(f"   WebSocket events: {', '.join(ev) if ev else '(none)'}")
        result["event"] = any(e.startswith("nowSelectionUpdated") for e in ev)
    else:
        out("   (websocket-client not installed: events not checked)")
        result["event"] = None
    if STATION_HITS:
        out("   Requests received by the local station server:")
        for h in STATION_HITS:
            out(f"      {h}")
    if np and np["status"] == "PLAY_STATE":
        result["native"] = True
        out(f"   -> the speaker plays it BY ITSELF (source={np['source']})")
    else:
        out("   -> no native playback; sending UPnP Play like the bridge does...")
        sp.upnp_play(stream, name)
        np2 = wait_playing(sp, 10, "after UPnP")
        result["upnp_after"] = bool(np2 and np2["source"] == "UPNP" and np2["status"] == "PLAY_STATE")
        if not result["upnp_after"]:
            out("   !! UPnP does not play any more: the playback engine may be stuck.")
            if ask("   Reboot the speaker now (TAP port 17000, ~2 min)?", args.yes):
                sp.reboot()
                wait_back(sp)

    if result["native"] or (result["upnp_after"] and result["event"] is not False):
        result["verdict"] = "VALIDATED"
    return result


def save_method(method):
    path = os.path.join(base_dir(), "config.ini")
    if not os.path.exists(path):
        out(f"   (no config.ini next to this program: add 'preset_write_method = {method}' "
            "to the [bridge] section yourself)")
        return
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    out_lines, section, done = [], None, False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if section == "bridge" and not done:
                out_lines.append(f"preset_write_method = {method}")
                done = True
            section = s[1:-1].strip().lower()
        elif section == "bridge" and s.split("=")[0].strip().lower() == "preset_write_method":
            line = f"preset_write_method = {method}"
            done = True
        out_lines.append(line)
    if not done:
        if section != "bridge":
            out_lines += ["", "[bridge]"]
        out_lines.append(f"preset_write_method = {method}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")
    out(f"   config.ini updated: preset_write_method = {method}")


def restore(sp, backup_file, pid, args):
    root = ET.parse(backup_file).getroot()
    for p in root.iter("preset"):
        if int(p.get("id")) != pid:
            continue
        ci = p.find("ContentItem")
        attrs = {k: v for k, v in ci.attrib.items() if k != "isPresetable"}
        name = ci.findtext("itemName") or ""
        body = preset_body(pid, attrs, name)
        out(f"Restore preset {pid}: {body}")
        if ask("Send?", args.yes):
            code, text = sp.post("/storePreset", body)
            out(f"-> HTTP {code}: {text[:300]}")
        return
    out(f"Preset {pid} not found in {backup_file}")


def read_config():
    """host, stream and name of the tested preset from config.ini / config.yaml, if any."""
    cfg = {"host": None, "presets": {}, "labels": {}}
    ini = os.path.join(base_dir(), "config.ini")
    yml = os.path.join(base_dir(), "config.yaml")
    if os.path.exists(ini):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(ini, encoding="utf-8-sig")
        if cp.has_section("bose"):
            cfg["host"] = cp["bose"].get("host")
        for sect, key in (("presets", "presets"), ("labels", "labels")):
            if cp.has_section(sect):
                cfg[key] = {int(k): v for k, v in cp.items(sect) if v.strip()}
    elif os.path.exists(yml):
        try:
            import yaml
            with open(yml, encoding="utf-8-sig") as f:
                y = yaml.safe_load(f) or {}
            cfg["host"] = y.get("bose_host")
            cfg["presets"] = {int(k): v for k, v in (y.get("presets") or {}).items() if v}
            cfg["labels"] = {int(k): v for k, v in (y.get("preset_labels") or {}).items()}
        except ImportError:
            pass
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Probe how to write SoundTouch presets locally.")
    ap.add_argument("--host", help="speaker IP (default: from config.ini / config.yaml)")
    ap.add_argument("--preset", type=int, default=5, help="preset to test (default 5, never 1)")
    ap.add_argument("--variant", help="comma separated list among " + ", ".join(VARIANTS))
    ap.add_argument("--stream", help="stream URL to store (default: the configured one for this preset)")
    ap.add_argument("--name", help="station name to store")
    ap.add_argument("--port", type=int, default=8899, help="local port for variant lir_local")
    ap.add_argument("--all", action="store_true", help="test all variants even after a success")
    ap.add_argument("--no-press", action="store_true", help="only store, do not press the preset")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmations")
    ap.add_argument("--restore", metavar="BACKUP.xml", help="write back the preset from a backup file")
    args = ap.parse_args()

    cfg = read_config()
    host = args.host or cfg["host"]
    if not host:
        ap.error("no --host and no config.ini / config.yaml found")
    pid = args.preset
    if pid == 1:
        ap.error("preset 1 is never touched by this probe")
    if pid not in range(2, 7):
        ap.error("preset must be between 2 and 6")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(base_dir(), f"probe_presets_{stamp}.log")
    sp = Speaker(host)

    try:
        out(f"probe_presets - {datetime.datetime.now():%Y-%m-%d %H:%M:%S} - speaker {host}")
        code, text = sp.get("/info")
        if code != 200:
            out(f"!! Speaker not reachable on http://{host}:8090/info ({text})")
            return 2
        info = ET.fromstring(text)
        out(f"Speaker: {info.findtext('name')} - {info.findtext('type')} - firmware "
            f"{(info.findtext('.//softwareVersion') or '').split(' ')[0]}")
        np = sp.now_playing()
        out(f"Now playing: {np}")

        if http("GET", "http://127.0.0.1:8888/api/status", timeout=2)[0] == 200 or \
                http("GET", "http://127.0.0.1:8888/status", timeout=2)[0] == 200:
            out("")
            out("!! The bridge seems to run on this PC (port 8888). Quit it first (tray icon > Quitter),")
            out("   otherwise it answers the preset press itself and the test proves nothing.")
            if not ask("Continue anyway?", args.yes):
                return 1

        if args.restore:
            restore(sp, args.restore, pid, args)
            return 0

        xml, presets = sp.presets()
        backup = os.path.join(base_dir(), f"presets_backup_{stamp}.xml")
        with open(backup, "w", encoding="utf-8") as f:
            f.write(xml)
        out(f"Presets saved to {backup}")
        print_presets(presets)

        stream = args.stream or cfg["presets"].get(pid) or DEFAULT_STREAM
        name = args.name or cfg["labels"].get(pid) or (DEFAULT_NAME if stream == DEFAULT_STREAM else f"Preset {pid}")
        out(f"Test content: {name} - {stream}")

        variants = [v.strip() for v in args.variant.split(",")] if args.variant else ["upnp", "lir_direct", "lir_local"]
        for v in variants:
            if v not in VARIANTS:
                ap.error(f"unknown variant {v}")

        station_base = ""
        if "lir_local" in variants:
            ip = local_ip_towards(host)
            station_base = f"http://{ip}:{args.port}"
            srv = HTTPServer(("", args.port), StationHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            out(f"Local station server: {station_base}/orion/station")

        events = Events(host)
        results = []
        for v in variants:
            STATION_HITS.clear()
            r = probe_variant(sp, events, v, pid, stream, name, station_base, args)
            results.append(r)
            out(f"   => {v}: {r['verdict']}")
            if r["verdict"] == "VALIDATED" and not args.all:
                break

        out("")
        out("=== Summary ===")
        for r in results:
            out(f"   {r['variant']:<11} stored={r['stored']} event={r['event']} native_play={r['native']} "
                f"upnp_after={r['upnp_after']} -> {r['verdict']}")
        ok = [r for r in results if r["verdict"] == "VALIDATED"]
        _, presets = sp.presets()
        out(f"Preset {pid} is now: {presets.get(pid)}")
        if ok:
            method = ok[0]["variant"]
            out("")
            out(f"VALIDATED METHOD: {method}")
            if method != "remove" and ask(f"Write 'preset_write_method = {method}' into config.ini?", args.yes):
                save_method(method)
        else:
            out("")
            out("No method validated. The backup allows to restore the preset:")
            out(f"   python probe_presets.py --restore {os.path.basename(backup)} --preset {pid}")
        return 0
    finally:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(LOG_LINES) + "\n")
        print(f"(log written to {log_path})")


if __name__ == "__main__":
    sys.exit(main())
