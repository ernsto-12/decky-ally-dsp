"""One service owner, with converted and original-DLL runtime backends."""
from __future__ import annotations

from typing import Any, Dict

from . import converted_runtime, original_runtime, paths, settings

# Keep the established public interface for setup, jack handling, CLI and Decky.
ACTIVE_CONF = converted_runtime.ACTIVE_CONF
ACTIVE_IRS = converted_runtime.ACTIVE_IRS
ACTIVE_META = converted_runtime.ACTIVE_META
REMOVAL_UNIT_PREFIX = converted_runtime.REMOVAL_UNIT_PREFIX
REMOVAL_DELAY_S = converted_runtime.REMOVAL_DELAY_S


def engine_kind() -> str:
    return settings.load().get("engine", {}).get("kind", "converted")


def _backend():
    kind = engine_kind()
    if kind == "converted":
        return converted_runtime
    if kind == "original":
        return original_runtime
    raise ValueError("Unknown audio engine: " + str(kind))


def engine_status() -> Dict[str, Any]:
    result = original_runtime.availability()
    result["kind"] = engine_kind()
    return result


def unit_text() -> str:
    return _backend().unit_text()


def unit_installed() -> bool:
    return _backend().unit_installed()


def install_unit() -> None:
    _backend().install_unit()


def ensure_unit() -> None:
    _backend().ensure_unit()


def is_active() -> bool:
    return converted_runtime.is_active()


def is_enabled() -> bool:
    return converted_runtime.is_enabled()


def enable(on: bool = True) -> None:
    # The original virtual sink and route journal must be prepared before
    # starting. Decky reconciles this engine at login; systemd must not start a
    # stale runtime.json by itself during PipeWire/session startup.
    converted_runtime.enable(on and engine_kind() == "converted")


def start() -> bool:
    return _backend().start()


def stop() -> bool:
    # Restore any original-engine routing even after a failed previous startup.
    stopped = converted_runtime.stop()
    original_runtime.restore_routing()
    return stopped


def restart() -> bool:
    stop()
    ensure_unit()
    return start()


def resume_after_install() -> bool:
    """Resume an enabled engine before Decky starts after a plugin update.

    Original workers are immutable copies. Updating public adapters requires a
    new private generation before the new runtime validates or starts them.
    The installer snapshots the small generation descriptors for rollback.
    """
    state = settings.load()
    if not state.get("enabled"):
        return True
    if engine_kind() == "original":
        original_runtime._validate_hardware()
        available = original_runtime.availability()
        if not available.get("controlsAvailable") or available.get("needsUpgrade"):
            from .original_assets import upgrade_engine_profiles
            upgrade_engine_profiles()
        # Stopping the old worker must not make installation take over a USB,
        # Bluetooth or HDMI output selected by the user during that stop.
        if original_runtime.health().get("reason") == "external-output-selected":
            return True
    ensure_unit()
    return bool(start())


def active_meta():
    return _backend().active_meta()


def verify(timeout: float = 4.0) -> bool:
    return _backend().verify(timeout)


def apply_preset(profile: str, voicing: str, pregain_db: float = 0.0,
                 restart_unit: bool = True) -> Dict[str, Any]:
    return _backend().apply_preset(profile, voicing, pregain_db, restart_unit)


def journal(lines: int = 20) -> str:
    return converted_runtime.journal(lines)


def status(with_journal: bool = True) -> Dict[str, Any]:
    result = _backend().status(with_journal)
    result["engine"] = engine_kind()
    return result


def pause_for_headphones() -> bool:
    # Original rendering targets the active analog route, including headphones.
    return engine_kind() == "converted"


def remove_unit() -> None:
    stop()
    converted_runtime.remove_unit()


def removal_command(unit: str):
    return converted_runtime.removal_command(unit)


def schedule_removal() -> bool:
    return converted_runtime.schedule_removal()


def cancel_removal() -> None:
    converted_runtime.cancel_removal()
