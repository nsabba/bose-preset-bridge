#!/usr/bin/env python3
"""
setup_presets.py — saves all preset URLs onto the Bose SoundTouch.
Run once. For each configured preset it:
  1. plays the stream via UPnP
  2. sends a long-press of the preset button via the REST API to save it
"""

import time
import sys
import xml.etree.ElementTree as ET

import requests
import yaml


def load_config(path="config.yaml"):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["presets"] = {int(k): v for k, v in (cfg.get("presets") or {}).items() if v}
    return cfg


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
  <CurrentURIMetaData></CurrentURIMetaData>
</u:SetAVTransportURI>"""

SOAP_PLAY = """\
<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
  <InstanceID>0</InstanceID>
  <Speed>1</Speed>
</u:Play>"""


def soap_post(host, action, body_xml):
    url = f"http://{host}:8091/AVTransport/Control"
    envelope = SOAP_ENVELOPE.format(body=body_xml)
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
    }
    r = requests.post(url, data=envelope.encode(), headers=headers, timeout=10)
    r.raise_for_status()


def play_upnp(host, stream_url):
    soap_post(host, "SetAVTransportURI", SOAP_SET_URI.format(uri=stream_url))
    soap_post(host, "Play", SOAP_PLAY)


def key_longpress(host, key):
    """Simulate a long press (press + 3 s + release) via the Bose REST API."""
    url = f"http://{host}:8090/key"
    headers = {"Content-Type": "application/xml"}
    requests.post(url, data=f'<key state="press" sender="Gabbo">{key}</key>',
                  headers=headers, timeout=5)
    time.sleep(3)
    requests.post(url, data=f'<key state="release" sender="Gabbo">{key}</key>',
                  headers=headers, timeout=5)


def get_presets(host):
    r = requests.get(f"http://{host}:8090/presets", timeout=5)
    r.raise_for_status()
    return r.text


PRESET_KEYS = {1: "PRESET_1", 2: "PRESET_2", 3: "PRESET_3",
               4: "PRESET_4", 5: "PRESET_5", 6: "PRESET_6"}


def main():
    cfg = load_config()
    host = cfg["bose_host"]
    labels = cfg.get("preset_labels", {})

    print(f"Saving presets on {cfg.get('bose_name', host)} ({host})\n")

    for preset_id, stream_url in sorted(cfg["presets"].items()):
        label = labels.get(preset_id, f"Preset {preset_id}")
        key = PRESET_KEYS.get(preset_id)
        if not key:
            print(f"  [{preset_id}] skipped (unsupported preset number)")
            continue

        print(f"  [{preset_id}] {label}")
        print(f"       Playing: {stream_url}")
        try:
            play_upnp(host, stream_url)
        except Exception as e:
            print(f"       ✗ UPnP failed: {e}")
            continue

        print(f"       Waiting 4 s for stream to start...")
        time.sleep(4)

        print(f"       Saving as preset {preset_id} (long press)...")
        try:
            key_longpress(host, key)
        except Exception as e:
            print(f"       ✗ Key press failed: {e}")
            continue

        print(f"       ✔ Saved")
        time.sleep(2)

    print("\nDone. Verifying saved presets:")
    try:
        xml = get_presets(host)
        root = ET.fromstring(xml)
        for p in root.findall("preset"):
            pid = p.get("id")
            ci = p.find("ContentItem")
            loc = ci.get("location", "") if ci is not None else ""
            print(f"  Preset {pid}: {loc}")
    except Exception as e:
        print(f"  Could not read presets: {e}")


if __name__ == "__main__":
    main()
