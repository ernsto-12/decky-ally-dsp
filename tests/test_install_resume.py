"""Installer bootstrap and real descriptor rollback with generated fixtures."""
import copy
import os
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from allydsp import dsp_runtime, original_assets

INSTALLER = Path(__file__).resolve().parents[1] / 'install.sh'


@pytest.fixture
def backend(monkeypatch):
    state = {'enabled': True, 'engine': {'kind': 'original'}, 'original': {'outputGainDb': 4.5}}
    calls = []
    monkeypatch.setattr(dsp_runtime.settings, 'load', lambda: copy.deepcopy(state))
    monkeypatch.setattr(dsp_runtime.original_runtime, '_validate_hardware', lambda: calls.append('hardware'))
    monkeypatch.setattr(dsp_runtime.original_runtime, 'availability', lambda: {'available': False, 'error': 'old adapter pin'})
    monkeypatch.setattr(dsp_runtime.original_runtime, 'health', lambda: {'reason': 'service-inactive'})
    monkeypatch.setattr(original_assets, 'upgrade_engine_profiles', lambda: calls.append('upgrade'))
    monkeypatch.setattr(dsp_runtime, 'ensure_unit', lambda: calls.append('unit'))
    monkeypatch.setattr(dsp_runtime, 'start', lambda: calls.append('start') or True)
    return state, calls


def test_active_original_v1_is_upgraded_before_new_runtime_starts(backend):
    state, calls = backend
    previous = copy.deepcopy(state)
    assert dsp_runtime.resume_after_install()
    assert calls == ['hardware', 'upgrade', 'unit', 'start']
    assert state == previous


def test_current_original_pack_does_not_reimport(backend, monkeypatch):
    _, calls = backend
    monkeypatch.setattr(dsp_runtime.original_runtime, 'availability', lambda: {'available': True, 'controlsAvailable': True})
    assert dsp_runtime.resume_after_install()
    assert calls == ['hardware', 'unit', 'start']


def test_older_schema2_pack_with_controls_still_upgrades_new_worker_files(backend, monkeypatch):
    _, calls = backend
    monkeypatch.setattr(dsp_runtime.original_runtime, 'availability', lambda: {
        'available': True, 'controlsAvailable': True, 'needsUpgrade': True})
    assert dsp_runtime.resume_after_install()
    assert calls == ['hardware', 'upgrade', 'unit', 'start']


def test_disabled_setting_never_starts_or_upgrades_even_if_installer_saw_active_service(backend):
    state, calls = backend
    state['enabled'] = False
    assert dsp_runtime.resume_after_install()
    assert calls == []


def test_explicit_external_output_is_preserved_after_private_pack_upgrade(backend, monkeypatch):
    _, calls = backend
    monkeypatch.setattr(dsp_runtime.original_runtime, 'health', lambda: {'reason': 'external-output-selected'})
    assert dsp_runtime.resume_after_install()
    assert calls == ['hardware', 'upgrade']


def test_other_hardware_cannot_trigger_upgrade_or_start(backend, monkeypatch):
    _, calls = backend
    def reject():
        raise RuntimeError('unsupported subsystem')
    monkeypatch.setattr(dsp_runtime.original_runtime, '_validate_hardware', reject)
    with pytest.raises(RuntimeError, match='unsupported subsystem'):
        dsp_runtime.resume_after_install()
    assert calls == []


def test_converted_bootstrap_keeps_existing_unit_start_flow(backend):
    state, calls = backend
    state['engine']['kind'] = 'converted'
    assert dsp_runtime.resume_after_install()
    assert calls == ['unit', 'start']


def test_private_upgrade_failure_propagates_before_start(backend, monkeypatch):
    _, calls = backend
    def fail():
        calls.append('upgrade')
        raise RuntimeError('pinned source unavailable')
    monkeypatch.setattr(original_assets, 'upgrade_engine_profiles', fail)
    with pytest.raises(RuntimeError, match='pinned source unavailable'):
        dsp_runtime.resume_after_install()
    assert calls == ['hardware', 'upgrade']


def resume_snippet():
    match = re.search(r"python3 -c '([^']*resume_after_install[^']*)'", INSTALLER.read_text())
    assert match
    return match.group(1)


@pytest.mark.parametrize('modern', [False, True])
def test_installer_uses_bootstrap_when_present_and_old_interface_for_rollback(monkeypatch, modern):
    calls = []
    facade = SimpleNamespace(ensure_unit=lambda: calls.append('unit'), start=lambda: calls.append('start') or True)
    if modern:
        facade.resume_after_install = lambda: calls.append('bootstrap') or True
    package = ModuleType('allydsp')
    package.dsp_runtime = facade
    package.settings = SimpleNamespace(load=lambda: {'enabled': True, 'engine': {'kind': 'original'}})
    package.original_runtime = SimpleNamespace(health=lambda: {'reason': 'service-inactive'})
    monkeypatch.setitem(sys.modules, 'allydsp', package)
    exec(resume_snippet(), {})
    assert calls == (['bootstrap'] if modern else ['unit', 'start'])


@pytest.mark.parametrize('enabled,reason,have_health', [
    (False, 'service-inactive', True), (True, 'external-output-selected', True),
    (True, 'unknown', False),
])
def test_legacy_rollback_also_respects_off_or_external_output(monkeypatch, enabled, reason, have_health):
    calls = []
    package = ModuleType('allydsp')
    package.dsp_runtime = SimpleNamespace(ensure_unit=lambda: calls.append('unit'), start=lambda: calls.append('start') or True)
    package.settings = SimpleNamespace(load=lambda: {'enabled': enabled, 'engine': {'kind': 'original'}})
    package.original_runtime = SimpleNamespace()
    if have_health:
        package.original_runtime.health = lambda: {'reason': reason}
    monkeypatch.setitem(sys.modules, 'allydsp', package)
    exec(resume_snippet(), {})
    assert calls == []


def test_legacy_converted_plugin_does_not_require_original_runtime_module(monkeypatch):
    calls = []
    package = ModuleType('allydsp')
    package.dsp_runtime = SimpleNamespace(ensure_unit=lambda: calls.append('unit'), start=lambda: calls.append('start') or True)
    package.settings = SimpleNamespace(load=lambda: {'enabled': True})
    monkeypatch.setitem(sys.modules, 'allydsp', package)
    exec(resume_snippet(), {})
    assert calls == ['unit', 'start']


def descriptor_code():
    return INSTALLER.read_text().split("<<'ORIGINAL_STATE'\n", 1)[1].split('\nORIGINAL_STATE', 1)[0]


def descriptor(action, original, backup, *, check=True):
    return subprocess.run([sys.executable, '-', action, str(original), str(backup)], input=descriptor_code(),
                          text=True, capture_output=True, check=check)


requires_unix = pytest.mark.skipif(not hasattr(os, 'chown'), reason='Installer ownership rollback is a Linux operation')


@requires_unix
def test_failed_bootstrap_restores_old_manifest_active_and_control_absence_before_old_resume(tmp_path, backend, monkeypatch):
    state, calls = backend
    original, backup = tmp_path / 'original', tmp_path / 'snapshot'
    original.mkdir()
    old_manifest = b'{"generation":"old","worker":"old-frozen-adapter"}\n'
    old_active = b'{"profile":"game","outputGainDb":4.5}\n'
    (original / 'engine.json').write_bytes(old_manifest)
    (original / 'active.json').write_bytes(old_active)
    (original / 'engine.json').chmod(0o600)
    descriptor('backup', original, backup)
    new_generation = original / 'assets/new-immutable-generation'
    def upgrade():
        calls.append('upgrade')
        new_generation.mkdir(parents=True)
        (new_generation / 'worker.py').write_text('new immutable worker')
        (original / 'engine.json').write_text('{"generation":"new"}')
    monkeypatch.setattr(original_assets, 'upgrade_engine_profiles', upgrade)
    def failed_start():
        calls.append('start')
        (original / 'active.json').write_text('{"profile":"movie"}')
        (original / 'controls.json').write_text('{"controls":{"eqEnabled":true}}')
        return False
    monkeypatch.setattr(dsp_runtime, 'start', failed_start)
    assert not dsp_runtime.resume_after_install()
    descriptor('restore', original, backup)
    assert (original / 'engine.json').read_bytes() == old_manifest
    assert (original / 'active.json').read_bytes() == old_active
    assert not (original / 'controls.json').exists()
    assert (original / 'engine.json').stat().st_mode & 0o777 == 0o600
    assert new_generation.is_dir()  # immutable generations are never deleted
    assert state == {'enabled': True, 'engine': {'kind': 'original'}, 'original': {'outputGainDb': 4.5}}
    # The old resume interface observes the restored descriptor, not new pins.
    old_resume = lambda: (original / 'engine.json').read_bytes() == old_manifest
    assert old_resume()


@requires_unix
def test_successful_v1_bootstrap_keeps_new_generation_descriptor_and_existing_controls(tmp_path, backend, monkeypatch):
    original, backup = tmp_path / 'original', tmp_path / 'snapshot'
    original.mkdir()
    (original / 'engine.json').write_text('{"generation":"old"}')
    (original / 'controls.json').write_text('{"controls":{"dialogEnabled":true}}')
    descriptor('backup', original, backup)
    monkeypatch.setattr(original_assets, 'upgrade_engine_profiles', lambda: (original / 'engine.json').write_text('{"generation":"new"}'))
    monkeypatch.setattr(dsp_runtime, 'start', lambda: 'new' in (original / 'engine.json').read_text())
    assert dsp_runtime.resume_after_install()
    assert (original / 'engine.json').read_text() == '{"generation":"new"}'
    assert (original / 'controls.json').read_text() == '{"controls":{"dialogEnabled":true}}'
    assert (backup / 'engine.json').read_text() == '{"generation":"old"}'


@requires_unix
def test_tampered_snapshot_is_rejected_before_any_descriptor_changes(tmp_path):
    original, backup = tmp_path / 'original', tmp_path / 'snapshot'
    original.mkdir()
    for name in ('engine.json', 'active.json', 'controls.json'):
        (original / name).write_text('original ' + name)
    descriptor('backup', original, backup)
    for name in ('engine.json', 'active.json', 'controls.json'):
        (original / name).write_text('new ' + name)
    (backup / 'engine.json').write_text('tampered')
    result = descriptor('restore', original, backup, check=False)
    assert result.returncode and 'snapshot digest differs' in result.stderr
    assert all((original / name).read_text() == 'new ' + name for name in ('engine.json', 'active.json', 'controls.json'))


@requires_unix
def test_absent_old_descriptors_are_removed_without_touching_other_private_files(tmp_path):
    original, backup = tmp_path / 'original', tmp_path / 'snapshot'
    descriptor('backup', original, backup)
    original.mkdir()
    for name in ('engine.json', 'active.json', 'controls.json', 'unrelated.bin'):
        (original / name).write_text('new')
    descriptor('restore', original, backup)
    assert sorted(path.name for path in original.iterdir()) == ['unrelated.bin']


@requires_unix
def test_absent_original_directory_is_not_created_during_rollback(tmp_path):
    original, backup = tmp_path / 'original', tmp_path / 'snapshot'
    descriptor('backup', original, backup)
    descriptor('restore', original, backup)
    assert not original.exists()


@requires_unix
def test_descriptor_symlink_is_rejected_before_reading_its_target(tmp_path):
    original, backup = tmp_path / 'original', tmp_path / 'snapshot'
    original.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside bytes')
    (original / 'engine.json').symlink_to(outside)
    result = descriptor('backup', original, backup, check=False)
    assert result.returncode and 'bounded regular' in result.stderr
    assert outside.read_bytes() == b'outside bytes'


def test_installer_stops_before_snapshot_and_restores_before_old_resume():
    text = INSTALLER.read_text()
    assert text.index('original_descriptor_state backup') > text.index('then user_systemctl stop ally-dsp.service; fi')
    assert text.index('original_descriptor_state backup') < text.index('mv -- "$STAGE" "$PLUGIN_BASE/$PLUGIN_NAME"')
    assert text.index('original_descriptor_state restore') < text.index('[ "$DSP_ACTIVE" != 1 ] || [ "$DESCRIPTORS_RESTORED"')


@pytest.mark.skipif(os.name == 'nt', reason='The production installer runs under Linux bash')
def test_installer_shell_syntax():
    subprocess.run(['bash', '-n', str(INSTALLER)], check=True, capture_output=True, text=True)


@pytest.mark.parametrize('active', [False, True])
def test_installer_object_guard_blocks_active_sessions_using_old_installed_module(monkeypatch, active):
    source = INSTALLER.read_text()
    match = re.search(r"python3 -c '([^']*active_units[^']*)'", source)
    assert match
    calls = []
    package = ModuleType('allydsp')
    package.object_service = SimpleNamespace(active_units=lambda: calls.append('inspect') or (['ally-dsp-object-session-'+'a'*32+'.service'] if active else []))
    monkeypatch.setitem(sys.modules, 'allydsp', package)
    if active:
        with pytest.raises(SystemExit, match='Exit object-audio games before updating'):
            exec(match.group(1), {})
    else:
        exec(match.group(1), {})
    assert calls == ['inspect']


@pytest.mark.skipif(os.name == 'nt', reason='The production installer guard runs under Linux bash')
@pytest.mark.parametrize('installed,active', [(False, False), (False, True), (True, False), (True, True)])
def test_real_installer_object_guard_skips_legacy_and_stops_before_mutation(tmp_path, installed, active):
    text = INSTALLER.read_text()
    function = re.search(r'^guard_object_audio_sessions\(\) \{.*?^\}', text, flags=re.M | re.S)
    assert function
    base = tmp_path/'plugins'
    package = base/'Ally DSP/py_modules/allydsp'
    package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    if installed:
        (package/'object_service.py').write_text(
            'import os\ndef active_units():\n'
            '    print("INSPECTED")\n'
            '    return ["own-uuid-session"] if os.environ["TEST_OBJECT_ACTIVE"]=="1" else []\n')
    # Replace sudo only; execute the production function, env and Python guard.
    script = 'set -e\nsudo() { shift 2; "$@"; }\n' + function.group(0) + '\nguard_object_audio_sessions\nprintf "MUTATION_ALLOWED\\n"\n'
    environment = dict(os.environ, PLUGIN_BASE=str(base), PLUGIN_NAME='Ally DSP', DECK_USER='deck',
                       DECK_UID=str(os.getuid()), USER_HOME=str(tmp_path), TEST_OBJECT_ACTIVE='1' if active else '0')
    result = subprocess.run(['bash', '-s'], input=script, env=environment, capture_output=True, text=True)
    assert ('INSPECTED' in result.stdout) == installed
    if installed and active:
        assert result.returncode and 'Exit object-audio games before updating' in result.stderr
        assert 'MUTATION_ALLOWED' not in result.stdout
    else:
        assert result.returncode == 0 and 'MUTATION_ALLOWED' in result.stdout


def test_object_guard_is_checked_before_download_and_again_before_any_installed_state_mutation():
    text = INSTALLER.read_text()
    first = text.index('\nguard_object_audio_sessions\n')
    last = text.rindex('\nguard_object_audio_sessions\n')
    assert first < text.index('TMP="$(mktemp -d)"')
    assert last > text.index('chown -R "$DECK_USER:$DECK_USER" "$STAGE"')
    assert last < text.index('MUTATING=1')
    assert last < text.index('[ "$LOADER_ACTIVE" != 1 ] || systemctl stop plugin_loader')
    assert last < text.index('original_descriptor_state backup')


def test_exclusive_update_lock_wraps_final_guard_and_all_cleanup():
    text=INSTALLER.read_text()
    acquired=text.index('\nacquire_object_update_lock\n')
    assert acquired>text.index('chown -R "$DECK_USER:$DECK_USER" "$STAGE"')
    assert acquired<text.rindex('\nguard_object_audio_sessions\n')<text.index('MUTATING=1')
    release=text.index('exec {OBJECT_LOCK_WRITE}>&-')
    assert release>text.index('original_descriptor_state restore')
    assert release>text.index('rm -rf -- "$TMP"')
    # Validate the exact embedded user-side helper even on the Windows editor.
    helper=re.search(r"python3 -c '\n(import fcntl.*?)\n' \"\$USER_HOME/homebrew/data/",text,re.S)
    assert helper
    compile(helper.group(1),'installer-object-lock','exec')
