"""Import a locally owned, verified original engine pack into private data.

No Dolby binary or tuning value is distributed here. Only SHA256 identifiers of
the reviewed files are included. Importing does not start audio, change routing,
download assets or change the selected engine. Old generations are preserved.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any, Dict, Optional

from . import paths
from .util import atomic_write_text

VENDOR_PINS = {
    "DolbyAudioProcessing.dll": "0fada40251880d03c657a357745d362799c93fb6024f733f1c8bf1057f5e4715",
    "DolbyHrtfEnc.dll": "3da34330b36a79d6c4e42220190ee8239cad65564911c248ebda59d237bf9f88",
    "game-init-source.bin": "b6be9fd91f09bab641f99db05d02b8f19fd93f64e5c0e6c425048c0357c5b168",
    "po-enable-0.bin": "59250bb431a88665fe095477fee0fbea30c928cc038ff34fd83313bd8d0bedd8",
    "headphone-game-output-spatial-rendering.bin": "cdda85711544f2640c674dbae82878f5c83f1441c365f94960f3875220428289",
    "speaker-game-supported-cp.bin": "88e29379fe4007d623daee006eb9b7e6786db7fb79e49401dfef989b648cfb1a",
    "DolbyDAXVLLDP160.dll": "28990e8d5845b012020b52947ceab0cf37199ede1e8162cd7d7a03db580af82c",
    "speaker-game-vlldp-plan.json": "f36a04973a6e373b8ba0cb734c14ac6d2d0d401ffe4347be448091940e3348af",
    "audioeng.dll": "00d8b24227cb93d7576e3b07da1cdc25b2377e946b7eaa586c98b8d5e0b87a43",
}
# Reviewed CPython 3.12 x64 embedded distribution, including its license.
PYTHON_PINS = {
    "_asyncio.pyd": "c77de90cad749087a0c5dc5e19d78d24f6ef44e7e5ac19b8420cfc0cea62395d",
    "_bz2.pyd": "65f610646d10d5babea080e060d244b546ec52c54012e5e937dc72dcc2e2e7bc",
    "_ctypes.pyd": "0e56107a891100535f2da79d85a5ae65ff1227b4765bd108f8593ac1a1110d81",
    "_decimal.pyd": "aeacfead2befcc0768063a037bb64bd32109ae09cb144e5f571b04dd7965db9d",
    "_elementtree.pyd": "5009a9a53c03ef8387443b5cf6ce08e5c3a79727466a0b931c5bbca915bf89ca",
    "_hashlib.pyd": "03d233dbace599168eaffec823de6ed7beec8c2a4ebfe9b8a3d8e042a59af3ba",
    "_lzma.pyd": "e3deecc1f59a3b811c608861decd84de1e8df8bafd5e2b39188861318c5a38cf",
    "_msi.pyd": "b49332bae154ebdd8c3d5e7ddfdde336d9c47f91ba028dc67bafb2420b3dbb5c",
    "_multiprocessing.pyd": "2c636df7059cb0dd7e978dd735c7c5c8ddb09cf94754cf9e232296b96c5c545a",
    "_overlapped.pyd": "662126ff80c3a1b4cedf74074029c12ae4743e6d3b37376e39ac70aacb7d4d3c",
    "_queue.pyd": "48e76e0ad63f6932b4a77b5819f41c1e7595636ca8d9e824f0943a14b8bb5035",
    "_socket.pyd": "3efa4636cf9e350856f6927d67a4c2564bf9bd2758a00bbf17d3aece5b60016d",
    "_sqlite3.pyd": "64b51df2805c58b023a875e8ae3be7ac7f61ef664f32ca6c90f6315da47ef0bc",
    "_ssl.pyd": "0bc527d2dbc50009f43b2ed5d5982c4f63f4b647647ffa9eda612dcda80f6040",
    "_uuid.pyd": "acce4f281d7ffb2dec0997527faf25b1df62ad7bd3032c568ddcf65c90edc867",
    "_wmi.pyd": "a980f1e7a29842bd946863fc2693fbeb555a9dc0f9e1630dfed9a2fcdef3ece2",
    "_zoneinfo.pyd": "76f1439cda82ab6976df6ba505c4079444703f6d44d8b0259ce59c0ff4279fb4",
    "libcrypto-3.dll": "ccfffddcd3defb8d899026298af9af43bc186130f8483d77e97c93233d5f27d7",
    "libffi-8.dll": "eff52743773eb550fcc6ce3efc37c85724502233b6b002a35496d828bd7b280a",
    "libssl-3.dll": "007142039f04d04e0ed607bda53de095e5bc6a8a10d26ecedde94ea7d2d7eefe",
    "LICENSE.txt": "e502c6b880ff58d614901495a9009c136539cd0b1e2a2abb8fc00b934c203419",
    "pyexpat.pyd": "d615e193d1affcdbfe504e90d3621cbdcff94bf72b79d899126534507fd6d27a",
    "python.cat": "5d85439736ae088d9680b391e2bca55ca7e9cc5b41a12ebe8e2f554506c7e72e",
    "python.exe": "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a",
    "python3.dll": "fb975a606e7fbf74f64260e3f60c3490b4f74a183c0926fd6ed1ac4c52ac7b1c",
    "python312._pth": "2820f241bc9d6810d4db21c21cca3845799367fbdf0199620fb37c86a74b945c",
    "python312.dll": "9a0e3435aaa680d868150f87ab3e388ad2eebc22f87e036155c7b4eda8cd2120",
    "python312.zip": "fb131c0ef7e35cc5250a74c8cd18744bf4115fb8163710711f3758d7df3d1f88",
    "pythonw.exe": "d72294fb338bc2fc8896d25a7395a4db466425427e1559e77185d5135a830681",
    "select.pyd": "d4d7181b11db0bd22a6cd5cda6f8583d1f5c77615fd0a6ca33350619d6e0438a",
    "sqlite3.dll": "8daa45684bf6688fca28d956b2e8942f2fa98f656675ce68e6d61dd869848a7a",
    "unicodedata.pyd": "208ee1cd3cbad0828433ca7246b05be2b1ff5569983d27d296c0b38888585488",
    "vcruntime140.dll": "052ad6a20d375957e82aa6a3c441ea548d89be0981516ca7eb306e063d5027f4",
    "vcruntime140_1.dll": "6a99bc0128e0c7d6cbbf615fcc26909565e17d4ca3451b97f8987f9c6acbc6c8",
    "winsound.pyd": "100e8d676777603c7258daf9b2f23ef001f09f1596d8708de359326fff72bddd"
}
# Updated as a unit with the public original-engine worker bundle.
PUBLIC_PINS = {
    "frontend_adapter.py": "265d63f39de0b43fdef6096803107599d527b58a63596e861ab63f2ef749e0f3",
    "hrtf_adapter.py": "24cefd10d90d04d644663d7cc2d7d31eaa627b71af06be63496cd078c7167580",
    "linux_client.py": "4d5a22c6b4892ac0b1b787330548987e91a2f68ff3152e7d859818d1f76bfa62",
    "local_property_store.py": "f5cfc1d90ce8c58ab70bf700a941645bed340ed4f54b31a41d4800473877c0b4",
    "pipewire_bridge.py": "e8179c62b0c0315f4aa196ad39bc2ac1b01a710fb2a4a4034c0e21f364c86472",
    "pipewire_graph.py": "78c77c460ffddaf24a423e220b5c3e4b431a22327ca9d6fcfa513899150ee93e",
    "protocol.py": "d4f6c7e0f48e3ae0396783cd91c6732f23a91966f15358a4713c2a614e76ccc4",
    "vlldp_adapter.py": "e2c6c7c2a8acdcaa87f00598a52635ff786d52944f1858f7e4514ba3b600ee6e",
    "windows_worker.py": "e91131019bfb372c47c81fb57fd043b4ed22c4195a793aeb987e0d323bb23a30"
}

LABEL = "Original Dolby / Auto Game"
SCOPE = ("Official Speaker Game runtime controls or Headphone Game spatial configuration; 5.1 PCM → stereo. "
         "The active analog route selects the profile. Device speaker calibration "
         "equivalence and Atmos object metadata are not established.")
HEADPHONE_LABEL = "Original Dolby / Headphone Game"
HEADPHONE_SCOPE = ("Official generic headphone Game; 5.1 channel beds → binaural stereo. "
                   "Atmos object metadata is unavailable from PCM.")
SPEAKER_LABEL = "Original Dolby / Speaker Game"
SPEAKER_SCOPE = ("Official Speaker Game controls with an original VLLDP160 speaker post-stage candidate; 5.1 PCM → stereo. "
                "The live Windows VLLDP version/order, device speaker calibration equivalence and Atmos object metadata are not established.")
MAX_FILE_BYTES = 64 * 1024 * 1024


class AssetError(ValueError):
    pass


def _absolute(value: os.PathLike, label: str) -> Path:
    text = os.fspath(value)
    if any(char in text for char in ("\0", "\r", "\n")):
        raise AssetError("Invalid " + label + " path")
    path = Path(text).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise AssetError(label + " must be an absolute path without parent traversal")
    return path


def _directory(value: os.PathLike, label: str) -> Path:
    path = _absolute(value, label)
    if path.is_symlink() or not path.is_dir():
        raise AssetError(label + " must be an existing regular directory")
    return path.resolve(strict=True)


def _regular_file(path: Path) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise AssetError("Expected a bounded regular file: " + path.name)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise AssetError("Source file changed while opening: " + path.name)
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) != info.st_size or len(data) > MAX_FILE_BYTES:
            raise AssetError("Source file changed while reading: " + path.name)
        return data
    except OSError as error:
        raise AssetError("Cannot read required regular file: " + path.name) from error


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verified_files(directory: Path, pins: Dict[str, str]) -> Dict[str, bytes]:
    if not pins:
        raise AssetError("The public engine bundle has not been pinned")
    result = {}
    for name, expected in pins.items():
        if Path(name).name != name or name in (".", "..") or "/" in name or "\\" in name:
            raise AssetError("Invalid pinned asset name")
        data = _regular_file(directory / name)
        if _hash(data) != expected:
            raise AssetError("Asset SHA256 differs from the verified pack: " + name)
        result[name] = data
    return result


def _private_directory(path: Path):
    # Reject writable-path redirection before creating any destination files.
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise AssetError("Private engine destination contains a symbolic link")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise AssetError("Private engine destination is not a directory")


def _steam_directory(proton: Path, explicit: Optional[os.PathLike]) -> Path:
    if explicit is not None:
        return _directory(explicit, "Steam directory")
    for parent in proton.parents:
        if parent.name in ("steamapps", "compatibilitytools.d"):
            return _directory(parent.parent, "Steam directory")
    raise AssetError("Cannot derive Steam installation; provide --steam-dir")


def _windows_path(path: Path) -> str:
    # Proton maps the Linux filesystem at Z:. Arguments remain separate tokens;
    # paths with spaces are never interpolated into a shell command.
    return "Z:" + path.as_posix().replace("/", "\\")


def _verify_generation(target: Path, pins: Dict[str, str]):
    seen = set()
    for item in target.rglob("*"):
        if item.is_symlink():
            raise AssetError("Existing engine generation contains a symbolic link")
        if item.is_dir():
            continue
        name = item.relative_to(target).as_posix()
        if name not in pins or _hash(_regular_file(item)) != pins[name]:
            raise AssetError("Existing private engine generation changed")
        seen.add(name)
    if seen != set(pins):
        raise AssetError("Existing private engine generation is incomplete")


def import_engine(source_dir: os.PathLike, python_dir: os.PathLike, proton: os.PathLike,
                  *, steam_dir: Optional[os.PathLike] = None,
                  runtime_dir: Optional[os.PathLike] = None,
                  public_dir: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """Validate local inputs, preserve immutable generations, commit manifest last.

    source_dir is a flat private pack containing the DLLs, source settings and
    allocator shim named in VENDOR_PINS. python_dir is the matching Windows
    embedded runtime. Both must already exist; this function never downloads.
    """
    source = _directory(source_dir, "Private source")
    python = _directory(python_dir, "Windows Python")
    public = _directory(public_dir or Path(paths.PLUGIN_DIR) / "py_modules/allydsp/original", "Public worker")
    proton_path = _absolute(proton, "Proton")
    proton_data = _regular_file(proton_path)
    if os.name != "nt" and not os.access(proton_path, os.X_OK):
        raise AssetError("Proton must be executable")
    proton_path = proton_path.resolve(strict=True)
    steam = _steam_directory(proton_path, steam_dir)
    vendor_files = _verified_files(source, VENDOR_PINS)
    python_files = _verified_files(python, PYTHON_PINS)
    public_files = _verified_files(public, PUBLIC_PINS)
    payloads = {**{"original/" + name: data for name, data in vendor_files.items()},
                **{"python-embed/" + name: data for name, data in python_files.items()},
                **{"worker/" + name: data for name, data in public_files.items()}}
    file_pins = {name: _hash(data) for name, data in sorted(payloads.items())}
    generation = _hash(json.dumps(file_pins, sort_keys=True, separators=(",", ":")).encode())
    original = _absolute(runtime_dir or paths.RUNTIME_DIR, "Runtime directory") / "original"
    assets_root = original / "assets"
    _private_directory(assets_root)
    manifest_path = original / "engine.json"
    if manifest_path.is_symlink():
        raise AssetError("Engine manifest must not be a symbolic link")
    target = assets_root / generation
    if target.is_symlink():
        raise AssetError("Engine generation must not be a symbolic link")
    staging = Path(tempfile.mkdtemp(prefix=".import-", dir=assets_root))
    try:
        for name, data in payloads.items():
            output = staging / name
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        # The target is content addressed. Re-import never mutates an in-use
        # generation; a mismatch is rejected rather than overwritten.
        if target.exists():
            if not target.is_dir():
                raise AssetError("Existing engine generation is not a directory")
            _verify_generation(target, file_pins)
        else:
            os.replace(staging, target)
        compat = original / "compatdata" / generation
        _private_directory(compat)
        command = [str(proton_path), "runinprefix", _windows_path(target / "python-embed/python.exe"), "-u", "-B",
                   _windows_path(target / "worker/windows_worker.py"), "--engine", "hrtf-headphones",
                   "--dll", _windows_path(target / "original/DolbyAudioProcessing.dll"),
                   "--hrtf", _windows_path(target / "original/DolbyHrtfEnc.dll"),
                   "--frontend-adapter", _windows_path(target / "worker/frontend_adapter.py"),
                   "--property-store", _windows_path(target / "worker/local_property_store.py"),
                   "--init-blob", _windows_path(target / "original/game-init-source.bin"),
                   "--initial-runtime-blob", _windows_path(target / "original/po-enable-0.bin"),
                   "--runtime-blob", _windows_path(target / "original/headphone-game-output-spatial-rendering.bin"),
                   "--input-channels", "6"]
        environment = {
            "STEAM_COMPAT_DATA_PATH": str(compat), "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam),
            "STEAM_COMPAT_INSTALL_PATH": str(target), "STEAM_COMPAT_APP_ID": "0",
            "SteamAppId": "0", "SteamGameId": "0", "PROTON_LOG": "0",
            "PROTON_NO_ESYNC": "1", "PROTON_NO_FSYNC": "1", "WINEPREFIX": str(compat / "pfx"),
            "WINEDLLOVERRIDES": "winemenubuilder.exe=d;mscoree,mshtml=", "WINEDEBUG": "-all",
            "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "DISPLAY": ":0", "WAYLAND_DISPLAY": "",
        }
        speaker_command = list(command)
        speaker_command[speaker_command.index("--engine") + 1] = "hrtf-speakers"
        speaker_command[speaker_command.index("--runtime-blob") + 1] = _windows_path(target / "original/speaker-game-supported-cp.bin")
        speaker_command.extend([
            "--speaker-post-adapter", _windows_path(target / "worker/vlldp_adapter.py"),
            "--speaker-post-dll", _windows_path(target / "original/DolbyDAXVLLDP160.dll"),
            "--speaker-post-plan", _windows_path(target / "original/speaker-game-vlldp-plan.json"),
            "--speaker-post-shim", _windows_path(target / "original/audioeng.dll"),
        ])
        headphone_worker = {"command": command, "cwd": str(target), "environment": dict(environment)}
        speaker_environment = dict(environment, WINEDLLOVERRIDES=environment["WINEDLLOVERRIDES"] + ";audioeng=n")
        speaker_worker = {"command": speaker_command, "cwd": str(target), "environment": speaker_environment}
        profiles = {
            "headphones": {"label": HEADPHONE_LABEL, "scope": HEADPHONE_SCOPE,
                           "profile": "game", "voicing": "official-headphones", "worker": headphone_worker},
            "speakers": {"label": SPEAKER_LABEL, "scope": SPEAKER_SCOPE,
                         "profile": "game", "voicing": "official-speakers", "worker": speaker_worker},
        }
        files = [{"path": str(target / name), "sha256": expected} for name, expected in file_pins.items()]
        # Linux-side bridge/client imports are read from the installed plugin.
        files.extend({"path": str(public / name), "sha256": expected} for name, expected in PUBLIC_PINS.items())
        files.append({"path": str(proton_path), "sha256": _hash(proton_data)})
        config = {"schema": "ally-dsp-original-engine-v1", "generation": generation,
                  "sample_rate": 48000, "input_channels": 6, "output_channels": 2,
                  "files": files, "worker": headphone_worker, "profiles": profiles,
                  "label": LABEL, "scope": SCOPE, "profile": "game", "voicing": "official-headphones"}
        atomic_write_text(str(manifest_path), json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o600)
        return {"ok": True, "generation": generation, "manifest": str(manifest_path),
                "files": len(files), "label": LABEL, "scope": SCOPE, "profiles": list(profiles)}
    finally:
        if staging.exists():
            # This is exclusively our fresh staging directory. Never remove a
            # generation, prefix, source pack or caller-supplied path.
            if staging.parent.resolve() == assets_root.resolve() and staging.name.startswith(".import-") and not staging.is_symlink():
                shutil.rmtree(staging)
