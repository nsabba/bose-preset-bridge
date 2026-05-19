#!/usr/bin/env python3
"""
bose-preset-bridge — relays Bose SoundTouch preset button presses to UPnP AVTransport.
Works entirely locally; no Bose cloud dependency.
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import requests
import websocket
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _base_dir() -> str:
    """Directory containing the executable (works both frozen and plain Python)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def setup_logging(level: str) -> logging.Logger:
    fmt = "[%(asctime)s] %(levelname)-5s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    # On Windows without a console, also write to a log file next to the exe
    if sys.platform == "win32":
        log_path = os.path.join(_base_dir(), "bose-bridge.log")
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(format=fmt, datefmt=datefmt,
                        level=getattr(logging, level.upper(), logging.INFO),
                        handlers=handlers)
    return logging.getLogger("bridge")


log = logging.getLogger("bridge")

# ---------------------------------------------------------------------------
# Now-playing state (shared between WebSocket thread and HTTP server)
# ---------------------------------------------------------------------------

now_playing: dict = {"preset": None, "label": "—", "logo_url": "", "stream_url": ""}

# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

HTML = """\
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Valentine</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #111827;
      color: #f9fafb;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .card {
      text-align: center;
      padding: 3rem 2rem;
      background: #1f2937;
      border-radius: 24px;
      box-shadow: 0 25px 60px rgba(0,0,0,.5);
      width: min(360px, 90vw);
    }
    .logo-wrap {
      width: 140px;
      height: 140px;
      margin: 0 auto 1.5rem;
      border-radius: 20px;
      overflow: hidden;
      background: #374151;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 4rem;
    }
    .logo-wrap img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      padding: 12px;
    }
    h1 { font-size: 1.6rem; font-weight: 700; letter-spacing: -.02em; }
    .sub { margin-top: .5rem; color: #9ca3af; font-size: .85rem; }
    .dot {
      display: inline-block;
      width: 8px; height: 8px;
      background: #22c55e;
      border-radius: 50%;
      margin-right: 6px;
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0%,100% { opacity: 1; } 50% { opacity: .3; }
    }
    .idle .dot { background: #6b7280; animation: none; }
  </style>
</head>
<body>
  <div class="card" id="card">
    <div class="logo-wrap" id="logo"></div>
    <h1 id="label">—</h1>
    <p class="sub" id="sub"></p>
  </div>
  <script>
    function update() {
      fetch('/status').then(r => r.json()).then(d => {
        const card = document.getElementById('card');
        const logo = document.getElementById('logo');
        const label = document.getElementById('label');
        const sub = document.getElementById('sub');
        const playing = !!d.preset;

        label.textContent = d.label;

        if (d.logo_url) {
          logo.innerHTML = '<img src="' + d.logo_url + '" alt="" onerror="this.parentNode.textContent=\'\\uD83D\\uDCFB\'">';
        } else {
          logo.textContent = '📻';
        }

        if (playing) {
          card.classList.remove('idle');
          sub.innerHTML = '<span class="dot"></span>En direct · Preset ' + d.preset;
        } else {
          card.classList.add('idle');
          sub.innerHTML = '<span class="dot"></span>En attente';
        }
      }).catch(() => {});
    }
    update();
    setInterval(update, 2000);
  </script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # silence HTTP access logs

    def do_GET(self):
        if self.path == "/status":
            body = json.dumps(now_playing).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def start_web_server(port: int):
    server = HTTPServer(("", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Web UI → http://localhost:%d", port)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_ini(path: str) -> dict:
    """Load config from a .ini file (configparser)."""
    import configparser
    ini = configparser.ConfigParser()
    ini.read(path, encoding="utf-8")

    cfg: dict = {}
    bose = ini["bose"] if "bose" in ini else {}
    cfg["bose_host"] = bose.get("host", "")
    cfg["bose_name"] = bose.get("name", "Bose")
    cfg["bose_mac"]  = bose.get("mac", "")

    cfg["presets"] = {
        int(k): v for k, v in ini.items("presets") if v.strip()
    } if ini.has_section("presets") else {}

    cfg["preset_labels"] = dict(
        (int(k), v) for k, v in ini.items("labels")
    ) if ini.has_section("labels") else {}

    cfg["preset_logos"] = dict(
        (int(k), v) for k, v in ini.items("logos")
    ) if ini.has_section("logos") else {}

    bridge = ini["bridge"] if "bridge" in ini else {}
    cfg["web_port"]                  = int(bridge.get("web_port", 8888))
    cfg["reconnect_delay_seconds"]   = int(bridge.get("reconnect_delay", 5))
    cfg["reconnect_max_delay_seconds"] = int(bridge.get("reconnect_max_delay", 60))
    cfg["log_level"]                 = bridge.get("log_level", "INFO")
    return cfg


def load_config(path: str = "config.yaml") -> dict:
    base = _base_dir()
    # Prefer config.ini if present next to the executable
    ini_path = os.path.join(base, "config.ini")
    if os.path.exists(ini_path):
        return _load_ini(ini_path)
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["presets"] = {
        int(k): v for k, v in (cfg.get("presets") or {}).items() if v
    }
    return cfg

# ---------------------------------------------------------------------------
# UPnP AVTransport
# ---------------------------------------------------------------------------

SOAP_ENVELOPE = """\
<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>{body}</s:Body>
</s:Envelope>"""

SOAP_SET_URI = """\
<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
  <InstanceID>0</InstanceID>
  <CurrentURI>{uri}</CurrentURI>
  <CurrentURIMetaData>{metadata}</CurrentURIMetaData>
</u:SetAVTransportURI>"""

DIDL_TEMPLATE = """\
<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
           xmlns:dc="http://purl.org/dc/elements/1.1/"
           xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
  <item id="1" parentID="0" restricted="1">
    <dc:title>{title}</dc:title>
    <upnp:class>object.item.audioItem.audioBroadcast</upnp:class>
    <upnp:albumArtURI>{logo}</upnp:albumArtURI>
    <res protocolInfo="http-get:*:audio/mpeg:*">{uri}</res>
  </item>
</DIDL-Lite>"""

SOAP_PLAY = """\
<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
  <InstanceID>0</InstanceID>
  <Speed>1</Speed>
</u:Play>"""


def _discover_control_url(host: str, mac: str) -> str:
    mac_clean = mac.replace(":", "").upper()
    desc_url = f"http://{host}:8091/XD/BO5EBO5E-F00D-F00D-FEED-{mac_clean}.xml"
    try:
        r = requests.get(desc_url, timeout=5)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
            stype = svc.findtext("{urn:schemas-upnp-org:device-1-0}serviceType", "")
            if "AVTransport" in stype:
                ctrl = svc.findtext("{urn:schemas-upnp-org:device-1-0}controlURL", "")
                if ctrl:
                    base = f"http://{host}:8091"
                    url = ctrl if ctrl.startswith("http") else base + ctrl
                    log.debug("Discovered AVTransport control URL: %s", url)
                    return url
    except Exception as exc:
        log.debug("UPnP discovery failed (%s), using default endpoint", exc)
    return f"http://{host}:8091/AVTransport/Control"


def _soap_post(url: str, action: str, body_xml: str) -> bool:
    envelope = SOAP_ENVELOPE.format(body=body_xml)
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
    }
    try:
        r = requests.post(url, data=envelope.encode("utf-8"), headers=headers, timeout=10)
        r.raise_for_status()
        return True
    except requests.HTTPError as exc:
        log.error("SOAP %s HTTP error: %s — %s", action, exc, exc.response.text[:200])
    except Exception as exc:
        log.error("SOAP %s failed: %s", action, exc)
    return False


def _build_metadata(stream_url: str, label: str, logo_url: str) -> str:
    """Build a DIDL-Lite metadata string, XML-escaped for embedding in SOAP."""
    import html
    didl = DIDL_TEMPLATE.format(
        title=html.escape(label),
        logo=html.escape(logo_url),
        uri=html.escape(stream_url),
    )
    return html.escape(didl)


def play_stream(control_url: str, stream_url: str, label: str, logo_url: str = "") -> bool:
    metadata = _build_metadata(stream_url, label, logo_url)
    ok = _soap_post(control_url, "SetAVTransportURI",
                    SOAP_SET_URI.format(uri=stream_url, metadata=metadata))
    if not ok:
        return False
    log.info("UPnP SetAVTransportURI → OK")
    ok = _soap_post(control_url, "Play", SOAP_PLAY)
    if not ok:
        return False
    log.info("UPnP Play → OK")
    log.info("✔ %s is playing", label)
    return True

# ---------------------------------------------------------------------------
# WebSocket listener
# ---------------------------------------------------------------------------

def _parse_preset_id(xml_text: str) -> Optional[int]:
    try:
        root = ET.fromstring(xml_text)
        el = root if root.tag == "nowSelectionUpdated" else root.find(".//nowSelectionUpdated")
        if el is None:
            return None
        preset = el.find("preset")
        if preset is not None:
            pid = preset.get("id")
            if pid is not None:
                return int(pid)
    except ET.ParseError:
        pass
    return None


class BoseListener:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.host = cfg["bose_host"]
        self.ws_url = f"ws://{self.host}:8080"
        self.control_url: Optional[str] = None
        self._ws: Optional[websocket.WebSocketApp] = None

    def _get_control_url(self) -> str:
        if self.control_url is None:
            mac = self.cfg.get("bose_mac", "")
            self.control_url = _discover_control_url(self.host, mac)
        return self.control_url

    def _on_open(self, ws):
        log.info("Connected to %s (%s)", self.cfg.get("bose_name", self.host), self.host)

    def _on_message(self, ws, message):
        if not isinstance(message, str):
            return
        log.debug("WS message: %s", message)
        if "<nowSelectionUpdated" not in message:
            return
        preset_id = _parse_preset_id(message)
        if preset_id is None:
            return
        stream_url = self.cfg["presets"].get(preset_id)
        if not stream_url:
            log.debug("Preset %d pressed but not configured — ignoring", preset_id)
            return
        label = self.cfg.get("preset_labels", {}).get(preset_id, f"Preset {preset_id}")
        logo_url = self.cfg.get("preset_logos", {}).get(preset_id, "")
        log.info("Preset %d pressed → playing %s", preset_id, label)
        if play_stream(self._get_control_url(), stream_url, label, logo_url):
            now_playing.update(preset=preset_id, label=label,
                               logo_url=logo_url, stream_url=stream_url)

    def _on_error(self, ws, error):
        log.warning("WebSocket error: %s", error)

    def _on_close(self, ws, code, reason):
        log.info("WebSocket closed (code=%s reason=%s)", code, reason)

    def run_forever(self):
        delay = self.cfg.get("reconnect_delay_seconds", 5)
        max_delay = self.cfg.get("reconnect_max_delay_seconds", 60)
        current_delay = delay

        while True:
            log.info("Connecting to %s ...", self.ws_url)
            ws = websocket.WebSocketApp(
                self.ws_url,
                subprotocols=["gabbo"],
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._ws = ws
            ws.run_forever(ping_interval=30, ping_timeout=10)
            log.info("Reconnecting in %d s ...", current_delay)
            time.sleep(current_delay)
            current_delay = min(current_delay * 2, max_delay)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_upnp(host: str, url: str, mac: str = ""):
    log.info("Testing UPnP on %s", host)
    ctrl = _discover_control_url(host, mac)
    log.info("Control URL: %s", ctrl)
    play_stream(ctrl, url, "test stream")

# ---------------------------------------------------------------------------
# Windows system tray
# ---------------------------------------------------------------------------

def _make_tray_icon_image():
    """Generate a simple speaker icon as a PIL Image."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Dark background circle
    draw.ellipse([0, 0, size - 1, size - 1], fill="#1f2937")
    # White speaker shape
    cx, cy = size // 2, size // 2
    draw.rectangle([18, 24, 28, 40], fill="white")
    draw.polygon([(28, 24), (44, 14), (44, 50), (28, 40)], fill="white")
    # Sound waves
    draw.arc([46, 20, 58, 44], -40, 40, fill="white", width=3)
    return img


# ---------------------------------------------------------------------------
# Preset setup (embedded, called from tray menu)
# ---------------------------------------------------------------------------

PRESET_KEYS = {1: "PRESET_1", 2: "PRESET_2", 3: "PRESET_3",
               4: "PRESET_4", 5: "PRESET_5", 6: "PRESET_6"}


def run_setup_presets(cfg: dict, notify_fn=None):
    """Save all configured preset URLs onto the Bose via play + long-press."""
    host = cfg["bose_host"]
    labels = cfg.get("preset_labels", {})
    ctrl = _discover_control_url(host, cfg.get("bose_mac", ""))

    def _notify(msg):
        log.info(msg)
        if notify_fn:
            notify_fn(msg)

    _notify(f"Configuration des presets sur {cfg.get('bose_name', host)}...")

    for preset_id, stream_url in sorted(cfg["presets"].items()):
        label = labels.get(preset_id, f"Preset {preset_id}")
        key = PRESET_KEYS.get(preset_id)
        if not key:
            continue
        _notify(f"Preset {preset_id} — {label}")
        try:
            _soap_post(ctrl, "SetAVTransportURI",
                       SOAP_SET_URI.format(uri=stream_url,
                                           metadata=_build_metadata(stream_url, label, "")))
            _soap_post(ctrl, "Play", SOAP_PLAY)
        except Exception as exc:
            log.error("Preset %d UPnP failed: %s", preset_id, exc)
            continue

        time.sleep(4)  # wait for stream to start

        # Long-press to save
        try:
            url = f"http://{host}:8090/key"
            headers = {"Content-Type": "application/xml"}
            requests.post(url, data=f'<key state="press" sender="Gabbo">{key}</key>',
                          headers=headers, timeout=5)
            time.sleep(3)
            requests.post(url, data=f'<key state="release" sender="Gabbo">{key}</key>',
                          headers=headers, timeout=5)
        except Exception as exc:
            log.error("Preset %d key press failed: %s", preset_id, exc)
            continue

        time.sleep(2)

    _notify("✔ Configuration terminée !")


# ---------------------------------------------------------------------------
# Windows system tray
# ---------------------------------------------------------------------------

def start_tray(cfg: dict):
    """Run a system tray icon in the main thread (Windows only)."""
    try:
        import pystray
    except ImportError:
        log.warning("pystray not installed — no tray icon (pip install pystray pillow)")
        return False

    icon_img = _make_tray_icon_image()
    bose_name = cfg.get("bose_name", "Bose")
    _setup_running = threading.Event()

    def get_tooltip():
        label = now_playing.get("label", "—")
        return f"{bose_name}  ·  {label}"

    def on_setup(icon, _item):
        if _setup_running.is_set():
            return
        _setup_running.set()

        def _run():
            try:
                run_setup_presets(cfg, notify_fn=lambda m: setattr(icon, "title", m))
            finally:
                _setup_running.clear()
                icon.title = get_tooltip()

        threading.Thread(target=_run, daemon=True).start()

    def on_quit(icon, _item):
        icon.stop()
        os._exit(0)

    tray = pystray.Icon(
        "bose-bridge",
        icon_img,
        title=get_tooltip(),
        menu=pystray.Menu(
            pystray.MenuItem(lambda _: get_tooltip(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Configurer les presets", on_setup),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", on_quit),
        ),
    )

    def _refresh():
        while True:
            time.sleep(3)
            if not _setup_running.is_set():
                try:
                    tray.title = get_tooltip()
                except Exception:
                    pass

    threading.Thread(target=_refresh, daemon=True).start()
    tray.run()
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bose SoundTouch preset → UPnP bridge")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--test", metavar="HOST",
                        help="Test UPnP playback on HOST (uses first preset URL from config)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.get("log_level", "INFO"))

    if args.test:
        first_url = next(iter(cfg["presets"].values()), None)
        if not first_url:
            log.error("No preset URL configured in %s", args.config)
            sys.exit(1)
        test_upnp(args.test, first_url, mac=cfg.get("bose_mac", ""))
        return

    web_port = cfg.get("web_port", 8888)
    if web_port:
        start_web_server(web_port)

    # Start bridge in background thread, tray in main thread on Windows
    listener = BoseListener(cfg)
    if sys.platform == "win32":
        t = threading.Thread(target=listener.run_forever, daemon=True)
        t.start()
        start_tray(cfg)   # blocks until user clicks Quitter
    else:
        listener.run_forever()


if __name__ == "__main__":
    main()
