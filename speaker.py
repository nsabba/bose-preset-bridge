"""
speaker.py - client for the local interfaces of a Bose SoundTouch speaker.

Every request sent here is documented in PROTOCOL.md (same section names), so that
the ESP32 port can reproduce them byte for byte.

    8090/tcp  HTTP "webservices" API   (/info, /presets, /now_playing, /key, /storePreset)
    8091/tcp  UPnP MediaRenderer        (AVTransport SetAVTransportURI / Play)
    17000/tcp TAP console               ("sys reboot")
    8080/tcp  WebSocket, subprotocol "gabbo" (events, handled in bridge.py)
"""

import base64
import html
import json
import logging
import socket
import time
import xml.etree.ElementTree as ET
from typing import Optional
from xml.sax.saxutils import escape, quoteattr

import requests

log = logging.getLogger("bridge")

HTTP_PORT = 8090
WS_PORT = 8080
UPNP_PORT = 8091
TAP_PORT = 17000

PRESET_IDS = range(1, 7)

# Methods to write a preset into the speaker memory (see probe_presets.py).
# "none" = not validated yet: the bridge never writes presets.
PRESET_METHODS = ("upnp", "lir_direct", "lir_local")


class SpeakerError(Exception):
    pass


def is_cloud_content(preset: dict) -> bool:
    """True if a stored preset still points to the (dead) Bose cloud."""
    return preset.get("source") == "TUNEIN" or "bose.io" in preset.get("location", "")


def encode_station_data(name: str, stream_url: str, image_url: str = "") -> str:
    """Same payload as the Bose 'orion' adapter: base64 of {name, imageUrl, streamUrl}."""
    raw = json.dumps({"name": name, "imageUrl": image_url, "streamUrl": stream_url},
                     separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_station_data(data: str) -> dict:
    data = data.strip()
    padded = data + "=" * (-len(data) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return json.loads(decoder(padded).decode("utf-8"))
        except Exception:
            continue
    raise ValueError("invalid station data")


def station_json(info: dict) -> dict:
    """Answer served on /orion/station for LOCAL_INTERNET_RADIO presets (method lir_local).
    Mimics the Bose bmx adapter; the stream URL is repeated at every place a firmware
    might look for it."""
    url = info.get("streamUrl", "")
    stream = {"streamUrl": url, "hasPlaylist": False, "isRealtime": True}
    return {
        "name": info.get("name", ""),
        "imageUrl": info.get("imageUrl", ""),
        "streamType": "liveRadio",
        "isFavorite": False,
        "audio": dict(stream, maxTimeout=60, streams=[stream]),
        "streamUrl": url,
        "streams": [stream],
    }


def preset_content(method: str, stream_url: str, name: str, logo_url: str = "",
                   station_base: str = "") -> dict:
    """ContentItem attributes to store a stream in a preset with the given method."""
    if method == "upnp":
        return {"source": "UPNP", "location": stream_url, "sourceAccount": "UPnPUserName"}
    if method == "lir_direct":
        return {"source": "LOCAL_INTERNET_RADIO", "type": "stationurl", "location": stream_url}
    if method == "lir_local":
        data = encode_station_data(name, stream_url, logo_url)
        return {"source": "LOCAL_INTERNET_RADIO", "type": "stationurl",
                "location": f"{station_base}/orion/station?data={data}"}
    raise ValueError(f"unknown preset method {method!r}")


def preset_xml(preset_id: int, content: dict, name: str) -> str:
    attrs = "".join(f" {k}={quoteattr(v)}" for k, v in content.items())
    return (f'<preset id="{preset_id}"><ContentItem{attrs} isPresetable="true">'
            f"<itemName>{escape(name)}</itemName></ContentItem></preset>")


def _parse_content_item(el: Optional[ET.Element]) -> dict:
    if el is None:
        return {"source": "", "type": "", "location": "", "account": "", "name": ""}
    return {
        "source": el.get("source", ""),
        "type": el.get("type", ""),
        "location": el.get("location", ""),
        "account": el.get("sourceAccount", ""),
        "name": (el.findtext("itemName") or "").strip(),
    }


def parse_presets(xml_text: str) -> list:
    root = ET.fromstring(xml_text)
    out = []
    for p in root.iter("preset"):
        item = _parse_content_item(p.find("ContentItem"))
        item["id"] = int(p.get("id", "0"))
        item["updated_on"] = p.get("updatedOn", "")
        out.append(item)
    return sorted(out, key=lambda x: x["id"])


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

SOAP_GET_TRANSPORT_INFO = """\
<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
  <InstanceID>0</InstanceID>
</u:GetTransportInfo>"""


def build_metadata(stream_url: str, label: str, logo_url: str) -> str:
    """DIDL-Lite metadata, XML-escaped for embedding in the SOAP body."""
    didl = DIDL_TEMPLATE.format(title=html.escape(label), logo=html.escape(logo_url),
                                uri=html.escape(stream_url))
    return html.escape(didl)


class Speaker:
    def __init__(self, host: str, mac: str = "", timeout: float = 5.0):
        self.host = host
        self.mac = mac.replace(":", "").upper()
        self.timeout = timeout
        self._control_url: Optional[str] = None

    @property
    def base(self) -> str:
        return f"http://{self.host}:{HTTP_PORT}"

    # -- HTTP API (port 8090) ----------------------------------------------

    def _get(self, path: str, timeout: Optional[float] = None) -> ET.Element:
        try:
            r = requests.get(self.base + path, timeout=timeout or self.timeout)
            r.raise_for_status()
            return ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError) as exc:
            raise SpeakerError(f"GET {path}: {exc}") from exc

    def _post(self, path: str, body: str, timeout: Optional[float] = None) -> requests.Response:
        try:
            return requests.post(self.base + path, data=body.encode("utf-8"),
                                 headers={"Content-Type": "application/xml"},
                                 timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise SpeakerError(f"POST {path}: {exc}") from exc

    def info(self, timeout: Optional[float] = None) -> dict:
        root = self._get("/info", timeout)
        return {
            "name": (root.findtext("name") or "").strip(),
            "type": (root.findtext("type") or "").strip(),
            "device_id": root.get("deviceID", ""),
            "firmware": (root.findtext(".//softwareVersion") or "").split(" ")[0],
        }

    def presets(self) -> list:
        try:
            r = requests.get(self.base + "/presets", timeout=self.timeout)
            r.raise_for_status()
            return parse_presets(r.text)
        except (requests.RequestException, ET.ParseError) as exc:
            raise SpeakerError(f"GET /presets: {exc}") from exc

    def now_playing(self, timeout: Optional[float] = None) -> dict:
        root = self._get("/now_playing", timeout)
        item = _parse_content_item(root.find("ContentItem"))
        return {
            "source": root.get("source", ""),
            "play_status": (root.findtext("playStatus") or "").strip(),
            "item_name": item["name"] or (root.findtext("stationName") or "").strip(),
            "location": item["location"],
        }

    @staticmethod
    def is_playing_upnp(np: dict) -> bool:
        return np.get("source") == "UPNP" and np.get("play_status") == "PLAY_STATE"

    def send_key(self, key: str) -> None:
        """Short press (press immediately followed by release).
        Never hold a PRESET_n key: a long press stores the current content in it."""
        for state in ("press", "release"):
            r = self._post("/key", f'<key state="{state}" sender="Gabbo">{key}</key>')
            if r.status_code != 200:
                raise SpeakerError(f"POST /key {key} {state}: HTTP {r.status_code}")

    def store_preset(self, preset_id: int, content: dict, name: str) -> requests.Response:
        return self._post("/storePreset", preset_xml(preset_id, content, name), timeout=10)

    def remove_preset(self, preset_id: int) -> requests.Response:
        return self._post("/removePreset", f'<preset id="{preset_id}"></preset>', timeout=10)

    # -- UPnP (port 8091) ----------------------------------------------------

    def control_url(self) -> str:
        if self._control_url is None:
            self._control_url = self._discover_control_url()
        return self._control_url

    def _discover_control_url(self) -> str:
        default = f"http://{self.host}:{UPNP_PORT}/AVTransport/Control"
        if not self.mac:
            return default
        desc_url = f"http://{self.host}:{UPNP_PORT}/XD/BO5EBO5E-F00D-F00D-FEED-{self.mac}.xml"
        ns = "{urn:schemas-upnp-org:device-1-0}"
        try:
            r = requests.get(desc_url, timeout=5)
            r.raise_for_status()
            for svc in ET.fromstring(r.content).iter(ns + "service"):
                if "AVTransport" in svc.findtext(ns + "serviceType", ""):
                    ctrl = svc.findtext(ns + "controlURL", "")
                    if ctrl:
                        url = ctrl if ctrl.startswith("http") else f"http://{self.host}:{UPNP_PORT}" + ctrl
                        log.debug("Discovered AVTransport control URL: %s", url)
                        return url
        except Exception as exc:
            log.debug("UPnP discovery failed (%s), using default endpoint", exc)
        return default

    def _soap(self, action: str, body_xml: str) -> Optional[str]:
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        }
        envelope = SOAP_ENVELOPE.format(body=body_xml)
        try:
            r = requests.post(self.control_url(), data=envelope.encode("utf-8"),
                              headers=headers, timeout=10)
            if r.status_code != 200:
                log.error("SOAP %s: HTTP %s - %s", action, r.status_code, r.text[:200])
                return None
            return r.text
        except requests.RequestException as exc:
            log.error("SOAP %s failed: %s", action, exc)
            return None

    def play_url(self, stream_url: str, label: str, logo_url: str = "") -> bool:
        metadata = build_metadata(stream_url, label, logo_url)
        if self._soap("SetAVTransportURI",
                      SOAP_SET_URI.format(uri=escape(stream_url), metadata=metadata)) is None:
            return False
        if self._soap("Play", SOAP_PLAY) is None:
            return False
        log.info("UPnP SetAVTransportURI + Play sent (%s)", label)
        return True

    def transport_state(self) -> Optional[str]:
        text = self._soap("GetTransportInfo", SOAP_GET_TRANSPORT_INFO)
        if text is None:
            return None
        try:
            el = ET.fromstring(text).find(".//CurrentTransportState")
            return el.text if el is not None else None
        except ET.ParseError:
            return None

    # -- TAP console (port 17000) --------------------------------------------

    def reboot(self) -> bool:
        """Reboot through the TAP console. Validated on site 2026-09-23: connect, wait ~1 s
        (the console prints a prompt), send "sys reboot\\n", close. Full reboot ~2 min."""
        try:
            with socket.create_connection((self.host, TAP_PORT), timeout=5) as s:
                time.sleep(1.0)
                s.settimeout(0.5)
                try:
                    s.recv(1024)
                except (socket.timeout, OSError):
                    pass
                s.sendall(b"sys reboot\n")
                time.sleep(0.5)
            return True
        except OSError as exc:
            log.error("Reboot via TAP port %d failed: %s", TAP_PORT, exc)
            return False


def stream_reachable(url: str, timeout: float = 6.0) -> bool:
    """Check from the bridge that a stream answers with audio bytes."""
    try:
        with requests.get(url, stream=True, timeout=timeout,
                          headers={"Icy-MetaData": "0"}) as r:
            if not 200 <= r.status_code < 300:
                return False
            chunk = next(r.iter_content(1024), b"")
            return len(chunk) > 0
    except Exception:
        return False
