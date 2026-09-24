"""
webui.py - settings page and HTTP API (port 8888).

The page (web/index.html, one self-contained file) only talks to this API, which is
specified in PROTOCOL.md so that the ESP32 version can serve the same page unchanged.

    GET  /                              the page
    GET  /api/status
    GET  /api/presets
    PUT  /api/presets/{n}               {"name", "stream_url", "logo_url"}
    POST /api/play/{n}
    POST /api/test                      {"stream_url", "name"?, "logo_url"?}
    POST /api/reboot-speaker
    POST /api/rewrite-speaker-presets
    GET  /catalog/radios.json           bundled copy of the catalogue (fallback)
    GET  /orion/station?data=...        station JSON for LOCAL_INTERNET_RADIO presets
"""

import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from speaker import PRESET_IDS, decode_station_data, station_json

log = logging.getLogger("bridge")

MAX_BODY = 8192


def resource_path(rel: str) -> str:
    """File bundled with PyInstaller (--add-data) or next to this module."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _read_resource(rel: str) -> bytes:
    try:
        with open(resource_path(rel), "rb") as f:
            return f.read()
    except OSError:
        return b""


def _valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/]+", url or "")) and len(url) <= 1024


class Handler(BaseHTTPRequestHandler):
    bridge = None
    page = b""
    catalog = b""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("HTTP %s %s", self.address_string(), fmt % args)

    # -- responses -------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, error: str, message: str = "") -> None:
        self._json(code, {"ok": False, "error": error, "message": message or error})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    @staticmethod
    def _preset_id(path: str, prefix: str):
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", path)
        if m and int(m.group(1)) in PRESET_IDS:
            return int(m.group(1))
        return None

    # -- routes ------------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        b = self.bridge
        u = urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            self._send(200, self.page or b"page missing", "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, b.status())
        elif path == "/api/presets":
            self._json(200, {"presets": b.presets_list()})
        elif path == "/catalog/radios.json" and self.catalog:
            self._send(200, self.catalog, "application/json; charset=utf-8")
        elif path == "/orion/station":
            try:
                info = decode_station_data(parse_qs(u.query).get("data", [""])[0])
            except ValueError:
                return self._error(400, "bad_data")
            log.info("Station JSON served to %s: %s", self.client_address[0], info.get("name"))
            self._json(200, station_json(info))
        elif path == "/status":     # V1 compatibility
            item = b.player.playing_item or {}
            self._json(200, {"preset": item.get("id"), "label": item.get("name", "-"),
                             "logo_url": item.get("logo_url", ""),
                             "stream_url": item.get("stream_url", "")})
        else:
            self._error(404, "not_found")

    def do_PUT(self):
        n = self._preset_id(urlparse(self.path).path, "/api/presets/")
        if n is None:
            return self._error(404, "not_found")
        try:
            data = self._body()
        except ValueError as exc:
            return self._error(400, "bad_json", str(exc))
        name = " ".join(str(data.get("name", "")).split())[:64]
        stream_url = str(data.get("stream_url", "")).strip()
        logo_url = str(data.get("logo_url", "")).strip()
        if stream_url and not _valid_url(stream_url):
            return self._error(400, "bad_stream_url", "stream_url must start with http:// or https://")
        if logo_url and not _valid_url(logo_url):
            return self._error(400, "bad_logo_url", "logo_url must start with http:// or https://")
        if stream_url and not name:
            return self._error(400, "missing_name", "name is required")
        try:
            result = self.bridge.update_preset(n, name, stream_url, logo_url)
        except OSError as exc:
            log.error("Saving preset %d failed: %s", n, exc)
            return self._error(500, "save_failed", str(exc))
        self._json(200, result)

    def do_POST(self):
        b = self.bridge
        path = urlparse(self.path).path
        try:
            data = self._body()
        except ValueError as exc:
            return self._error(400, "bad_json", str(exc))
        if path.startswith("/api/play/"):
            n = self._preset_id(path, "/api/play/")
            if n is None:
                return self._error(404, "not_found")
            if not b.play_preset(n):
                return self._error(409, "preset_empty", f"preset {n} is not configured")
            self._json(202, {"ok": True, "preset": n})
        elif path == "/api/test":
            url = str(data.get("stream_url", "")).strip()
            if not _valid_url(url):
                return self._error(400, "bad_stream_url", "stream_url must start with http:// or https://")
            b.test_stream(url, str(data.get("name", ""))[:64], str(data.get("logo_url", "")))
            self._json(202, {"ok": True})
        elif path == "/api/reboot-speaker":
            if not b.reboot_speaker("web page"):
                return self._error(409, "busy", "a reboot is already running")
            self._json(202, {"ok": True, "message": "reboot sent, about 2 minutes"})
        elif path == "/api/rewrite-speaker-presets":
            r = b.rewrite_speaker_presets(reason="web page")
            code = 200 if r.get("ok") else (409 if r.get("error") in ("busy", "method_not_validated") else 502)
            self._json(code, r)
        else:
            self._error(404, "not_found")


def start(bridge, port: int) -> ThreadingHTTPServer:
    Handler.bridge = bridge
    Handler.page = _read_resource(os.path.join("web", "index.html"))
    Handler.catalog = _read_resource(os.path.join("catalog", "radios.json"))
    if not Handler.page:
        log.error("web/index.html not found: the settings page will be empty")
    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    return server
