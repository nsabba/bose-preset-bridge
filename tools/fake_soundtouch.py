#!/usr/bin/env python3
"""
tools/fake_soundtouch.py - minimal SoundTouch simulator to test the bridge (or the
ESP32 port) without the speaker. Standard library only.

Serves on 127.0.0.1: 8080 WebSocket "gabbo", 8090 HTTP API, 8091 UPnP AVTransport,
17000 TAP console. Reproduces the failure seen on site on 2026-09-23: a preset that
still points to the Bose cloud blocks the playback engine; afterwards UPnP Play answers
200 but nothing plays (/now_playing = INVALID_SOURCE) until "sys reboot".

    python tools/fake_soundtouch.py                   presets 2 and 3 are cloud ones
    python tools/fake_soundtouch.py --dup-events      each press sends the event twice
    python tools/fake_soundtouch.py --silent 2        preset 2 sends no event at all

Simulate a remote press:
    curl -X POST -d '<key state="press" sender="Gabbo">PRESET_3</key>' http://127.0.0.1:8090/key
    curl -X POST -d '<key state="release" sender="Gabbo">PRESET_3</key>' http://127.0.0.1:8090/key
"""

import argparse
import base64
import hashlib
import re
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape, quoteattr

HOST = "127.0.0.1"
ARGS = None
LOCK = threading.RLock()
CLOUD = "https://content.api.bose.io/core02/svc-bmx-adapter-orion/prod/orion/station?data=e30"
STATE = {
    "down_until": 0.0,
    "standby": True,
    "stuck": False,
    "source": "STANDBY",
    "play_status": "",
    "uri": "",
    "title": "",
    "presets": {
        1: {"source": "UPNP", "location": "http://icecast.radiofrance.fr/franceinter-midfi.mp3",
            "sourceAccount": "UPnPUserName", "name": "France Inter"},
        2: {"source": "LOCAL_INTERNET_RADIO", "location": CLOUD, "name": "franceinfo (cloud)"},
        3: {"source": "TUNEIN", "location": "/v1/playback/station/s15200", "name": "TuneIn (cloud)"},
    },
}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def is_down():
    return time.time() < STATE["down_until"]


# ---------------------------------------------------------------------------
# WebSocket (gabbo)
# ---------------------------------------------------------------------------

WS_CLIENTS = []


def ws_frame(opcode, payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def ws_broadcast(xml):
    data = ws_frame(1, xml.encode())
    for c in list(WS_CLIENTS):
        try:
            c.sendall(data)
        except OSError:
            WS_CLIENTS.remove(c)


def ws_close_all():
    for c in list(WS_CLIENTS):
        try:
            c.shutdown(socket.SHUT_RDWR)
            c.close()
        except OSError:
            pass
    WS_CLIENTS.clear()


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def ws_client(sock):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = sock.recv(4096)
            if not chunk:
                return
            req += chunk
        if is_down():
            sock.close()
            return
        key = re.search(rb"Sec-WebSocket-Key: *(\S+)", req, re.I).group(1)
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\nSec-WebSocket-Protocol: gabbo\r\n\r\n")
        WS_CLIENTS.append(sock)
        sock.sendall(ws_frame(1, b'<SoundTouchSdkInfo serverVersion="4" serverBuild="fake" />'))
        while True:
            b1, b2 = recv_exact(sock, 2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", recv_exact(sock, 2))[0]
            elif n == 127:
                n = struct.unpack("!Q", recv_exact(sock, 8))[0]
            mask = recv_exact(sock, 4) if b2 & 0x80 else b"\0\0\0\0"
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(recv_exact(sock, n)))
            if opcode == 9:
                sock.sendall(ws_frame(10, payload))
            elif opcode == 8:
                break
    except (OSError, ConnectionError, AttributeError):
        pass
    finally:
        if sock in WS_CLIENTS:
            WS_CLIENTS.remove(sock)
        try:
            sock.close()
        except OSError:
            pass


def serve_ws():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, 8080))
    srv.listen()
    while True:
        c, _ = srv.accept()
        threading.Thread(target=ws_client, args=(c,), daemon=True).start()


# ---------------------------------------------------------------------------
# HTTP API 8090 and UPnP 8091
# ---------------------------------------------------------------------------

def presets_xml():
    parts = []
    for pid, p in sorted(STATE["presets"].items()):
        attrs = "".join(f" {k}={quoteattr(v)}" for k, v in p.items() if k != "name")
        parts.append(f'<preset id="{pid}"><ContentItem{attrs} isPresetable="true">'
                     f'<itemName>{escape(p.get("name", ""))}</itemName></ContentItem></preset>')
    return '<?xml version="1.0" encoding="UTF-8" ?><presets>' + "".join(parts) + "</presets>"


def now_playing_xml():
    s = STATE
    if s["standby"]:
        return '<nowPlaying deviceID="FAKE" source="STANDBY"><ContentItem source="STANDBY" isPresetable="false" /></nowPlaying>'
    if s["source"] != "UPNP":
        return '<nowPlaying deviceID="FAKE" source="INVALID_SOURCE"><ContentItem source="INVALID_SOURCE" isPresetable="true" /></nowPlaying>'
    return (f'<nowPlaying deviceID="FAKE" source="UPNP" sourceAccount="UPnPUserName">'
            f'<ContentItem source="UPNP" location={quoteattr(s["uri"])} sourceAccount="UPnPUserName" isPresetable="false">'
            f'<itemName>{escape(s["title"])}</itemName></ContentItem>'
            f'<playStatus>{s["play_status"]}</playStatus></nowPlaying>')


def press_preset(pid):
    p = STATE["presets"].get(pid)
    STATE["standby"] = False
    if pid not in ARGS.silent:
        ev = (f'<updates deviceID="FAKE"><nowSelectionUpdated><preset id="{pid}">'
              f'<ContentItem source="{p["source"] if p else ""}" isPresetable="true" /></preset>'
              f'</nowSelectionUpdated></updates>')
        ws_broadcast(ev)
        if ARGS.dup_events:
            ws_broadcast(ev)
    if p and (p["source"] == "TUNEIN" or "bose.io" in p["location"]):
        log(f"preset {pid}: cloud content -> playback engine STUCK until reboot")
        STATE["stuck"] = True
        ws_broadcast(f'<updates deviceID="FAKE"><nowPlayingUpdated><nowPlaying source="{p["source"]}">'
                     f'<ContentItem source="{p["source"]}" location={quoteattr(p["location"])} />'
                     f'</nowPlaying></nowPlayingUpdated></updates>')
    STATE["source"], STATE["play_status"] = "INVALID_SOURCE", ""


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, body, ctype="text/xml"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _down(self):
        if is_down():
            self.close_connection = True
            self.connection.close()
            return True
        return False

    def do_GET(self):
        if self._down():
            return
        with LOCK:
            if self.path == "/info":
                self._reply(200, '<info deviceID="FAKE"><name>Fake</name><type>SoundTouch 20</type>'
                                 '<components><component><softwareVersion>27.0.6.fake</softwareVersion>'
                                 '</component></components></info>')
            elif self.path == "/presets":
                self._reply(200, presets_xml())
            elif self.path == "/now_playing":
                self._reply(200, now_playing_xml())
            else:
                self._reply(404, "<error/>")

    def do_POST(self):
        if self._down():
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        with LOCK:
            if self.server.server_port == 8091:
                return self.upnp(body)
            if self.path == "/key":
                root = ET.fromstring(body)
                key, state = root.text, root.get("state")
                log(f"key {key} {state}")
                if state == "release":
                    if key.startswith("PRESET_"):
                        press_preset(int(key[7:]))
                    elif key == "POWER":
                        STATE["standby"] = not STATE["standby"]
                self._reply(200, "<status>/key</status>")
            elif self.path == "/storePreset":
                p = ET.fromstring(body)
                ci = p.find("ContentItem")
                entry = {k: v for k, v in ci.attrib.items() if k != "isPresetable"}
                entry["name"] = ci.findtext("itemName") or ""
                STATE["presets"][int(p.get("id"))] = entry
                log(f"storePreset {p.get('id')} -> {entry['source']} {entry['location']}")
                self._reply(200, presets_xml())
            elif self.path == "/removePreset":
                STATE["presets"].pop(int(ET.fromstring(body).get("id")), None)
                self._reply(200, presets_xml())
            else:
                self._reply(404, "<error/>")

    def upnp(self, body):
        action = self.headers.get("SOAPAction", "").strip('"').split("#")[-1]
        if action == "SetAVTransportURI":
            STATE["uri"] = re.search(r"<CurrentURI>(.*?)</CurrentURI>", body, re.S).group(1)
            m = re.search(r"dc:title&gt;(.*?)&lt;", body)
            STATE["title"] = m.group(1) if m else ""
        elif action == "Play":
            if STATE["stuck"]:
                log("UPnP Play -> 200 but engine stuck (PAUSED_PLAYBACK / INVALID_SOURCE)")
            elif STATE["standby"] and ARGS.standby_ignores_upnp:
                log("UPnP Play ignored: standby")
            else:
                STATE["standby"] = False
                STATE["source"], STATE["play_status"] = "UPNP", "BUFFERING_STATE"
                threading.Timer(1.5, lambda: STATE.update(play_status="PLAY_STATE")).start()
                log(f"UPnP Play -> playing {STATE['title']}")
        state = "PLAYING" if STATE["play_status"] == "PLAY_STATE" else "PAUSED_PLAYBACK"
        self._reply(200, f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                         f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                         f'<CurrentTransportState>{state}</CurrentTransportState>'
                         f'</u:{action}Response></s:Body></s:Envelope>')


# ---------------------------------------------------------------------------
# TAP console 17000
# ---------------------------------------------------------------------------

def tap_client(sock):
    try:
        sock.sendall(b"->")
        line = sock.recv(256).decode(errors="replace")
        if line.strip() == "sys reboot" and line.endswith("\n"):
            log(f"TAP: sys reboot -> down for {ARGS.reboot_seconds} s")
            with LOCK:
                STATE.update(down_until=time.time() + ARGS.reboot_seconds, stuck=False, standby=True,
                             source="STANDBY", play_status="")
            ws_close_all()
    finally:
        sock.close()


def serve_tap():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, 17000))
    srv.listen()
    while True:
        c, _ = srv.accept()
        threading.Thread(target=tap_client, args=(c,), daemon=True).start()


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--dup-events", action="store_true")
    ap.add_argument("--silent", type=int, nargs="*", default=[])
    ap.add_argument("--standby-ignores-upnp", action="store_true")
    ap.add_argument("--reboot-seconds", type=int, default=20)
    ARGS = ap.parse_args()
    for port in (8090, 8091):
        srv = ThreadingHTTPServer((HOST, port), Api)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=serve_ws, daemon=True).start()
    threading.Thread(target=serve_tap, daemon=True).start()
    log("fake SoundTouch on 127.0.0.1 (8080 ws, 8090 api, 8091 upnp, 17000 tap)")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
