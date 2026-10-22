"""Build private original-engine profiles from two pinned official XML files.

The public module contains format mappings only. OEM values, coefficients and
protected XML fields are never copied into source code or bundled releases.
The small XML reader avoids dependencies absent from Decky's Python runtime.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
from typing import Any, Dict
from urllib.parse import urlsplit

SCHEMA = "ally-dsp-original-profiles-v1"
PRESETS = ("game", "dynamic", "movie", "music", "voice")
VOICINGS = ("balanced", "detailed", "warm")
ROUTES = {"speakers": "internal_speaker", "headphones": "headphone"}
SPEAKER_XML_PIN = "3b422b4c36ed73d7023072ccde9fc288b556d77013788bee30910f54de3bb7b6"
HEADPHONE_XML_PIN = "4e43f062ba431545919f84fbd03346eec7bd10b74247cd37b4552e9502596d60"
GAME_CP_PIN = "88e29379fe4007d623daee006eb9b7e6786db7fb79e49401dfef989b648cfb1a"
GAME_POST_PIN = "f36a04973a6e373b8ba0cb734c14ac6d2d0d401ffe4347be448091940e3348af"
POST_DLL_PIN = "28990e8d5845b012020b52947ceab0cf37199ede1e8162cd7d7a03db580af82c"
PACKAGE_URL = "https://dlcdnets.asus.com/pub/ASUS/IOTHMD/Image/Software/SoftwareandUtility/17507/ASUS_Z_V11.130.1340.46_17507_2.exe"
PACKAGE_PIN = "b649614c0da2bea51096e47d503d753928446f8b275de55647772f29d19c2fd7"
PACKAGE_BYTES = 10827184
# An older converted-engine archive may still be cached. Its version string
# alone is not trusted: both extracted XML members must match the source pins.
LEGACY_PACKAGE_PIN = "f68bb32658ddbf6970a7ab338c22e94dcff46b69ef0709249748013326bb4778"
LEGACY_PACKAGE_NAME = "DolbyAtmosdriverforConsumer_ASUS_Z_V11.130.1340.46_17507.exe"
SPEAKER_XML_NAME = "DEV_0294_SUBSYS_10431384_PCI_SUBSYS_13841043.xml"
HEADPHONE_XML_NAME = "Headphone_Default_Generic_Default_Atmos3.10.xml"
MAX_BYTES = 4 * 1024 * 1024

# The original ASAR consumer has named implementations for these fields.
# PO's official false input is retained for exact Game serialization; it has no
# supported effect in this DLL. Its channel banks are deliberately excluded.
CP_SCALARS = frozenset((
    "audio-optimizer-enable", "mi-virt-steering-enable", "virtual-bass-mode",
    "virtual-bass-overall-gain", "virtual-bass-slope-gain", "bass-enhancer-enable",
    "bass-enhancer-boost", "bass-enhancer-cutoff-frequency", "bass-enhancer-width",
    "bass-extraction-enable", "bass-extraction-cutoff-frequency", "calibration-boost",
    "dialog-enhancer-enable", "dialog-enhancer-amount", "dialog-enhancer-ducking",
    "graphic-equalizer-enable", "height-filter-mode", "ieq-enable", "ieq-amount",
    "mi-dialog-enhancer-steering-enable", "mi-dv-leveler-steering-enable",
    "mi-ieq-steering-enable", "mi-surround-compressor-steering-enable", "postgain",
    "pregain", "process-optimizer-enable", "regulator-enable", "regulator-overdrive",
    "regulator-relaxation-amount", "regulator-speaker-dist-enable",
    "regulator-timbre-preservation", "surround-boost", "surround-decoder-enable",
    "system-gain", "virtualizer-front-speaker-angle", "virtualizer-height-speaker-angle",
    "virtualizer-surround-speaker-angle", "volmax-boost", "volume-leveler-enable",
    "volume-leveler-drc-enable", "volume-leveler-amount", "volume-leveler-in-target",
    "volume-leveler-out-target", "volume-modeler-enable", "volume-modeler-calibration",
    "output-mode/processing_mode", "output-mode/nb_output_channels",
))
CP_VECTORS = frozenset((
    "output-mode/mix_matrix", "graphic-equalizer-bands", "ieq-bands-set",
    "regulator-tuning/threshold_low", "regulator-tuning/threshold_high",
    "regulator-tuning/isolated_band", "virtual-bass-src-freqs", "virtual-bass-mix-freqs",
    "virtual-bass-subgains", "advanced-speaker-virtualizer-rendering-config",
    *("audio-optimizer-bands/ch_" + str(channel).zfill(2) for channel in range(10)),
))
INIT_KEYS = ("mi_process_disable", "virtual_bass_process_enable", "mode")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path, pin):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_BYTES:
        raise ValueError("Expected a bounded regular official profile source")
    data = path.read_bytes()
    if len(data) > MAX_BYTES or digest(data) != pin:
        raise ValueError("Official profile source SHA256 differs: " + path.name)
    return data


def _integers(value):
    if value == "":
        return ""
    parts = value.split(",")
    if any(not re.fullmatch(r"-?\d+", part) for part in parts):
        raise ValueError("Unexpected noninteger tuning value")
    result = [int(part) for part in parts]
    if any(not -2**31 <= item < 2**31 for item in result):
        raise ValueError("Tuning value is outside int32")
    return result if len(result) > 1 else result[0]


def _xml(data):
    """Parse the pinned, element/attribute-only vendor document; no entities."""
    text = data.decode("utf-8-sig")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    if "<!" in text or "<?" in text or re.search(r"&(?!amp;|lt;|gt;|quot;|apos;)", text):
        raise ValueError("Unsupported XML declaration or entity")
    stack, roots, previous, count = [], [], 0, 0
    for match in re.finditer(r"<(/?)([\w.-]+)([^<>]*)>", text):
        if text[previous:match.start()].strip():
            raise ValueError("Unexpected tuning XML text")
        previous = match.end()
        closing, tag, tail = match.groups()
        if closing:
            if tail.strip() or not stack or stack[-1]["tag"] != tag:
                raise ValueError("Mismatched tuning XML element")
            stack.pop()
            continue
        empty = tail.rstrip().endswith("/")
        if empty:
            tail = tail.rstrip()[:-1]
        attrs, position = {}, 0
        for attr in re.finditer(r'([\w.:-]+)\s*=\s*"([^"<>]*)"', tail):
            if tail[position:attr.start()].strip() or attr[1] in attrs:
                raise ValueError("Invalid or duplicate tuning XML attribute")
            attrs[attr[1]] = re.sub(r"&(amp|lt|gt|quot|apos);",
                                    lambda entity: {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}[entity[1]],
                                    attr[2])
            position = attr.end()
        if tail[position:].strip():
            raise ValueError("Unexpected tuning XML attribute encoding")
        node = {"tag": tag, "attrs": attrs, "children": []}
        (stack[-1]["children"] if stack else roots).append(node)
        count += 1
        if count > 20000 or len(stack) > 24:
            raise ValueError("Tuning XML exceeds structural limits")
        if not empty:
            stack.append(node)
    if text[previous:].strip() or stack or len(roots) != 1 or roots[0]["tag"] != "device_data":
        raise ValueError("Unexpected or incomplete tuning XML root")
    return roots[0]


def _child(node, name):
    found = [child for child in node["children"] if child["tag"] == name]
    if len(found) != 1:
        raise ValueError("Expected one official tuning element: " + name)
    return found[0]


def _constants(root):
    constant = _child(root, "constant")
    selected = {child["tag"]: child["attrs"] for child in constant["children"]
                if child["tag"] in ("band_20_freq", "array_20_zero", "array_20_n192",
                                     *("ieq_" + name for name in VOICINGS))}
    frequencies = _integers(selected["band_20_freq"]["fs_48000"])
    if (not isinstance(frequencies, list) or len(frequencies) != 20
            or frequencies != sorted(set(frequencies)) or not all(20 <= value <= 20000 for value in frequencies)):
        raise ValueError("Unexpected official 48 kHz frequency table")
    targets = {name: _integers(selected["ieq_" + name]["target"]) for name in VOICINGS}
    if any(not isinstance(values, list) or len(values) != 20 for values in targets.values()):
        raise ValueError("Unexpected official voicing target shape")
    return frequencies, targets, selected


def _leaves(node, constants, prefix=""):
    result = {}
    def walk(item, path):
        children = item["children"]
        if children:
            for child in children:
                walk(child, path + "/" + child["tag"])
            return
        if path in result:
            raise ValueError("Duplicate official tuning leaf")
        attrs = item["attrs"]
        if "value" in attrs and len(attrs) == 1:
            result[path] = _integers(attrs["value"])
        elif "preset" in attrs and len(attrs) == 1:
            target = attrs["preset"]
            if target not in constants or "target" not in constants[target]:
                raise ValueError("Unsupported tuning preset reference")
            result[path] = _integers(constants[target]["target"])
        elif not attrs:
            result[path] = None
        else:
            raise ValueError("Unsupported tuning leaf encoding")
    for child in node["children"]:
        walk(child, prefix + child["tag"])
    return result


def _profiles(root, endpoint):
    endpoints = [node for node in root["children"] if node["tag"] == "endpoint"
                 and node["attrs"].get("type") == endpoint]
    if len(endpoints) != 1:
        raise ValueError("Expected one official endpoint")
    profiles = {}
    for node in endpoints[0]["children"]:
        name = node["attrs"].get("type")
        if node["tag"] == "profile" and name in PRESETS:
            if name in profiles:
                raise ValueError("Duplicate official profile")
            profiles[name] = node
    if set(profiles) != set(PRESETS):
        raise ValueError("Official tuning is missing a required profile")
    return profiles


def _cp_fields(values, frequencies):
    missing = CP_SCALARS - values.keys()
    if missing:
        raise ValueError("Official profile is missing supported CP fields")
    fields = {name: copy.deepcopy(value) for name, value in values.items()
              if name in CP_SCALARS | CP_VECTORS}
    if any(type(fields[name]) is not int for name in CP_SCALARS):
        raise ValueError("Unexpected official CP scalar type")
    fields["_band-frequencies"] = list(frequencies)
    if fields["process-optimizer-enable"] != 0:
        raise ValueError("This processing DLL does not support an enabled Process Optimizer")
    return fields


def _post_plan(reference, values):
    """Reapply the reviewed property mapping with each profile's source values."""
    plan = copy.deepcopy(reference)
    for section in ("initialization_properties", "properties", "baseline_commit_properties"):
        properties = plan[section]
        if not isinstance(properties, list) or len(properties) > 64:
            raise ValueError("Unexpected reviewed speaker post plan")
        for prop in properties:
            source = [values[path] for path in prop["source_paths"]]
            name, kind = prop["name"], prop["kind"]
            if kind == "i4":
                if len(source) != 1 or type(source[0]) is not int:
                    raise ValueError("Unexpected speaker post scalar")
                prop["value"] = source[0]
            elif kind == "blob_i32":
                if name == "filter_coefficients":
                    if source != [""]:
                        raise ValueError("Unreviewed nonempty speaker filter coefficients")
                    prop["value"] = [0]
                elif name == "audio-optimizer-bands":
                    flat = [item for channel in source for item in channel]
                    prop["value"] = [len(flat), *flat]
                elif name == "multi-regulator-threshold":
                    channels = len(next(p for p in plan["properties"] if p["name"] == "audio-optimizer-bands")["source_paths"])
                    prop["value"] = [item for band in source for item in [len(band) * channels, *(band * channels)]]
                elif name == "mb-compressor-tuning":
                    groups = source[0]
                    if type(groups) is not int or groups != len(source) - 1:
                        raise ValueError("Unsupported active speaker compressor group count")
                    prop["value"] = [groups, *[item for group in source[1:] for item in [len(group), *group]]]
                elif name in ("regulator-threshold-legacy", "regulator-isolated-band-legacy",
                              "multi-regulator-isolated-band", "regulator-stress-amount", "sliding-bass-gain-curves"):
                    prop["value"] = [item for band in source for item in [len(band), *band]]
                else:
                    raise ValueError("Unreviewed speaker post vector mapping")
            else:
                raise ValueError("Unreviewed speaker post property type")
    plan["unconsumed_vlldp_leaves"] = {path: values[path] for path in reference["unconsumed_vlldp_leaves"]}
    return plan


def _json_bytes(value, *, crlf=False):
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")


def build_profile_pack(speaker_xml, headphone_xml, game_post_plan) -> Dict[str, Any]:
    """Return private catalog and generation-relative payloads; no writes or I/O to a device."""
    from .original.runtime_controls import encode_runtime
    speaker_data = _read(speaker_xml, SPEAKER_XML_PIN)
    headphone_data = _read(headphone_xml, HEADPHONE_XML_PIN)
    post_data = _read(game_post_plan, GAME_POST_PIN)
    reference = json.loads(post_data.decode("utf-8-sig"))
    if reference.get("dll_sha256") != POST_DLL_PIN:
        raise ValueError("Reviewed speaker post plan targets another DLL")
    catalog = {"schema": SCHEMA, "sources": {"speakers": SPEAKER_XML_PIN, "headphones": HEADPHONE_XML_PIN},
               "presets": list(PRESETS), "voicings": list(VOICINGS), "routes": {},
               "scope": "Original supported channel-bed processing; Windows endpoint calibration/order and dynamic Atmos metadata remain unverified."}
    payloads = {}
    for route, data in (("speakers", speaker_data), ("headphones", headphone_data)):
        root = _xml(data)
        frequencies, targets, constants = _constants(root)
        records = {}
        for preset, node in _profiles(root, ROUTES[route]).items():
            values = _leaves(_child(node, "tuning-cp"), constants)
            fields = _cp_fields(values, frequencies)
            initial = {name: values["init-info/" + name] for name in INIT_KEYS}
            if any(type(value) is not int for value in initial.values()):
                raise ValueError("Unexpected official initialization value")
            blob = encode_runtime(fields)
            relative = route + "/" + preset + "-cp.bin"
            payloads[relative] = blob
            record = {"cp": fields, "init": initial, "voicings": copy.deepcopy(targets),
                      "runtime_blob": {"path": relative, "sha256": digest(blob)}, "post_plan": None,
                      "unconsumed_cp": sorted(set(values) - (CP_SCALARS | CP_VECTORS)
                                              - {"init-info/" + key for key in INIT_KEYS})}
            if route == "speakers":
                vlldp = _leaves(_child(node, "tuning-vlldp"), constants)
                plan = _post_plan(reference, vlldp)
                encoded = _json_bytes(plan, crlf=True)
                if preset == "game" and encoded != post_data:
                    raise ValueError("Reconstructed Game speaker post plan differs from the verified reference")
                plan_path = route + "/" + preset + "-vlldp-plan.json"
                payloads[plan_path] = encoded
                record["post_plan"] = {"path": plan_path, "sha256": digest(encoded)}
                if preset == "game" and digest(blob) != GAME_CP_PIN:
                    raise ValueError("Reconstructed speaker Game CP differs from the verified reference")
            records[preset] = record
        # Runtime profile switches must not silently change original allocation
        # or initialization. All five pinned official profiles share this init.
        if any(record["init"] != records["game"]["init"] for record in records.values()):
            raise ValueError("Official profiles need different original engine initialization")
        catalog["routes"][route] = {"profiles": records}
    payloads["catalog.json"] = _json_bytes(catalog)
    return {"catalog": catalog, "payloads": payloads}


def ensure_profile_xml_sources(*, speaker_xml=None, headphone_xml=None,
                               runtime_dir=None, allow_download=True):
    """Reuse local official XMLs or extract only these members from pinned ASUS.

    The downloaded archive is verified before extraction and never executed.
    Store DLL acquisition is separate; this helper downloads no Dolby Access
    application, changes no engine manifest and starts no audio service.
    """
    from . import asus_fetch, paths
    from .util import run, sha256_file
    explicit_speaker, explicit_headphone = speaker_xml is not None, headphone_xml is not None
    if speaker_xml is None:
        speaker_xml = asus_fetch.current_xml()
    if speaker_xml is not None:
        try:
            _read(speaker_xml, SPEAKER_XML_PIN)
        except (ValueError, OSError):
            if explicit_speaker:
                raise
            speaker_xml = None
    if headphone_xml is None and speaker_xml is not None:
        adjacent = Path(speaker_xml).parent / HEADPHONE_XML_NAME
        if adjacent.exists():
            headphone_xml = adjacent
    if headphone_xml is not None:
        try:
            _read(headphone_xml, HEADPHONE_XML_PIN)
        except (ValueError, OSError):
            if explicit_headphone:
                raise
            headphone_xml = None
    if speaker_xml is not None and headphone_xml is not None:
        return Path(speaker_xml), Path(headphone_xml)
    cache_root = Path(runtime_dir or paths.RUNTIME_DIR) / "original/profile-sources"
    if any(candidate.is_symlink() for candidate in (cache_root, *cache_root.parents)):
        raise ValueError("Official profile source cache contains a symbolic link")
    archive_names = ((PACKAGE_PIN, Path(urlsplit(PACKAGE_URL).path).name),
                     (LEGACY_PACKAGE_PIN, LEGACY_PACKAGE_NAME))
    for package_pin, _name in archive_names:
        cache = cache_root / package_pin
        if cache.exists():
            if cache.is_symlink() or not cache.is_dir():
                raise ValueError("Official profile source cache is not a regular directory")
            cached_speaker, cached_headphone = cache / SPEAKER_XML_NAME, cache / HEADPHONE_XML_NAME
            _read(cached_speaker, SPEAKER_XML_PIN)
            _read(cached_headphone, HEADPHONE_XML_PIN)
            return Path(speaker_xml) if speaker_xml else cached_speaker, Path(headphone_xml) if headphone_xml else cached_headphone
    archives = []
    for package_pin, name in archive_names:
        candidate = Path(paths.TMP_DIR) / name
        if (not candidate.is_symlink() and candidate.is_file()
                and sha256_file(str(candidate)) == package_pin):
            archives.append((package_pin, candidate))
    if not archives and not allow_download:
        raise ValueError("The pinned ASUS archive is not cached and downloading is disabled")
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".profile-source-", dir=cache_root) as temporary:
        work = Path(temporary)
        def extract(archive, package_pin, destination):
            if sha256_file(str(archive)) != package_pin:
                raise ValueError("Pinned ASUS archive digest differs before extraction")
            destination.mkdir(mode=0o700)
            offset = asus_fetch.find_7z_offset(str(archive))
            if offset is None:
                raise ValueError("The pinned ASUS archive has no supported payload")
            payload = destination / "payload.7z"
            asus_fetch.slice_payload(str(archive), str(payload), offset)
            entries = asus_fetch.list_7z(str(payload))
            selections = []
            for name in (SPEAKER_XML_NAME, HEADPHONE_XML_NAME):
                matches = [entry for entry in entries if entry.replace("\\", "/").split("/")[-1] == name]
                if len(matches) != 1:
                    raise ValueError("Expected one reviewed profile XML member in ASUS archive")
                selections.append(matches[0])
            extracted = destination / "xml"
            extracted.mkdir(mode=0o700)
            result = run(["7z", "e", "-y", "-o" + str(extracted), str(payload), *selections], timeout=120)
            if not result.ok:
                raise ValueError("Could not extract the reviewed XML members from ASUS archive")
            return (_read(extracted / SPEAKER_XML_NAME, SPEAKER_XML_PIN),
                    _read(extracted / HEADPHONE_XML_NAME, HEADPHONE_XML_PIN))
        selected = None
        for index, (archive_pin, archive) in enumerate(archives):
            try:
                selected = (archive_pin, extract(archive, archive_pin, work / ("cached-" + str(index))))
                break
            except (ValueError, OSError):
                # Old cached archives are usable only if their actual members
                # match. A matching version label is not a member-level proof.
                continue
        if selected is None:
            if not allow_download:
                raise ValueError("Cached ASUS archives do not contain the reviewed profile XML members")
            archive = work / "official-package.exe"
            asus_fetch.download(PACKAGE_URL, str(archive), PACKAGE_PIN, PACKAGE_BYTES, timeout=300)
            selected = (PACKAGE_PIN, extract(archive, PACKAGE_PIN, work / "downloaded"))
        archive_pin, (speaker_data, headphone_data) = selected
        cache = cache_root / archive_pin
        completed = work / "completed"
        completed.mkdir(mode=0o700)
        for name, data in ((SPEAKER_XML_NAME, speaker_data), (HEADPHONE_XML_NAME, headphone_data)):
            descriptor = os.open(completed / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        if cache.exists():
            # A concurrent preparation is acceptable only if its exact pinned
            # files match. Never replace or recursively remove an existing cache.
            _read(cache / SPEAKER_XML_NAME, SPEAKER_XML_PIN)
            _read(cache / HEADPHONE_XML_NAME, HEADPHONE_XML_PIN)
        else:
            os.replace(completed, cache)
    return (Path(speaker_xml) if speaker_xml else cache / SPEAKER_XML_NAME,
            Path(headphone_xml) if headphone_xml else cache / HEADPHONE_XML_NAME)
