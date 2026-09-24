#!/usr/bin/env python3
"""
bose-preset-bridge - relays Bose SoundTouch preset button presses to UPnP AVTransport.
Works entirely locally; no Bose cloud dependency.

V2: playback watchdog (/now_playing) with retry and automatic reboot, debounce,
presets rewritten into the speaker (no more cloud content), web editor on port 8888.
Every request to the speaker is documented in PROTOCOL.md.
"""

__version__ = "2.0.0"

import argparse
import datetime
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from typing import Optional

import websocket

import webui
from configfile import base_dir, load_config, save_presets
from speaker import (PRESET_IDS, PRESET_METHODS, WS_PORT, Speaker, SpeakerError,
                     is_cloud_content, preset_content, stream_reachable)

log = logging.getLogger("bridge")

# ---------------------------------------------------------------------------
# Logging (ASCII only: the Windows log must stay readable with Get-Content)
# ---------------------------------------------------------------------------

_ASCII_MAP = {"→": "->", "✔": "OK", "—": "-", "–": "-", "·": "-",
              "…": "...", "’": "'", "«": '"', "»": '"', " ": " "}


def to_ascii(text: str) -> str:
    for k, v in _ASCII_MAP.items():
        text = text.replace(k, v)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.encode("ascii", "replace").decode("ascii")


class AsciiFormatter(logging.Formatter):
    def format(self, record):
        return to_ascii(super().format(record))


def setup_logging(level: str) -> None:
    fmt = AsciiFormatter("[%(asctime)s] %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = []
    if sys.stderr is not None:          # None in a PyInstaller --noconsole exe
        handlers.append(logging.StreamHandler())
    if sys.platform == "win32":
        handlers.append(logging.handlers.RotatingFileHandler(
            os.path.join(base_dir(), "bose-bridge.log"),
            maxBytes=1_000_000, backupCount=3, encoding="ascii", errors="replace"))
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.CRITICAL)


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def local_ip_towards(host: str) -> str:
    """LAN address of this machine, as seen from the speaker (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host or "192.168.1.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Player: UPnP play + watchdog + automatic reboot
# ---------------------------------------------------------------------------

class Player:
    """Plays an item {id, name, stream_url, logo_url} and checks it really plays.

    After Play, /now_playing is polled every 2 s for up to watchdog_timeout seconds.
    Not playing -> SetAVTransportURI + Play once more (POWER first if in STANDBY).
    Still not playing -> reboot via TAP (max once per auto_reboot_min_interval), wait
    for the speaker (WebSocket reconnected + /info answers), replay the last preset.
    A newer request always supersedes the one in progress."""

    POLL = 2.0

    def __init__(self, bridge: "Bridge"):
        self.b = bridge
        self._lock = threading.Lock()
        self._seq = 0
        self.rebooting = False
        self._pending = None            # item requested while rebooting
        self.last_item = None           # last preset requested (replayed after a reboot)
        self.playing_item = None
        self.last_auto_reboot = None    # epoch seconds
        self.last_auto_reboot_iso = None
        self.state = "idle"
        self.message = ""

    # -- helpers -------------------------------------------------------------

    def _set(self, state: str, message: str, level=logging.INFO) -> None:
        self.state, self.message = state, message
        log.log(level, "[player] %s", message)

    def _current(self, seq: int) -> bool:
        return seq == self._seq

    def _sleep(self, seq: int, seconds: float) -> bool:
        """Sleep, return False as soon as the request is superseded."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not self._current(seq):
                return False
            time.sleep(0.2)
        return self._current(seq)

    # -- public --------------------------------------------------------------

    def play(self, item: dict, allow_reboot: bool = True, remember: bool = True) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
            if remember:
                self.last_item = item
            if self.rebooting:
                self._pending = item
                self._set("rebooting", f"reboot in progress: {item['name']} will be played "
                                       "when the speaker is back")
                return
        threading.Thread(target=self._run, args=(seq, item, allow_reboot),
                         name="player", daemon=True).start()

    def reboot(self, reason: str, replay: bool) -> bool:
        """Manual reboot (tray / web). Returns False if one is already running."""
        with self._lock:
            if self.rebooting:
                return False
            self._seq += 1
            self.rebooting = True
            self._pending = None
        threading.Thread(target=self._reboot_and_wait, args=(reason, replay),
                         name="reboot", daemon=True).start()
        return True

    # -- internals -----------------------------------------------------------

    def _run(self, seq: int, item: dict, allow_reboot: bool) -> None:
        cfg = self.b.cfg
        sp = self.b.speaker
        name = item["name"]
        self._set("starting", f"play {name} ({item['stream_url']})")
        if not cfg["watchdog"]:
            if sp.play_url(item["stream_url"], name, item.get("logo_url", "")):
                self.playing_item = item
                self._set("playing", f"{name}: Play sent (watchdog disabled)")
            else:
                self._set("failed", f"{name}: UPnP Play failed", logging.ERROR)
            return

        result = self._start_and_verify(seq, item)
        if result in ("ok", "superseded"):
            return
        if result == "unreachable":
            self._set("failed", f"{name}: speaker unreachable, nothing more to do", logging.ERROR)
            return
        if not allow_reboot:
            self._set("failed", f"{name}: still not playing (no automatic reboot for this request)",
                      logging.ERROR)
            return
        if not cfg["auto_reboot"]:
            self._set("failed", f"{name}: still not playing (auto_reboot disabled)", logging.ERROR)
            return
        if not stream_reachable(item["stream_url"]):
            self._set("failed", f"{name}: the stream itself does not answer from this PC "
                                "-> dead stream, no reboot", logging.ERROR)
            return
        min_interval = cfg["auto_reboot_min_interval_seconds"]
        if self.last_auto_reboot and time.time() - self.last_auto_reboot < min_interval:
            ago = int(time.time() - self.last_auto_reboot)
            self._set("failed", f"{name}: still not playing, but last automatic reboot was "
                                f"{ago} s ago (< {min_interval} s): anti-loop, no reboot", logging.ERROR)
            return
        with self._lock:
            if not self._current(seq) or self.rebooting:
                return
            self.rebooting = True
            self._pending = None
        self.last_auto_reboot = time.time()
        self.last_auto_reboot_iso = now_iso()
        self._reboot_and_wait("watchdog: playback engine stuck", replay=True)

    def _start_and_verify(self, seq: int, item: dict) -> str:
        sp = self.b.speaker
        name = item["name"]
        power_sent = False
        attempt = 0
        while attempt < 2:
            attempt += 1
            if not self._current(seq):
                return "superseded"
            sent = sp.play_url(item["stream_url"], name, item.get("logo_url", ""))
            if not sent:
                log.warning("[player] %s: UPnP Play not accepted (attempt %d)", name, attempt)
            res, np = self._wait_playing(seq)
            if res == "ok":
                self.playing_item = item
                self._set("playing", f"{name} is playing (checked on /now_playing, attempt {attempt})")
                return "ok"
            if res in ("superseded", "unreachable"):
                return res
            if res == "standby" and not power_sent:
                log.warning("[player] speaker in STANDBY after Play -> sending POWER then Play again")
                try:
                    sp.send_key("POWER")
                except SpeakerError as exc:
                    log.error("[player] POWER key failed: %s", exc)
                power_sent = True
                attempt -= 1        # the standby round does not count as the retry
                if not self._sleep(seq, 3):
                    return "superseded"
                continue
            state = f"source={np.get('source')} playStatus={np.get('play_status')}" if np else "no answer"
            if attempt < 2:
                self._set("retrying", f"{name}: not playing after {self.b.cfg['watchdog_timeout_seconds']} s "
                                      f"({state}) -> SetAVTransportURI + Play again", logging.WARNING)
            else:
                self._set("failed", f"{name}: still not playing after retry ({state})", logging.ERROR)
        return "failed"

    def _wait_playing(self, seq: int):
        """Poll /now_playing. Returns (result, last_now_playing)."""
        sp = self.b.speaker
        deadline = time.monotonic() + self.b.cfg["watchdog_timeout_seconds"]
        np, standby_count, answered = None, 0, False
        while time.monotonic() < deadline:
            if not self._sleep(seq, self.POLL):
                return "superseded", np
            try:
                np = sp.now_playing(timeout=3)
                answered = True
            except SpeakerError as exc:
                log.debug("[player] /now_playing: %s", exc)
                continue
            log.debug("[player] now_playing source=%s status=%s", np["source"], np["play_status"])
            if Speaker.is_playing_upnp(np):
                return "ok", np
            standby_count = standby_count + 1 if np["source"] == "STANDBY" else 0
            if standby_count >= 2:
                return "standby", np
        if not answered:
            return "unreachable", None
        return "failed", np

    def _reboot_and_wait(self, reason: str, replay: bool) -> None:
        b = self.b
        back = False
        try:
            self._set("rebooting", f"REBOOT of the speaker ({reason}) via TAP port 17000", logging.WARNING)
            b.listener.fast_reconnect = True
            if not b.speaker.reboot():
                self._set("failed", "reboot command could not be sent", logging.ERROR)
                return
            # 1. wait until it goes down
            t0 = time.monotonic()
            down = False
            while time.monotonic() - t0 < 90:
                time.sleep(3)
                if not b.ws_connected.is_set() or not self._info_ok():
                    down = True
                    break
            if not down:
                self._set("failed", "speaker did not go down within 90 s: reboot command ignored?",
                          logging.ERROR)
                return
            log.info("[player] speaker is down, waiting for it to come back (about 2 min)")
            # 2. wait until WebSocket reconnected and /info answers
            while time.monotonic() - t0 < 360:
                time.sleep(5)
                if b.ws_connected.is_set() and self._info_ok():
                    back = True
                    break
            if not back:
                self._set("failed", "speaker not back 6 min after the reboot", logging.ERROR)
                return
            log.info("[player] speaker back after %d s", int(time.monotonic() - t0))
            time.sleep(5)       # let the firmware settle
        finally:
            b.listener.fast_reconnect = False
            with self._lock:
                self.rebooting = False
                pending, self._pending = self._pending, None
            if not back and self.state == "rebooting":
                self._set("failed", "reboot did not complete", logging.ERROR)
        target = pending or (self.last_item if replay else None)
        if target:
            log.info("[player] replaying %s after the reboot", target["name"])
            self.play(target, remember=False)
        else:
            self._set("idle", "speaker back after reboot")

    def _info_ok(self) -> bool:
        try:
            self.b.speaker.info(timeout=3)
            return True
        except SpeakerError:
            return False


# ---------------------------------------------------------------------------
# WebSocket listener
# ---------------------------------------------------------------------------

def parse_event(xml_text: str):
    """Returns (tag, preset_id or None, content_location or None) for a gabbo message."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, None, None
    el = root[0] if root.tag == "updates" and len(root) else root
    preset_id, location = None, None
    p = el.find(".//preset")
    if p is not None and p.get("id", "").isdigit():
        preset_id = int(p.get("id"))
    ci = el.find(".//ContentItem")
    if ci is not None:
        location = ci.get("location")
    return el.tag, preset_id, location


QUIET_EVENTS = {"volumeUpdated", "userActivityUpdate", "SoundTouchSdkInfo", "connectionStateUpdated"}


class BoseListener:
    def __init__(self, bridge: "Bridge"):
        self.b = bridge
        self.initial_delay = bridge.cfg["reconnect_delay_seconds"]
        self.max_delay = bridge.cfg["reconnect_max_delay_seconds"]
        self.current_delay = self.initial_delay
        self.fast_reconnect = False
        self._last_press = {}           # preset id -> monotonic time of last accepted press

    @property
    def ws_url(self) -> str:
        return f"ws://{self.b.speaker.host}:{WS_PORT}"

    def _on_open(self, ws):
        log.info("Connected to %s (%s)", self.b.cfg["bose_name"], self.b.speaker.host)
        self.current_delay = self.initial_delay
        self.b.ws_connected.set()
        threading.Thread(target=self.b.on_speaker_connected, name="on-connect", daemon=True).start()

    def _on_message(self, ws, message):
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        tag, preset_id, location = parse_event(message)
        if tag is None:
            return
        if tag in QUIET_EVENTS:
            log.debug("WS %s", tag)
        else:
            log.info("WS %s%s", tag, f" preset={preset_id}" if preset_id is not None else "")
        log.debug("WS raw: %s", message)
        if tag == "nowSelectionUpdated" and preset_id in PRESET_IDS:
            self.press(preset_id)
        elif tag == "nowPlayingUpdated" and location:
            # Fallback when no nowSelectionUpdated arrives: the speaker started the cloud
            # content of one of its presets (only cloud content, never what we push).
            pid = self.b.preset_for_cloud_location(location)
            if pid is not None:
                self.press(pid, inferred=True)

    def press(self, preset_id: int, inferred: bool = False) -> None:
        now = time.monotonic()
        window = 15.0 if inferred else self.b.cfg["debounce_seconds"]
        last = self._last_press.get(preset_id)
        if last is not None and now - last < window:
            log.info("Preset %d: duplicate event %.1f s after the previous one -> ignored",
                     preset_id, now - last)
            return
        self._last_press[preset_id] = now
        self.b.on_preset_pressed(preset_id, inferred)

    def _on_error(self, ws, error):
        log.warning("WebSocket error: %s", error)

    def _on_close(self, ws, code, reason):
        self.b.ws_connected.clear()
        log.info("WebSocket closed (code=%s reason=%s)", code, reason)

    def run_forever(self):
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
            try:
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                log.warning("WebSocket loop error: %s", exc)
            self.b.ws_connected.clear()
            delay = min(self.current_delay, 5) if self.fast_reconnect else self.current_delay
            log.info("Reconnecting in %d s ...", delay)
            time.sleep(delay)
            self.current_delay = min(self.current_delay * 2, self.max_delay)


# ---------------------------------------------------------------------------
# Bridge: shared state used by the listener, the web API and the tray
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg: dict, config_path: str, config_kind: str):
        self.cfg = cfg
        self.config_path = config_path
        self.config_kind = config_kind
        self.cfg_lock = threading.RLock()
        self.speaker = Speaker(cfg["bose_host"], cfg["bose_mac"])
        self.ws_connected = threading.Event()
        self.player = Player(self)
        self.listener = BoseListener(self)
        self.last_preset = None                 # {id, name, at, inferred}
        self.speaker_presets = []               # cache of GET /presets
        self._rewrite_lock = threading.Lock()
        self.last_rewrite = None                # result of the last rewrite
        self.notify = lambda msg: None          # replaced by the tray
        self._np_cache = (0.0, None)

    @property
    def lan_ip(self) -> str:
        return local_ip_towards(self.cfg["bose_host"])   # recomputed: the PC may change IP (DHCP)

    # -- presets from the config -----------------------------------------------

    def preset_item(self, n: int) -> Optional[dict]:
        with self.cfg_lock:
            url = self.cfg["presets"].get(n, "")
            if not url:
                return None
            return {"id": n, "stream_url": url,
                    "name": self.cfg["preset_labels"].get(n, f"Preset {n}"),
                    "logo_url": self.cfg["preset_logos"].get(n, "")}

    def presets_list(self) -> list:
        out = []
        with self.cfg_lock:
            for n in PRESET_IDS:
                out.append({"id": n,
                            "name": self.cfg["preset_labels"].get(n, ""),
                            "stream_url": self.cfg["presets"].get(n, ""),
                            "logo_url": self.cfg["preset_logos"].get(n, "")})
        return out

    def update_preset(self, n: int, name: str, stream_url: str, logo_url: str) -> dict:
        with self.cfg_lock:
            for key, value in (("presets", stream_url), ("preset_labels", name), ("preset_logos", logo_url)):
                if value:
                    self.cfg[key][n] = value
                else:
                    self.cfg[key].pop(n, None)
            save_presets(self.config_path, self.config_kind, self.cfg["presets"],
                         self.cfg["preset_labels"], self.cfg["preset_logos"])
        log.info("Preset %d saved in %s: %s - %s", n, os.path.basename(self.config_path), name, stream_url)
        result = {"ok": True, "preset": n, "config": "saved"}
        if not stream_url:
            result["speaker"] = "skipped (empty preset)"
        elif self.cfg["preset_write_method"] not in PRESET_METHODS:
            result["speaker"] = "skipped (preset_write_method = none)"
        else:
            r = self.rewrite_speaker_presets(only=[n], reason=f"preset {n} edited")
            result["speaker"] = "written" if r.get("ok") else f"failed: {r.get('message', r.get('error'))}"
        return result

    # -- speaker presets ---------------------------------------------------------

    def station_base(self) -> str:
        return f"http://{self.lan_ip}:{self.cfg['web_port']}"

    def refresh_speaker_presets(self) -> list:
        self.speaker_presets = self.speaker.presets()
        return self.speaker_presets

    def preset_for_cloud_location(self, location: str) -> Optional[int]:
        for p in self.speaker_presets:
            if p["location"] == location and is_cloud_content(p) and p["id"] in PRESET_IDS:
                return p["id"]
        return None

    def _needs_rewrite(self, p: dict) -> bool:
        item = self.preset_item(p["id"])
        if item is None:
            return False
        if is_cloud_content(p):
            return True
        wanted = preset_content(self.cfg["preset_write_method"], item["stream_url"], item["name"],
                                item["logo_url"], self.station_base())
        return p["source"] != wanted["source"] or p["location"] != wanted["location"]

    def rewrite_speaker_presets(self, only=None, reason: str = "manual") -> dict:
        """Store the configured streams into the speaker presets (method preset_write_method).
        Presets 2-6 first, each one checked with GET /presets; preset 1 is written last and
        only if every other write succeeded (it is the one that must never break)."""
        method = self.cfg["preset_write_method"]
        if method not in PRESET_METHODS:
            return {"ok": False, "error": "method_not_validated",
                    "message": "preset_write_method = none: run probe_presets first"}
        if not self._rewrite_lock.acquire(blocking=False):
            return {"ok": False, "error": "busy", "message": "a rewrite is already running"}
        try:
            ids = sorted(only) if only else [n for n in PRESET_IDS if self.preset_item(n)]
            log.info("Rewriting speaker presets %s (method %s, %s)", ids, method, reason)
            results = {}
            order = [n for n in ids if n != 1] + ([1] if 1 in ids else [])
            for n in order:
                item = self.preset_item(n)
                if item is None:
                    results[n] = {"ok": False, "message": "not configured"}
                    continue
                if n == 1 and any(not results[i]["ok"] for i in results):
                    results[n] = {"ok": False, "message": "skipped: another preset failed, "
                                                          "preset 1 left untouched"}
                    log.warning("Preset 1 not rewritten because another preset failed")
                    continue
                content = preset_content(method, item["stream_url"], item["name"],
                                         item["logo_url"], self.station_base())
                try:
                    r = self.speaker.store_preset(n, content, item["name"])
                    stored = next((p for p in self.refresh_speaker_presets() if p["id"] == n), None)
                    ok = (r.status_code == 200 and stored is not None
                          and stored["source"] == content["source"]
                          and stored["location"] == content["location"])
                    msg = "written" if ok else f"HTTP {r.status_code}, preset now {stored}"
                except SpeakerError as exc:
                    ok, msg = False, str(exc)
                results[n] = {"ok": ok, "message": msg}
                log.log(logging.INFO if ok else logging.ERROR,
                        "Speaker preset %d <- %s: %s", n, item["name"], msg)
            ok = bool(results) and all(r["ok"] for r in results.values())
            self.last_rewrite = {"at": now_iso(), "ok": ok, "reason": reason,
                                 "results": {str(k): v for k, v in results.items()}}
            return {"ok": ok, "method": method, "results": self.last_rewrite["results"]}
        finally:
            self._rewrite_lock.release()

    # -- events ------------------------------------------------------------------

    def on_speaker_connected(self) -> None:
        """After each WebSocket connection: refresh the presets cache and rewrite the
        speaker presets that still point to the Bose cloud (or to an old stream)."""
        try:
            info = self.speaker.info()
            log.info("Speaker: %s, %s, firmware %s", info["name"], info["type"], info["firmware"])
            presets = self.refresh_speaker_presets()
        except SpeakerError as exc:
            log.warning("Speaker not readable after connection: %s", exc)
            return
        for p in presets:
            log.info("Speaker preset %d: %s %s", p["id"], p["source"], p["location"][:90])
        if not self.cfg["rewrite_presets_on_startup"]:
            return
        if self.cfg["preset_write_method"] not in PRESET_METHODS:
            if any(is_cloud_content(p) for p in presets):
                log.warning("Speaker presets still point to the Bose cloud, but "
                            "preset_write_method = none: not rewritten")
            return
        todo = [p["id"] for p in presets if self._needs_rewrite(p)]
        configured = {p["id"] for p in presets}
        todo += [n for n in PRESET_IDS if n not in configured and self.preset_item(n)]
        if todo:
            log.info("Speaker presets %s differ from the configuration -> rewriting", sorted(todo))
            r = self.rewrite_speaker_presets(only=todo, reason="startup check")
            self.notify("Presets de l'enceinte reecrits" if r["ok"] else
                        "Reecriture des presets incomplete (voir log)")

    def on_preset_pressed(self, n: int, inferred: bool = False) -> None:
        item = self.preset_item(n)
        how = "inferred from the cloud content started" if inferred else "event"
        if item is None:
            log.info("Preset %d pressed (%s) but not configured - ignoring", n, how)
            return
        log.info("Preset %d pressed (%s) -> %s", n, how, item["name"])
        self.last_preset = {"id": n, "name": item["name"], "at": now_iso(), "inferred": inferred}
        self.player.play(item)

    def play_preset(self, n: int) -> bool:
        item = self.preset_item(n)
        if item is None:
            return False
        log.info("Web: play preset %d (%s)", n, item["name"])
        self.last_preset = {"id": n, "name": item["name"], "at": now_iso(), "inferred": False}
        self.player.play(item)
        return True

    def test_stream(self, stream_url: str, name: str = "", logo_url: str = "") -> None:
        log.info("Web: test %s", stream_url)
        self.player.play({"id": 0, "name": name or "Test", "stream_url": stream_url,
                          "logo_url": logo_url}, allow_reboot=False, remember=False)

    def reboot_speaker(self, reason: str = "manual") -> bool:
        return self.player.reboot(reason, replay=False)

    # -- status ------------------------------------------------------------------

    def now_playing(self) -> Optional[dict]:
        ts, np = self._np_cache
        if time.monotonic() - ts < 2:
            return np
        try:
            np = self.speaker.now_playing(timeout=2)
        except SpeakerError:
            np = None
        self._np_cache = (time.monotonic(), np)
        return np

    def page_url(self) -> str:
        return f"http://{self.lan_ip}:{self.cfg['web_port']}/"

    def status(self) -> dict:
        p = self.player
        return {
            "version": __version__,
            "speaker": {"name": self.cfg["bose_name"], "host": self.speaker.host},
            "ws_connected": self.ws_connected.is_set(),
            "now_playing": self.now_playing(),
            "last_preset": self.last_preset,
            "playing": ({"id": p.playing_item["id"], "name": p.playing_item["name"]}
                        if p.playing_item else None),
            "player": {"state": p.state, "message": p.message},
            "last_auto_reboot": p.last_auto_reboot_iso,
            "rebooting": p.rebooting,
            "preset_write_method": self.cfg["preset_write_method"],
            "speaker_presets_cloud": [x["id"] for x in self.speaker_presets if is_cloud_content(x)],
            "last_rewrite": self.last_rewrite,
            "catalog_url": self.cfg["catalog_url"],
            "page_url": self.page_url(),
        }


# ---------------------------------------------------------------------------
# Windows firewall: allow the exe on the private network (web page from the phone)
# ---------------------------------------------------------------------------

FIREWALL_RULE = "BosePresetBridge"


def firewall_rule_ok() -> Optional[bool]:
    """True/False on a frozen Windows exe, None when it cannot be checked."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                            f"name={FIREWALL_RULE}", "verbose"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0 and sys.executable.lower() in r.stdout.lower()
    except Exception as exc:
        log.warning("Firewall check failed: %s", exc)
        return None


def request_firewall_rule() -> None:
    """Ask Windows (UAC prompt) to replace every rule of this exe by one allow rule.
    Deleting first matters: if the first-launch popup was dismissed, Windows created
    block rules, and block rules win over allow rules."""
    import ctypes
    exe = sys.executable
    cmd = (f'/c netsh advfirewall firewall delete rule name=all program="{exe}" & '
           f'netsh advfirewall firewall add rule name="{FIREWALL_RULE}" dir=in action=allow '
           f'program="{exe}" enable=yes profile=private,domain')
    log.info("Requesting the firewall rule (UAC prompt)")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd, None, 0)


def check_firewall(cfg: dict) -> Optional[bool]:
    if not cfg["firewall_check"]:
        return None
    ok = firewall_rule_ok()
    if ok is None:
        return None
    if ok:
        log.info("Firewall: rule %s present", FIREWALL_RULE)
        return True
    marker = os.path.join(base_dir(), ".firewall_requested")
    if not os.path.exists(marker):
        with open(marker, "w") as f:
            f.write(now_iso())
        request_firewall_rule()
    else:
        log.warning("Firewall: no rule for this exe, the settings page is only reachable from "
                    "this PC. Tray menu > 'Autoriser dans le pare-feu' (admin).")
    return False


# ---------------------------------------------------------------------------
# Windows system tray
# ---------------------------------------------------------------------------

def _make_tray_icon_image():
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill="#1f2937")
    draw.rectangle([18, 24, 28, 40], fill="white")
    draw.polygon([(28, 24), (44, 14), (44, 50), (28, 40)], fill="white")
    draw.arc([46, 20, 58, 44], -40, 40, fill="white", width=3)
    return img


def start_tray(bridge: Bridge, firewall_ok: Optional[bool]) -> bool:
    try:
        import pystray
    except ImportError:
        log.warning("pystray not installed - no tray icon (pip install pystray pillow)")
        return False
    import webbrowser

    cfg = bridge.cfg
    state = {"firewall_ok": firewall_ok}

    def label():
        item = bridge.player.playing_item
        if bridge.player.rebooting:
            return "redemarrage de l'enceinte..."
        return item["name"] if item else "-"

    def tooltip():
        conn = "" if bridge.ws_connected.is_set() else " (deconnecte)"
        text = f"{cfg['bose_name']}{conn} - {label()}\n{bridge.page_url()}"
        return text[:127]

    def notify(msg):
        log.info("Tray: %s", msg)
        try:
            tray.notify(msg, cfg["bose_name"])
        except Exception:
            pass

    bridge.notify = notify

    def in_thread(fn):
        def handler(icon, _item):
            threading.Thread(target=fn, daemon=True).start()
        return handler

    def do_rewrite():
        r = bridge.rewrite_speaker_presets(reason="tray")
        if r.get("ok"):
            notify("Presets de l'enceinte reecrits")
        else:
            notify("Reecriture impossible : " + str(r.get("message") or "voir le log"))

    def do_reboot():
        if bridge.reboot_speaker("tray"):
            notify("Redemarrage de l'enceinte (environ 2 min)")
        else:
            notify("Un redemarrage est deja en cours")

    def do_firewall():
        request_firewall_rule()
        time.sleep(10)
        state["firewall_ok"] = firewall_rule_ok()

    def on_quit(icon, _item):
        icon.stop()
        os._exit(0)

    tray = pystray.Icon(
        "bose-bridge", _make_tray_icon_image(), title=tooltip(),
        menu=pystray.Menu(
            pystray.MenuItem(lambda _: f"{cfg['bose_name']} - {label()}", None, enabled=False),
            pystray.MenuItem(lambda _: f"Reglages : {bridge.page_url()}", None, enabled=False),
            pystray.MenuItem("Ouvrir la page de reglages",
                             lambda *_: webbrowser.open(f"http://127.0.0.1:{cfg['web_port']}/"),
                             default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reecrire les presets de l'enceinte", in_thread(do_rewrite)),
            pystray.MenuItem("Redemarrer l'enceinte", in_thread(do_reboot)),
            pystray.MenuItem("Autoriser dans le pare-feu", in_thread(do_firewall),
                             visible=lambda _: state["firewall_ok"] is False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", on_quit),
        ),
    )

    def refresh():
        while True:
            time.sleep(3)
            try:
                tray.title = tooltip()
                tray.update_menu()
            except Exception:
                pass

    threading.Thread(target=refresh, daemon=True).start()
    tray.run()
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bose SoundTouch preset -> UPnP bridge")
    parser.add_argument("--config", default="config.yaml",
                        help="config file (config.ini next to the program wins)")
    parser.add_argument("--test", metavar="HOST",
                        help="test UPnP playback on HOST with the preset 1 stream, then exit")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--self-check", action="store_true",
                        help="exit 0 if the page and the catalogue are bundled (used by the CI)")
    args = parser.parse_args()

    if args.self_check:
        missing = [rel for rel in (os.path.join("web", "index.html"), os.path.join("catalog", "radios.json"))
                   if not os.path.exists(webui.resource_path(rel))]
        sys.exit(1 if missing else 0)

    cfg, path, kind = load_config(args.config)
    setup_logging(cfg["log_level"])
    log.info("bose-preset-bridge %s - config %s", __version__, path)

    if args.test:
        sp = Speaker(args.test, cfg["bose_mac"])
        url = cfg["presets"].get(1) or next(iter(cfg["presets"].values()), None)
        if not url:
            log.error("No preset URL configured")
            sys.exit(1)
        sp.play_url(url, "test stream")
        time.sleep(6)
        log.info("now_playing: %s", sp.now_playing())
        return

    bridge = Bridge(cfg, path, kind)
    log.info("Speaker %s (%s), preset write method: %s", cfg["bose_name"], cfg["bose_host"],
             cfg["preset_write_method"])
    if cfg["web_port"]:
        webui.start(bridge, cfg["web_port"])
        log.info("Settings page: %s", bridge.page_url())
    firewall_ok = check_firewall(cfg)

    if sys.platform == "win32":
        threading.Thread(target=bridge.listener.run_forever, name="listener", daemon=True).start()
        if not start_tray(bridge, firewall_ok):
            while True:
                time.sleep(3600)
    else:
        bridge.listener.run_forever()


if __name__ == "__main__":
    main()
