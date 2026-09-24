# Script PlatformIO lancé avant chaque compilation du firmware "bridge".
#
# 1. Compresse la page de la V2 (../web/index.html, le même fichier que le bridge Python)
#    et l'écrit en tableau PROGMEM dans src/bridge/page_html.h.
# 2. Extrait de ../config.yaml les presets, noms et logos par défaut et les écrit dans
#    src/bridge/defaults_gen.h : les valeurs par défaut de l'ESP32 restent ainsi
#    identiques à celles de la V2.
# Les deux fichiers sont générés (non versionnés).

import gzip
import json
import os
import re

try:
    Import("env")  # noqa: F821  (fourni par PlatformIO)
    PROJECT = env["PROJECT_DIR"]  # noqa: F821
except NameError:  # exécution directe : python scripts/embed_page.py
    PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(PROJECT)
OUT_DIR = os.path.join(PROJECT, "src", "bridge")


def write_if_changed(path, text):
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    if old != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def embed_page():
    with open(os.path.join(REPO, "web", "index.html"), "rb") as f:
        raw = f.read()
    gz = gzip.compress(raw, compresslevel=9, mtime=0)
    lines = []
    for i in range(0, len(gz), 20):
        lines.append("  " + ", ".join("0x%02x" % b for b in gz[i:i + 20]) + ",")
    text = (
        "// Généré par scripts/embed_page.py à partir de web/index.html : ne pas modifier.\n"
        "#pragma once\n#include <Arduino.h>\n"
        f"static const size_t PAGE_HTML_GZ_LEN = {len(gz)};  // {len(raw)} octets non compressés\n"
        "static const uint8_t PAGE_HTML_GZ[] PROGMEM = {\n" + "\n".join(lines) + "\n};\n"
    )
    write_if_changed(os.path.join(OUT_DIR, "page_html.h"), text)
    print(f"embed_page: web/index.html {len(raw)} -> {len(gz)} octets gzip")


def yaml_block(text, key):
    """Lit un bloc 'key:' suivi de lignes '  N: "valeur"' (format de config.yaml)."""
    m = re.search(rf"^{key}:\s*\n((?:[ \t]+.*\n|\s*\n)*)", text, re.M)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r'\s+(\d+)\s*:\s*(".*"|[^#]*?)\s*(#.*)?$', line)
            if mm:
                v = mm.group(2)
                out[int(mm.group(1))] = json.loads(v) if v.startswith('"') else v
    return out


def c_string(s):
    return json.dumps(s, ensure_ascii=False)


def gen_defaults():
    with open(os.path.join(REPO, "config.yaml"), encoding="utf-8") as f:
        text = f.read()
    urls = yaml_block(text, "presets")
    names = yaml_block(text, "preset_labels")
    logos = yaml_block(text, "preset_logos")
    rows = []
    for n in range(1, 7):
        rows.append("  {%s, %s, %s}," % (c_string(names.get(n, "")), c_string(urls.get(n, "")),
                                       c_string(logos.get(n, ""))))
    text = (
        "// Généré par scripts/embed_page.py à partir de config.yaml : ne pas modifier.\n"
        "#pragma once\n"
        "struct DefaultPreset { const char *name, *url, *logo; };\n"
        "static const DefaultPreset DEFAULT_PRESETS[6] = {\n" + "\n".join(rows) + "\n};\n"
    )
    write_if_changed(os.path.join(OUT_DIR, "defaults_gen.h"), text)
    print(f"embed_page: {len(urls)} presets par défaut lus dans config.yaml")


embed_page()
gen_defaults()
