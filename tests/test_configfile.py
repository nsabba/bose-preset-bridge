"""python -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import configfile  # noqa: E402

INI = """﻿[bose]
host = 192.168.1.17
name = Salon
mac  = A81B6A51E435

[presets]
; comment inside presets
1 = http://a/1.mp3
2 = http://a/2.mp3?x=%20

[labels]
1 = Radio Un
2 = Radio Deux

[bridge]
web_port = 8888
log_level = INFO
"""

YAML = """bose_host: "192.168.1.17"   # the speaker
bose_name: "Salon"

presets:
  1: "http://a/1.mp3"

  2: "http://a/2.mp3"

preset_labels:
  1: "Radio Un"
  2: "Radio Deux"

web_port: 8888   # 0 pour desactiver
log_level: "INFO"
"""


class ConfigTest(unittest.TestCase):
    def write(self, name, text):
        d = tempfile.mkdtemp()
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_ini_load_defaults_and_bom(self):
        path = self.write("x.ini", INI)
        cfg = configfile._normalize(configfile._load_ini(path))
        self.assertEqual(cfg["bose_host"], "192.168.1.17")
        self.assertEqual(cfg["presets"][2], "http://a/2.mp3?x=%20")   # no interpolation
        self.assertEqual(cfg["preset_write_method"], "upnp")           # V2 default
        self.assertEqual(cfg["debounce_seconds"], 3.0)
        self.assertTrue(cfg["watchdog"])

    def test_ini_save_only_touches_preset_sections(self):
        path = self.write("x.ini", INI)
        configfile.save_presets(path, "ini", {1: "http://b/1.mp3", 3: "http://b/3.mp3"},
                                {1: "Chérie FM", 3: "Trois"}, {3: "https://l/3.png"})
        cfg = configfile._normalize(configfile._load_ini(path))
        self.assertEqual(cfg["presets"], {1: "http://b/1.mp3", 3: "http://b/3.mp3"})
        self.assertEqual(cfg["preset_labels"], {1: "Chérie FM", 3: "Trois"})
        self.assertEqual(cfg["preset_logos"], {3: "https://l/3.png"})
        self.assertEqual(cfg["bose_host"], "192.168.1.17")
        text = open(path, encoding="utf-8").read()
        self.assertIn("mac  = A81B6A51E435", text)                  # untouched line kept verbatim
        self.assertIn("[bridge]\nweb_port = 8888", text)
        self.assertTrue(os.path.exists(path + ".bak"))

    def test_yaml_save_only_touches_preset_blocks(self):
        path = self.write("config.yaml", YAML)
        configfile.save_presets(path, "yaml", {1: "http://b/1.mp3", 2: "http://b/2.mp3"},
                                {1: 'Radio "Un"', 2: "Deux"}, {1: "https://l/1.png"})
        cfg = configfile._normalize(configfile._load_yaml(path))
        self.assertEqual(cfg["presets"], {1: "http://b/1.mp3", 2: "http://b/2.mp3"})
        self.assertEqual(cfg["preset_labels"][1], 'Radio "Un"')
        self.assertEqual(cfg["preset_logos"], {1: "https://l/1.png"})
        text = open(path, encoding="utf-8").read()
        self.assertIn('bose_host: "192.168.1.17"   # the speaker', text)
        self.assertIn("web_port: 8888   # 0 pour desactiver", text)
        for key in ("\npresets:", "\npreset_labels:", "\npreset_logos:"):
            self.assertEqual(text.count(key), 1, key)                # replaced, not duplicated


if __name__ == "__main__":
    unittest.main()
