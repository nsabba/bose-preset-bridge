"""
configfile.py - loads config.ini / config.yaml and rewrites only the preset sections.

config.ini (next to the executable) wins over config.yaml. Every key added in V2 has a
default, so an existing V1 configuration works unchanged.
"""

import json
import os
import re
import sys

DEFAULT_CATALOG_URL = ("https://raw.githubusercontent.com/nsabba/bose-preset-bridge/"
                       "main/catalog/radios.json")

DEFAULTS = {
    "bose_host": "",
    "bose_name": "Bose",
    "bose_mac": "",
    "presets": {},
    "preset_labels": {},
    "preset_logos": {},
    "web_port": 8888,
    "reconnect_delay_seconds": 5,
    "reconnect_max_delay_seconds": 60,
    "log_level": "INFO",
    # V2
    "catalog_url": DEFAULT_CATALOG_URL,
    "debounce_seconds": 3.0,
    "event_play_delay_seconds": 0.7,
    "watchdog": True,
    "watchdog_timeout_seconds": 10,
    "auto_reboot": True,
    "auto_reboot_min_interval_seconds": 900,
    "preset_write_method": "upnp",
    "rewrite_presets_on_startup": True,
    "firewall_check": True,
}

# config.ini [bridge] key -> internal key
INI_BRIDGE_KEYS = {
    "web_port": "web_port",
    "reconnect_delay": "reconnect_delay_seconds",
    "reconnect_max_delay": "reconnect_max_delay_seconds",
    "log_level": "log_level",
    "catalog_url": "catalog_url",
    "debounce_seconds": "debounce_seconds",
    "event_play_delay": "event_play_delay_seconds",
    "watchdog": "watchdog",
    "watchdog_timeout": "watchdog_timeout_seconds",
    "auto_reboot": "auto_reboot",
    "auto_reboot_min_interval": "auto_reboot_min_interval_seconds",
    "preset_write_method": "preset_write_method",
    "rewrite_presets_on_startup": "rewrite_presets_on_startup",
    "firewall_check": "firewall_check",
}

# section name in config.ini / key in config.yaml, for the 3 preset blocks
PRESET_BLOCKS = (("presets", "presets"), ("labels", "preset_labels"), ("logos", "preset_logos"))


def base_dir() -> str:
    """Directory containing the executable (works both frozen and plain Python)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _coerce(key: str, value):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "oui")
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    return str(value).strip() if value is not None else ""


def _int_map(d) -> dict:
    out = {}
    for k, v in (d or {}).items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            continue
        if v is not None and str(v).strip():
            out[n] = str(v).strip()
    return out


def _normalize(raw: dict) -> dict:
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in ("presets", "preset_labels", "preset_logos"):
            cfg[key] = _int_map(raw.get(key))
        elif raw.get(key) not in (None, ""):
            cfg[key] = _coerce(key, raw[key])
    cfg["preset_write_method"] = cfg["preset_write_method"].lower()
    return cfg


def _load_ini(path: str) -> dict:
    import configparser
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(path, encoding="utf-8-sig")
    raw = {}
    if ini.has_section("bose"):
        bose = ini["bose"]
        raw["bose_host"] = bose.get("host", "")
        raw["bose_name"] = bose.get("name", "")
        raw["bose_mac"] = bose.get("mac", "")
    for section, key in PRESET_BLOCKS:
        if ini.has_section(section):
            raw[key] = dict(ini.items(section))
    if ini.has_section("bridge"):
        for ini_key, key in INI_BRIDGE_KEYS.items():
            if ini_key in ini["bridge"]:
                raw[key] = ini["bridge"][ini_key]
    return raw


def _load_yaml(path: str) -> dict:
    import yaml
    with open(path, encoding="utf-8-sig") as f:
        return yaml.safe_load(f) or {}


def find_config(path_arg: str = "config.yaml") -> tuple:
    """(path, kind) with kind 'ini' or 'yaml'."""
    ini_path = os.path.join(base_dir(), "config.ini")
    if os.path.exists(ini_path):
        return ini_path, "ini"
    path = path_arg if os.path.isabs(path_arg) else os.path.join(base_dir(), path_arg)
    return path, ("ini" if path.lower().endswith(".ini") else "yaml")


def load_config(path_arg: str = "config.yaml") -> tuple:
    """Returns (cfg, path, kind)."""
    path, kind = find_config(path_arg)
    raw = _load_ini(path) if kind == "ini" else _load_yaml(path)
    return _normalize(raw), path, kind


# ---------------------------------------------------------------------------
# Saving: rewrite only the presets / labels / logos blocks, keep the rest verbatim
# ---------------------------------------------------------------------------

def _clean(value: str) -> str:
    return " ".join(str(value).split())


def _rewrite_ini(text: str, blocks: dict) -> str:
    """blocks: section name -> {n: value}."""
    out, current, seen = [], None, set()
    for line in text.splitlines():
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            if current in blocks:
                out.append("")
            current = m.group(1).strip().lower()
            out.append(line)
            if current in blocks:
                seen.add(current)
                out.extend(f"{n} = {_clean(v)}" for n, v in sorted(blocks[current].items()))
            continue
        if current in blocks:
            continue  # old content of a rewritten section
        out.append(line)
    for name, values in blocks.items():
        if name not in seen:
            out += ["", f"[{name}]"] + [f"{n} = {_clean(v)}" for n, v in sorted(values.items())]
    return "\n".join(out).rstrip("\n") + "\n"


def _rewrite_yaml(text: str, blocks: dict) -> str:
    """blocks: top-level key -> {n: value}. Values are written as JSON strings,
    which are valid YAML double-quoted scalars."""
    def render(key):
        lines = [f"{key}:"]
        lines += [f"  {n}: {json.dumps(_clean(v), ensure_ascii=False)}"
                  for n, v in sorted(blocks[key].items())]
        return lines

    out, skipping, pending_blank, seen = [], False, [], set()
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
        if m:
            skipping = False
            out.extend(pending_blank)
            pending_blank = []
            if m.group(1) in blocks:
                seen.add(m.group(1))
                out.extend(render(m.group(1)))
                skipping = True
                continue
            out.append(line)
            continue
        if skipping:
            if not line.strip():
                pending_blank.append(line)
                continue
            if line[:1] in (" ", "\t"):
                pending_blank = []
                continue
            skipping = False
        out.extend(pending_blank)
        pending_blank = []
        out.append(line)
    out.extend(pending_blank)
    for key in blocks:
        if key not in seen:
            out += [""] + render(key)
    return "\n".join(out).rstrip("\n") + "\n"


def save_presets(path: str, kind: str, presets: dict, labels: dict, logos: dict) -> None:
    """Rewrite the presets / labels / logos blocks of the config file, atomically."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    all_presets = {n: presets.get(n, "") for n in range(1, 7)}
    if kind == "ini":
        new = _rewrite_ini(text, {"presets": all_presets, "labels": labels, "logos": logos})
    else:
        new = _rewrite_yaml(text, {"presets": all_presets, "preset_labels": labels,
                                   "preset_logos": logos})
    if text:
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(text)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, path)
