import json
import os

import pytest

from allydsp import asus_fetch, devices, hardware, setup_flow

XBOX_API = {"Result": {"Obj": [{"Name": "Audio", "Files": [
    {"Title": "Dolby Atmos driver", "Version": "V10.1226.518.54", "ReleaseDate": "2026/01/16",
     "DownloadUrl": {"Global": "/pub/ASUS/old.exe"}, "sha256": "AB" * 32, "FileSize": "10.33 MB"},
    {"Title": "Dolby Atmos driver", "Version": "V11.130.1340.46", "ReleaseDate": "2026/02/25",
     "DownloadUrl": {"Global": "/pub/ASUS/new.exe"}, "sha256": "CD" * 32, "FileSize": "10.33 MB"},
]}]}}
RC72LA_API = {"Result": {"Obj": [{"Name": "Audio", "Files": [
    {"Title": "Dolby Atmos driver", "Version": "V9.719.332.11.7", "ReleaseDate": "2024/08/12",
     "DownloadUrl": {"Global": "/pub/ASUS/16088.exe"}, "sha256": "11" * 32, "FileSize": "8.38 MB"},
    {"Title": "Dolby Atmos driver", "Version": "V9.816.706.24", "ReleaseDate": "2024/09/13",
     "DownloadUrl": {"Global": "/pub/ASUS/16152.exe"}, "sha256": "9B0481DE" + "22" * 28, "FileSize": "8.4 MB"},
    {"Title": "Realtek Audio Driver", "Version": "V6", "ReleaseDate": "2026/03/01", "DownloadUrl": {"Global": "/x.exe"}},
]}]}}


def _registry(tmp_path, monkeypatch, data):
    p = tmp_path / "fallback-sources.json"
    p.write_text(json.dumps(data))
    monkeypatch.setattr(devices.paths, "FALLBACK_SOURCES", str(p))
    return str(p)


def _pkg(tag):
    return {"title": "Dolby Atmos driver", "version": f"V{tag}", "release_date": "2025/01/01",
            "url": f"https://cdn.example/{tag}.exe", "sha256": tag * 32, "size": 1000}


SAMPLE = {"asus_cdn": "https://cdn.example", "devices": {
    "10431384": {"name": "Xbox X", "asus_api": "https://api.example/xbox", "package": _pkg("a")},
    "10431EB3": {"name": "Ally X", "_comment": "ignored", "asus_api": "https://api.example/rc72la", "package": _pkg("b")},
}}


def test_shipped_registry_has_all_devices():
    reg = devices.load_registry()["devices"]
    assert set(reg) == {"10431384", "10431394", "10431eb3"}
    for ssid, dev in reg.items():
        assert ssid == ssid.lower()
        assert dev["name"] and dev["asus_api"].startswith("https://www.asus.com/support/webapi/ProductV2/GetPDDrivers?")
        pkg = dev["package"]
        assert pkg["url"].startswith("https://dlcdnets.asus.com/") and pkg["url"].endswith(".exe")
        assert len(pkg["sha256"]) == 64 and pkg["sha256"] == pkg["sha256"].lower()
        assert isinstance(pkg["size"], int) and pkg["size"] > 0
    assert reg["10431eb3"]["name"] == "ROG Ally X (RC72LA)"
    assert "cpu=RC72LA" in reg["10431eb3"]["asus_api"]
    assert reg["10431eb3"]["package"]["version"] == "V9.816.706.24"
    assert reg["10431eb3"]["package"]["sha256"] == "9b0481de5cc3a474fb941031948f26e44cb4efad80a3e040290748bd7f9e87c2"
    assert reg["10431eb3"]["package"]["size"] == 8807856
    assert reg["10431384"]["package"]["version"] == "V11.130.1340.46"
    from allydsp import original_profiles
    assert reg["10431384"]["package"]["url"] == original_profiles.PACKAGE_URL
    assert reg["10431384"]["package"]["sha256"] == original_profiles.PACKAGE_PIN
    assert reg["10431384"]["package"]["size"] == original_profiles.PACKAGE_BYTES
    assert reg["10431394"]["package"] == reg["10431384"]["package"]


def test_lookup_is_case_insensitive(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, SAMPLE)
    assert devices.supported_ssids() == ["10431384", "10431eb3"]
    for key in ("10431eb3", "10431EB3", " 10431Eb3 "):
        dev = devices.lookup(key)
        assert dev["name"] == "Ally X" and dev["ssid"] == "10431eb3"
    assert "_comment" not in devices.lookup("10431eb3")
    assert devices.display_name("10431384") == "Xbox X"
    assert devices.lookup("deadbeef") is None and devices.display_name("deadbeef") is None
    assert devices.normalize_ssid(None) == ""


def test_legacy_file_without_per_device_sources(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"asus_api": "https://api.example/legacy", "package": _pkg("c"),
                                      "devices": {"10431384": {"name": "Xbox X"},
                                                  "10431394": {"name": "Xbox", "asus_api": "https://api.example/own"}}})
    reg = devices.registry()
    assert reg["10431384"]["asus_api"] == "https://api.example/legacy" and reg["10431384"]["package"]["version"] == "Vc"
    assert reg["10431394"]["asus_api"] == "https://api.example/own" and reg["10431394"]["package"]["version"] == "Vc"


def test_empty_or_missing_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(devices.paths, "FALLBACK_SOURCES", str(tmp_path / "missing.json"))
    assert devices.registry() == {} and devices.supported_ssids() == []
    _registry(tmp_path, monkeypatch, {"devices": {"": {"name": "x"}, "10431384": "bad"}})
    assert devices.registry() == {}


def test_codec_info_normalises_ssid(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, SAMPLE)
    card = tmp_path / "card0"
    card.mkdir()
    (card / "id").write_text("Generic\n")
    (card / "codec#0").write_text("Codec: Realtek ALC294\nAddress: 0\nVendor Id: 0x10ec0294\nSubsystem Id: 0x10431EB3\n")
    monkeypatch.setattr(hardware, "_codec_files", lambda: [str(card / "codec#0")])
    info = hardware.codec_info()
    assert info["ssid"] == "10431eb3" and info["dev"] == "0294" and info["card"] == 0
    assert info["supported"] is True and info["model"] == "Ally X"
    (card / "codec#0").write_text("Codec: Realtek ALC294\nAddress: 0\nVendor Id: 0x10ec0294\nSubsystem Id: 0x10431ABC\n")
    info = hardware.codec_info()
    assert info["supported"] is False and info["model"] is None


def test_resolve_queries_the_device_api(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, SAMPLE)
    calls = []

    def fake_curl(url, timeout=30):
        calls.append(url)
        return {"https://api.example/xbox": XBOX_API, "https://api.example/rc72la": RC72LA_API}[url]

    monkeypatch.setattr(asus_fetch, "_curl_json", fake_curl)
    pick = asus_fetch.resolve_package("10431EB3")
    assert calls == ["https://api.example/rc72la"]
    assert pick["source"] == "asus-api" and pick["version"] == "V9.816.706.24"
    assert pick["url"] == "https://cdn.example/pub/ASUS/16152.exe"
    assert pick["sha256"] == "9b0481de" + "22" * 28
    pick = asus_fetch.resolve_package("10431384")
    assert calls[-1] == "https://api.example/xbox" and pick["version"] == "V11.130.1340.46"
    with pytest.raises(RuntimeError, match="deadbeef"):
        asus_fetch.resolve_package("deadbeef")


def test_resolve_falls_back_to_the_device_pin(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, SAMPLE)
    monkeypatch.setattr(asus_fetch, "_curl_json", lambda url, timeout=30: None)
    pick = asus_fetch.resolve_package("10431eb3")
    assert pick["source"] == "fallback" and pick["version"] == "Vb" and pick["sha256"] == "b" * 32
    assert asus_fetch.resolve_package("10431384")["version"] == "Va"

    monkeypatch.setattr(asus_fetch, "_curl_json", lambda url, timeout=30: {"Result": {"Obj": []}})
    assert asus_fetch.resolve_package("10431eb3")["source"] == "fallback"

    def boom(url, timeout=30):
        raise AssertionError("network must not be used")

    monkeypatch.setattr(asus_fetch, "_curl_json", boom)
    assert asus_fetch.resolve_package("10431eb3", use_network=False)["version"] == "Vb"
    assert asus_fetch.pinned_package("10431EB3")["source"] == "fallback"

    _registry(tmp_path, monkeypatch, {"devices": {"10431eb3": {"name": "no pin", "asus_api": ""}}})
    assert asus_fetch.pinned_package("10431eb3") is None
    with pytest.raises(RuntimeError, match="no fallback pinned"):
        asus_fetch.resolve_package("10431eb3")


def test_all_pinned_packages_dedupes(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"devices": {
        "10431384": {"name": "a", "package": _pkg("a")}, "10431394": {"name": "a2", "package": _pkg("a")},
        "10431eb3": {"name": "b", "package": _pkg("b")}, "ffffffff": {"name": "none"}}})
    assert [p["version"] for p in asus_fetch.all_pinned_packages()] == ["Va", "Vb"]


def test_package_candidates_adds_pin_after_api_pick():
    api = {"source": "asus-api", "sha256": "a" * 64}
    pin = {"source": "fallback", "sha256": "b" * 64}
    assert setup_flow.package_candidates(api, pin) == [api, pin]
    assert setup_flow.package_candidates(api, dict(api, source="fallback")) == [api]
    assert setup_flow.package_candidates(pin, pin) == [pin]
    assert setup_flow.package_candidates(api, None) == [api]


def test_extract_retries_with_pin_when_tuning_missing(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, SAMPLE)
    monkeypatch.setattr(setup_flow.paths, "TMP_DIR", str(tmp_path / "tmp"))
    attempts = []

    def fake_download(url, dest, sha=None, size=None, progress=None, timeout=0):
        attempts.append(url)
        open(dest, "wb").close()
        return sha

    def fake_extract(exe, codec, progress=None, package=None):
        if package["sha256"] == "9b0481de" + "22" * 28:
            raise asus_fetch.NoTuning("Package contains no tuning for DEV_0294_SUBSYS_10431EB3")
        return {"xml_name": "DEV_0294_SUBSYS_10431EB3_PCI_SUBSYS_1EB31043.xml", "package": package}

    monkeypatch.setattr(asus_fetch, "download", fake_download)
    monkeypatch.setattr(asus_fetch, "extract_dax3", fake_extract)
    events = []
    codec = {"ssid": "10431eb3", "dev": "0294"}
    api_pick = {"source": "asus-api", "title": "Dolby Atmos driver", "version": "V9.816.706.24",
                "url": "https://cdn.example/pub/ASUS/16152.exe", "sha256": "9b0481de" + "22" * 28}
    candidates = setup_flow.package_candidates(api_pick, asus_fetch.pinned_package("10431eb3"))
    assert [c["source"] for c in candidates] == ["asus-api", "fallback"]
    prov = None
    for i, cand in enumerate(candidates):
        try:
            prov = setup_flow._download_and_extract(events.append, cand, codec, lambda: None)
            break
        except asus_fetch.NoTuning:
            if i == len(candidates) - 1:
                raise
    assert attempts == ["https://cdn.example/pub/ASUS/16152.exe", "https://cdn.example/b.exe"]
    assert prov["package"]["source"] == "fallback"
    assert not os.listdir(tmp_path / "tmp")
    assert [e["step"] for e in events].count("download") >= 2
