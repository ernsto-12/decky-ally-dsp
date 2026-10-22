"""Settings persistence and preset resolution."""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Dict, Optional

from . import paths
from .constants import DEFAULT_PROFILE, DEFAULT_VOICING, PREGAIN_MAX_DB, PREGAIN_MIN_DB, PROFILE_IDS, VOICINGS
from .util import read_json, write_json

DEFAULTS: Dict[str, Any] = {
    "schema": 1,
    "enabled": True,
    "engine": {"kind": "converted"},
    "original": {"outputGainDb": 0.0, "controls": {}},
    "global": {"profile": DEFAULT_PROFILE, "voicing": DEFAULT_VOICING},
    "perApp": {},
    "extras": {"autogain": True, "dialog": True, "regulator": True, "virtualBass": False, "preGainDb": 0.0},
    "update": {"channel": "stable", "lastCheck": 0, "lastAttempt": 0, "latest": None, "autoCheck": True},
    "setup": {"done": False, "xmlSha256": None, "packageVersion": None, "converterVersion": None,
              "completedAt": None, "extrasSignature": None, "targetSink": None},
}
RECONVERT_KEYS = ("autogain", "dialog", "regulator", "virtualBass")

# setup_flow writes the "setup" section from a worker thread while the backend saves the rest.
_lock = threading.Lock()


def _merge(defaults: Any, data: Any) -> Any:
    if isinstance(defaults, dict):
        out = {}
        data = data if isinstance(data, dict) else {}
        for k, v in defaults.items():
            out[k] = _merge(v, data.get(k)) if k in data else json.loads(json.dumps(v))
        for k, v in data.items():
            if k not in out:
                out[k] = v
        return out
    return json.loads(json.dumps(defaults)) if data is None else data


def load() -> Dict[str, Any]:
    return _merge(DEFAULTS, read_json(paths.SETTINGS_FILE, {}) or {})


def save(s: Dict[str, Any]) -> None:
    write_json(paths.SETTINGS_FILE, s)


def update_section(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `values` into one section of the file on disk; returns the whole file."""
    with _lock:
        s = load()
        s[section].update(values)
        save(s)
        return s


def save_keeping(s: Dict[str, Any], section: str) -> None:
    """Save `s` but keep `section` as it is on disk (copied into `s`); update_section writes it."""
    with _lock:
        disk = read_json(paths.SETTINGS_FILE)
        if isinstance(disk, dict) and isinstance(disk.get(section), dict):
            s[section] = _merge(DEFAULTS[section], disk[section])
        save(s)


def valid_profile(p: Any) -> bool:
    return isinstance(p, str) and p in PROFILE_IDS


def valid_voicing(v: Any) -> bool:
    return isinstance(v, str) and v in VOICINGS


def clamp_pregain(db: Any) -> float:
    try:
        v = float(db)
    except (TypeError, ValueError):
        return 0.0
    return max(PREGAIN_MIN_DB, min(PREGAIN_MAX_DB, v))


def extras_signature(extras: Dict[str, Any]) -> str:
    sig = {k: bool(extras.get(k, DEFAULTS["extras"][k])) for k in RECONVERT_KEYS}
    return hashlib.sha1(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:12]


def resolve(s: Dict[str, Any], app_id: Optional[str]) -> Dict[str, Any]:
    if app_id:
        entry = (s.get("perApp") or {}).get(str(app_id))
        if entry and entry.get("enabled", True) and valid_profile(entry.get("profile")) and valid_voicing(entry.get("voicing")):
            return {"profile": entry["profile"], "voicing": entry["voicing"], "source": "app", "appId": str(app_id)}
    g = s.get("global") or {}
    profile = g.get("profile") if valid_profile(g.get("profile")) else DEFAULT_PROFILE
    voicing = g.get("voicing") if valid_voicing(g.get("voicing")) else DEFAULT_VOICING
    return {"profile": profile, "voicing": voicing, "source": "global", "appId": str(app_id) if app_id else None}
