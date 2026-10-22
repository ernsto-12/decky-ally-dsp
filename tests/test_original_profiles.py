"""Synthetic XML/profile integrity tests; no vendor settings are test fixtures."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from allydsp import original_profiles as profiles
from allydsp.original.runtime_controls import encode_runtime


def xml_node(values):
    tree = {}
    for name, value in values.items():
        parts = name.split("/")
        branch = tree
        for part in parts[:-1]:
            branch = branch.setdefault(part, {})
        branch[parts[-1]] = value
    def emit(name, value):
        if isinstance(value, dict):
            return "<" + name + ">" + "".join(emit(key, item) for key, item in value.items()) + "</" + name + ">"
        if value is None:
            return "<" + name + "/>"
        text = ",".join(map(str, value)) if isinstance(value, list) else str(value)
        return '<' + name + ' value="' + text + '"/>'
    return "".join(emit(name, value) for name, value in tree.items())


@pytest.fixture
def official_sources(tmp_path, monkeypatch):
    frequencies = [50 + 100 * index for index in range(20)]
    targets = {"balanced": [0] * 20, "detailed": [1] * 20, "warm": [-1] * 20}
    cp = {name: 0 for name in sorted(profiles.CP_SCALARS)}
    cp.update({"output-mode/processing_mode": 1, "output-mode/nb_output_channels": 2,
               "output-mode/mix_matrix": [0] * 16, "graphic-equalizer-bands": [0] * 20,
               "ieq-bands-set": [0] * 20, "regulator-tuning/threshold_low": [0] * 20,
               "regulator-tuning/threshold_high": [0] * 20, "regulator-tuning/isolated_band": [0] * 20,
               "virtual-bass-src-freqs": [0, 0], "virtual-bass-mix-freqs": [0, 0],
               "virtual-bass-subgains": [0, 0, 0]})
    cp.update({"audio-optimizer-bands/ch_" + str(channel).zfill(2): [0] * 20 for channel in range(10)})
    cp.update({"init-info/" + key: 0 for key in profiles.INIT_KEYS})
    cp["init-info/max_num_objects"] = 0
    vlldp = {"peak-level": 7, "filter_coefficients/coefficient": ""}
    reference = {"schema": "original-vlldp160-game-profile-v2", "dll_sha256": profiles.POST_DLL_PIN,
                 "initialization_properties": [], "properties": [
                     {"name": "peak-level", "kind": "i4", "value": 7, "source_paths": ["peak-level"]},
                     {"name": "filter_coefficients", "kind": "blob_i32", "value": [0],
                      "source_paths": ["filter_coefficients/coefficient"]}],
                 "baseline_commit_properties": [], "unconsumed_vlldp_leaves": {}}
    reference_path = tmp_path / "game-post.json"
    reference_path.write_bytes(profiles._json_bytes(reference, crlf=True))
    monkeypatch.setattr(profiles, "GAME_POST_PIN", profiles.digest(reference_path.read_bytes()))
    fields = {key: value for key, value in cp.items() if key in profiles.CP_SCALARS | profiles.CP_VECTORS}
    fields["_band-frequencies"] = frequencies
    monkeypatch.setattr(profiles, "GAME_CP_PIN", profiles.digest(encode_runtime(fields)))
    def create(endpoint):
        constants = '<band_20_freq fs_48000="' + ",".join(map(str, frequencies)) + '"/>'
        constants += "".join('<ieq_' + name + ' target="' + ",".join(map(str, target)) + '"/>'
                             for name, target in targets.items())
        body = ''
        for preset in profiles.PRESETS:
            values = copy.deepcopy(cp)
            if preset == "movie":
                values["dialog-enhancer-enable"] = 1
            if endpoint == "headphone":
                values["advanced-speaker-virtualizer-rendering-config"] = [0] * 8
            body += '<profile type="' + preset + '"><tuning-cp>' + xml_node(values) + '</tuning-cp>'
            body += '<tuning-vlldp>' + xml_node(vlldp) + '</tuning-vlldp></profile>'
        return ('<?xml version="1.0"?><device_data xmlns:xsi="example"><security-key value="DO-NOT-EXPORT"/>'
                '<constant>' + constants + '</constant><endpoint type="' + endpoint + '">' + body
                + '</endpoint></device_data>').encode()
    speaker, headphone = tmp_path / profiles.SPEAKER_XML_NAME, tmp_path / profiles.HEADPHONE_XML_NAME
    speaker.write_bytes(create("internal_speaker"))
    headphone.write_bytes(create("headphone"))
    monkeypatch.setattr(profiles, "SPEAKER_XML_PIN", profiles.digest(speaker.read_bytes()))
    monkeypatch.setattr(profiles, "HEADPHONE_XML_PIN", profiles.digest(headphone.read_bytes()))
    return speaker, headphone, reference_path


def test_private_catalog_contains_full_route_profiles_and_no_protected_source(official_sources):
    result = profiles.build_profile_pack(*official_sources)
    catalog = result["catalog"]
    assert catalog["schema"] == profiles.SCHEMA
    assert set(catalog["routes"]) == {"speakers", "headphones"}
    assert len(result["payloads"]) == 16
    assert b"DO-NOT-EXPORT" not in result["payloads"]["catalog.json"]
    speaker = catalog["routes"]["speakers"]["profiles"]
    headphone = catalog["routes"]["headphones"]["profiles"]
    assert set(speaker) == set(profiles.PRESETS) == set(headphone)
    assert speaker["movie"]["cp"]["dialog-enhancer-enable"] == 1
    assert headphone["game"]["post_plan"] is None
    assert "advanced-speaker-virtualizer-rendering-config" in headphone["game"]["cp"]
    assert "init-info/max_num_objects" in speaker["game"]["unconsumed_cp"]
    for route in catalog["routes"].values():
        for profile in route["profiles"].values():
            for key in ("runtime_blob", "post_plan"):
                if profile[key] is not None:
                    reference = profile[key]
                    assert profiles.digest(result["payloads"][reference["path"]]) == reference["sha256"]
    game = speaker["game"]
    assert profiles.digest(result["payloads"][game["runtime_blob"]["path"]]) == profiles.GAME_CP_PIN
    assert result["payloads"][game["post_plan"]["path"]] == official_sources[2].read_bytes()


def test_modified_xml_fails_before_a_catalog_is_produced(official_sources):
    speaker, headphone, plan = official_sources
    speaker.write_bytes(speaker.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        profiles.build_profile_pack(speaker, headphone, plan)


@pytest.mark.parametrize("bad", [
    b'<!DOCTYPE device_data [<!ENTITY x SYSTEM "file:///x">]><device_data/>',
    b'<device_data><a></b></device_data>',
    b'<device_data><a value="1" value="2"/></device_data>',
    b'<device_data><a value="&unknown;"/></device_data>',
    b'<device_data/> trailing',
])
def test_restricted_xml_rejects_external_entities_and_malformed_documents(bad):
    with pytest.raises(ValueError):
        profiles._xml(bad)


def test_local_official_sources_need_no_download(official_sources, monkeypatch):
    from allydsp import asus_fetch
    monkeypatch.setattr(asus_fetch, "pinned_package", lambda *_: pytest.fail("unexpected package/network lookup"))
    speaker, headphone, _ = official_sources
    assert profiles.ensure_profile_xml_sources(speaker_xml=speaker) == (speaker, headphone)


def test_missing_cache_does_not_download_when_disabled(official_sources, monkeypatch, tmp_path):
    from allydsp import asus_fetch, paths
    speaker, headphone, _ = official_sources
    headphone.unlink()
    monkeypatch.setattr(paths, "TMP_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(asus_fetch, "pinned_package", lambda *_: {"sha256": profiles.PACKAGE_PIN, "url": "https://example.test/p.exe"})
    monkeypatch.setattr(asus_fetch, "download", lambda *_args, **_kw: pytest.fail("unexpected download"))
    with pytest.raises(ValueError, match="downloading is disabled"):
        profiles.ensure_profile_xml_sources(speaker_xml=speaker, runtime_dir=tmp_path / "runtime", allow_download=False)


def test_verified_cached_archive_extracts_only_reviewed_members(official_sources, monkeypatch, tmp_path):
    from allydsp import asus_fetch, paths, util
    speaker, headphone, _ = official_sources
    data = {profiles.SPEAKER_XML_NAME: speaker.read_bytes(), profiles.HEADPHONE_XML_NAME: headphone.read_bytes()}
    headphone.unlink()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive = downloads / "package.exe"
    archive.write_bytes(b"synthetic pinned archive")
    monkeypatch.setattr(profiles, "PACKAGE_PIN", profiles.digest(archive.read_bytes()))
    monkeypatch.setattr(profiles, "PACKAGE_URL", "https://example.test/package.exe")
    monkeypatch.setattr(paths, "TMP_DIR", str(downloads))
    monkeypatch.setattr(asus_fetch, "pinned_package", lambda *_: {"sha256": profiles.PACKAGE_PIN, "url": "https://example.test/package.exe"})
    monkeypatch.setattr(asus_fetch, "download", lambda *_args, **_kw: pytest.fail("unexpected download"))
    monkeypatch.setattr(asus_fetch, "find_7z_offset", lambda *_: 0)
    monkeypatch.setattr(asus_fetch, "slice_payload", lambda _src, dest, _offset: Path(dest).write_bytes(b"synthetic 7z"))
    entries = ["official/" + name for name in data] + ["unrelated.dll", "credentials.txt"]
    monkeypatch.setattr(asus_fetch, "list_7z", lambda *_: entries)
    called = []
    def extract(command, timeout):
        called.append(command)
        selected = command[5:]
        assert selected == entries[:2]
        output = Path(command[3][2:])
        for name, content in data.items():
            (output / name).write_bytes(content)
        return SimpleNamespace(ok=True)
    monkeypatch.setattr(util, "run", extract)
    result = profiles.ensure_profile_xml_sources(speaker_xml=speaker, runtime_dir=tmp_path / "runtime", allow_download=False)
    assert len(called) == 1
    assert result[0] == speaker
    assert result[1].read_bytes() == data[profiles.HEADPHONE_XML_NAME]
    assert profiles.ensure_profile_xml_sources(speaker_xml=speaker, runtime_dir=tmp_path / "runtime", allow_download=False) == result
    assert len(called) == 1


@pytest.mark.parametrize("legacy_matches", [True, False])
def test_legacy_archive_is_used_only_when_both_xml_members_match(official_sources, monkeypatch, tmp_path, legacy_matches):
    from allydsp import asus_fetch, paths, util
    speaker, headphone, _ = official_sources
    data = {profiles.SPEAKER_XML_NAME: speaker.read_bytes(), profiles.HEADPHONE_XML_NAME: headphone.read_bytes()}
    headphone.unlink()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive = downloads / profiles.LEGACY_PACKAGE_NAME
    archive.write_bytes(b"synthetic older verified archive")
    old_pin = profiles.digest(archive.read_bytes())
    new_archive = b"synthetic current verified archive"
    new_pin = profiles.digest(new_archive)
    monkeypatch.setattr(profiles, "LEGACY_PACKAGE_PIN", old_pin)
    monkeypatch.setattr(profiles, "PACKAGE_PIN", new_pin)
    monkeypatch.setattr(profiles, "PACKAGE_URL", "https://example.test/current-official.exe")
    monkeypatch.setattr(paths, "TMP_DIR", str(downloads))
    monkeypatch.setattr(asus_fetch, "find_7z_offset", lambda *_: 0)
    monkeypatch.setattr(asus_fetch, "slice_payload", lambda _src, dest, _offset: Path(dest).write_bytes(b"synthetic 7z"))
    monkeypatch.setattr(asus_fetch, "list_7z", lambda *_: ["official/" + name for name in data])
    downloaded = []
    def download(url, destination, pin, size, **_):
        assert url == profiles.PACKAGE_URL and pin == new_pin and size == profiles.PACKAGE_BYTES
        downloaded.append(url)
        Path(destination).write_bytes(new_archive)
    monkeypatch.setattr(asus_fetch, "download", download)
    def extract(command, timeout):
        output = Path(command[3][2:])
        for name, content in data.items():
            wrong_legacy = not legacy_matches and output.parent.name.startswith("cached-")
            (output / name).write_bytes(content + (b" changed" if wrong_legacy else b""))
        return SimpleNamespace(ok=True)
    monkeypatch.setattr(util, "run", extract)
    result = profiles.ensure_profile_xml_sources(speaker_xml=speaker, runtime_dir=tmp_path / "runtime")
    assert len(downloaded) == (0 if legacy_matches else 1)
    assert result[1].parent.name == (old_pin if legacy_matches else new_pin)
    assert result[1].read_bytes() == data[profiles.HEADPHONE_XML_NAME]


def test_fresh_download_uses_explicit_reviewed_archive_and_checks_members(official_sources, monkeypatch, tmp_path):
    from allydsp import asus_fetch, paths, util
    speaker, headphone, _ = official_sources
    data = {profiles.SPEAKER_XML_NAME: speaker.read_bytes(), profiles.HEADPHONE_XML_NAME: headphone.read_bytes()}
    # An automatically discovered XML from a different package is not treated
    # as the verified profile source; the pinned member is prepared instead.
    previous = tmp_path / "previous-device.xml"
    previous.write_bytes(b"different older tuning")
    monkeypatch.setattr(asus_fetch, "current_xml", lambda: str(previous))
    monkeypatch.setattr(asus_fetch, "pinned_package", lambda *_: pytest.fail("an unrelated registry entry must not replace the reviewed profile source"))
    monkeypatch.setattr(paths, "TMP_DIR", str(tmp_path / "downloads"))
    archive_bytes = b"synthetic fresh official archive"
    monkeypatch.setattr(profiles, "PACKAGE_PIN", profiles.digest(archive_bytes))
    downloaded = []
    def download(url, destination, pin, size, **_):
        assert url == profiles.PACKAGE_URL and pin == profiles.PACKAGE_PIN
        assert size == profiles.PACKAGE_BYTES
        downloaded.append(url)
        Path(destination).write_bytes(archive_bytes)
    monkeypatch.setattr(asus_fetch, "download", download)
    monkeypatch.setattr(asus_fetch, "find_7z_offset", lambda *_: 0)
    monkeypatch.setattr(asus_fetch, "slice_payload", lambda _src, dest, _offset: Path(dest).write_bytes(b"synthetic 7z"))
    monkeypatch.setattr(asus_fetch, "list_7z", lambda *_: ["official/" + name for name in data])
    def extract(command, timeout):
        output = Path(command[3][2:])
        for name, content in data.items():
            (output / name).write_bytes(content)
        return SimpleNamespace(ok=True)
    monkeypatch.setattr(util, "run", extract)
    prepared = profiles.ensure_profile_xml_sources(runtime_dir=tmp_path / "runtime")
    assert len(downloaded) == 1
    assert prepared[0].read_bytes() == data[profiles.SPEAKER_XML_NAME]
    assert prepared[1].read_bytes() == data[profiles.HEADPHONE_XML_NAME]
    assert prepared[0].parent == prepared[1].parent
