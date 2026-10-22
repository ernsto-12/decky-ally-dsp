#!/usr/bin/env bash
# Replaces installed Ally DSP; same Decky ID and one user audio service.
# curl -fsSL https://raw.githubusercontent.com/ernsto-12/decky-ally-dsp/main/install.sh -o /tmp/ally-dsp-install.sh && sudo bash /tmp/ally-dsp-install.sh
set -euo pipefail
PLUGIN_NAME="Ally DSP"
REPO="ernsto-12/decky-ally-dsp"
PUBKEY="RWQTBD7XF11jehxTO8pC5Ido+yVLzy4FWr/ZI69x4ZTW3mTyK99etL1X"
OFFLINE_DIR=""
if [ "$#" != 0 ]; then
  [ "$#" = 2 ] && [ "$1" = --bundle-dir ] || { echo "Usage: $0 [--bundle-dir /absolute/signed-release-folder]" >&2; exit 1; }
  OFFLINE_DIR="$(realpath -- "$2")"
  [ -d "$OFFLINE_DIR" ] || exit 1
fi
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
DECK_USER="${SUDO_USER:-deck}"
[ "$DECK_USER" != root ] || { echo "Run sudo from the SteamOS user account." >&2; exit 1; }
USER_HOME="$(getent passwd "$DECK_USER" | cut -d: -f6)"
[ -n "$USER_HOME" ] && [ "$USER_HOME" != / ] || exit 1
PLUGIN_BASE="$USER_HOME/homebrew/plugins"
[ -d "$PLUGIN_BASE" ] || { echo "Install Decky Loader first: https://decky.xyz" >&2; exit 1; }
DECK_UID="$(id -u "$DECK_USER")"
user_systemctl() {
  sudo -u "$DECK_USER" env XDG_RUNTIME_DIR="/run/user/$DECK_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$DECK_UID/bus" systemctl --user "$@"
}
resume_engine() {
  sudo -u "$DECK_USER" env HOME="$USER_HOME" DECKY_USER_HOME="$USER_HOME" DECKY_USER="$DECK_USER" \
    ALLYDSP_PLUGIN_DIR="$PLUGIN_BASE/$PLUGIN_NAME" PYTHONPATH="$PLUGIN_BASE/$PLUGIN_NAME/py_modules" \
    XDG_RUNTIME_DIR="/run/user/$DECK_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$DECK_UID/bus" \
    python3 -c 'from allydsp import dsp_runtime
bootstrap=getattr(dsp_runtime,"resume_after_install",None)
if bootstrap:
    result=bootstrap()
else:
    from allydsp import settings
    state=settings.load(); deferred=not state.get("enabled")
    if not deferred and state.get("engine",{}).get("kind")=="original":
        from allydsp import original_runtime
        health=getattr(original_runtime,"health",None)
        deferred=health is None or health().get("reason")=="external-output-selected"
    result=True if deferred else (dsp_runtime.ensure_unit(),dsp_runtime.start())[1]
assert result, "Audio engine failed to resume"'
}
guard_object_audio_sessions() {
  # Older releases have no object renderer. Query an installed renderer before
  # stopping Decky or replacing any of its Python sources/private descriptors.
  [ -f "$PLUGIN_BASE/$PLUGIN_NAME/py_modules/allydsp/object_service.py" ] || return 0
  sudo -u "$DECK_USER" env HOME="$USER_HOME" DECKY_USER_HOME="$USER_HOME" DECKY_USER="$DECK_USER" \
    ALLYDSP_PLUGIN_DIR="$PLUGIN_BASE/$PLUGIN_NAME" PYTHONPATH="$PLUGIN_BASE/$PLUGIN_NAME/py_modules" \
    XDG_RUNTIME_DIR="/run/user/$DECK_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$DECK_UID/bus" \
    python3 -c 'from allydsp import object_service
if object_service.active_units():
    raise SystemExit("Exit object-audio games before updating")'
}
acquire_object_update_lock() {
  # A user-owned helper holds the exclusive lock while this shell mutates or
  # rolls back. Root never opens/creates a path inside a user-controlled tree.
  coproc ALLY_OBJECT_UPDATE_LOCK {
    sudo -u "$DECK_USER" env HOME="$USER_HOME" python3 -c '
import fcntl,os,pathlib,stat,sys
root=pathlib.Path(sys.argv[1])
if not root.is_absolute() or any(path.is_symlink() for path in (root,*root.parents)):
    raise SystemExit("Unsafe object launch/update directory")
root.mkdir(parents=True,exist_ok=True,mode=0o700)
if not root.is_dir() or root.stat().st_uid!=os.getuid():
    raise SystemExit("Object launch/update directory has another owner")
os.chmod(root,0o700)
path=root/".launch-update.lock"
fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
info=os.fstat(fd);named=path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino):
    raise SystemExit("Object launch/update lock changed")
os.fchmod(fd,0o600)
try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: raise SystemExit("Exit object-audio games before updating; a game launch is in progress")
print("LOCKED",flush=True)
sys.stdin.buffer.read(1)
' "$USER_HOME/homebrew/data/$PLUGIN_NAME/original/objects"
  }
  OBJECT_LOCK_PID="${ALLY_OBJECT_UPDATE_LOCK_PID:-}"
  OBJECT_LOCK_WRITE="${ALLY_OBJECT_UPDATE_LOCK[1]:-}"
  OBJECT_LOCK_READ="${ALLY_OBJECT_UPDATE_LOCK[0]:-}"
  local lock_result=""
  if [ -z "$OBJECT_LOCK_READ" ] || ! IFS= read -r -u "$OBJECT_LOCK_READ" lock_result || [ "$lock_result" != LOCKED ]; then
    echo "Could not acquire the object launch/update lock; retry after exiting games." >&2
    return 1
  fi
}
original_descriptor_state() {
  # These three small descriptors select an immutable private generation. Keep
  # every generation; restore the old descriptors before resuming an old plugin.
  python3 - "$1" "$USER_HOME/homebrew/data/$PLUGIN_NAME/original" "$BACKUP/original-descriptors" <<'ORIGINAL_STATE'
import hashlib,json,os,pathlib,stat,sys,tempfile
action=sys.argv[1];root=pathlib.Path(sys.argv[2]);backup=pathlib.Path(sys.argv[3])
names=('active.json','controls.json','engine.json')
limit=4*1024*1024
def regular(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit:
        raise RuntimeError('Expected a bounded regular original-engine descriptor: '+path.name)
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        opened=os.fstat(stream.fileno())
        if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):raise RuntimeError('Descriptor changed while opening')
        data=stream.read(limit+1)
    if len(data)!=info.st_size:raise RuntimeError('Descriptor changed while reading')
    return data,info
def safe_root():
    if any(path.is_symlink()for path in(root,*root.parents)):
        raise RuntimeError('Original-engine descriptor directory contains a symbolic link')
    if root.exists()and not root.is_dir():raise RuntimeError('Original-engine descriptor directory is not a directory')
safe_root()
if action=='backup':
    backup.mkdir(mode=0o700,exist_ok=False)
    records=[]
    for name in names:
        source=root/name
        try:data,info=regular(source)
        except FileNotFoundError:
            records.append({'name':name,'present':False});continue
        target=backup/name
        with target.open('xb')as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.chmod(target,0o600)
        records.append({'name':name,'present':True,'sha256':hashlib.sha256(data).hexdigest(),
                        'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'gid':info.st_gid,
                        'atime_ns':info.st_atime_ns,'mtime_ns':info.st_mtime_ns})
    metadata=backup/'snapshot.json'
    with metadata.open('x')as stream:json.dump({'schema':1,'records':records},stream);stream.flush();os.fsync(stream.fileno())
    os.chmod(metadata,0o600)
elif action=='restore':
    raw,_=regular(backup/'snapshot.json');snapshot=json.loads(raw)
    records=snapshot['records']
    if snapshot.get('schema')!=1 or [record['name']for record in records]!=list(names):raise RuntimeError('Invalid descriptor snapshot')
    # Validate the complete snapshot before changing any descriptor.
    payloads={}
    for record in records:
        if record['present']:
            data,_=regular(backup/record['name'])
            if hashlib.sha256(data).hexdigest()!=record['sha256']:raise RuntimeError('Descriptor snapshot digest differs')
            payloads[record['name']]=data
    if payloads:root.mkdir(mode=0o700,parents=True,exist_ok=True)
    for record in records:
        target=root/record['name']
        if target.is_symlink()or(target.exists()and not target.is_file()):raise RuntimeError('Unsafe original-engine descriptor destination')
    for record in records:
        target=root/record['name']
        if not record['present']:
            target.unlink(missing_ok=True);continue
        fd,temporary=tempfile.mkstemp(prefix='.installer-restore-',dir=root)
        try:
            with os.fdopen(fd,'wb')as stream:stream.write(payloads[record['name']]);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,record['mode']);os.chown(temporary,record['uid'],record['gid'])
            os.utime(temporary,ns=(record['atime_ns'],record['mtime_ns']));os.replace(temporary,target)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
else:raise RuntimeError('Unknown descriptor transaction action')
ORIGINAL_STATE
}
guard_object_audio_sessions
TMP="$(mktemp -d)"
STAGE=""
BACKUP=""
MUTATING=0
LOADER_ACTIVE=0
DSP_ACTIVE=0
REPLACED=0
DESCRIPTORS_SNAPSHOTTED=0
OBJECT_LOCK_PID=""
OBJECT_LOCK_READ=""
OBJECT_LOCK_WRITE=""
cleanup() {
  rc=$?
  trap - EXIT
  if [ "$rc" != 0 ] && [ "$MUTATING" = 1 ]; then
    echo "Installation failed; restoring $BACKUP" >&2
    systemctl stop plugin_loader || true
    user_systemctl stop ally-dsp.service || true
    if [ "$REPLACED" = 1 ] && [ -e "$PLUGIN_BASE/$PLUGIN_NAME" ]; then
      mv -- "$PLUGIN_BASE/$PLUGIN_NAME" "$BACKUP/failed-plugin"
    fi
    if [ -e "$BACKUP/plugin" ]; then
      mv -- "$BACKUP/plugin" "$PLUGIN_BASE/$PLUGIN_NAME"
    fi
    if [ -d "$BACKUP/settings" ]; then
      cp -a -- "$BACKUP/settings/." "$USER_HOME/homebrew/settings/$PLUGIN_NAME/"
    fi
    if [ -f "$BACKUP/ally-dsp.service" ]; then
      cp -a -- "$BACKUP/ally-dsp.service" "$USER_HOME/.config/systemd/user/ally-dsp.service"
    fi
    DESCRIPTORS_RESTORED=1
    if [ "$DESCRIPTORS_SNAPSHOTTED" = 1 ]; then
      original_descriptor_state restore || { DESCRIPTORS_RESTORED=0; echo "Original-engine descriptors could not be restored; inspect $BACKUP/original-descriptors before starting audio." >&2; }
    fi
    user_systemctl daemon-reload || true
    [ "$DSP_ACTIVE" != 1 ] || [ "$DESCRIPTORS_RESTORED" != 1 ] || resume_engine || echo "The previous audio engine could not resume; inspect the saved backup." >&2
    [ "$LOADER_ACTIVE" != 1 ] || systemctl start plugin_loader || true
  fi
  [ -z "$STAGE" ] || rm -rf -- "$STAGE"
  rm -rf -- "$TMP"
  # Release only after successful installation or complete rollback/cleanup.
  [ -z "$OBJECT_LOCK_WRITE" ] || exec {OBJECT_LOCK_WRITE}>&-
  [ -z "$OBJECT_LOCK_READ" ] || exec {OBJECT_LOCK_READ}<&-
  [ -z "$OBJECT_LOCK_PID" ] || wait "$OBJECT_LOCK_PID" || true
  exit "$rc"
}
trap cleanup EXIT
echo "Looking up the latest release from $REPO..."
if [ -n "$OFFLINE_DIR" ]; then cp -- "$OFFLINE_DIR/release.json" "$TMP/release.json"
else curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" -o "$TMP/release.json"; fi
TAG="$(python3 -c 'import json,sys,re; t=json.load(open(sys.argv[1]))["tag_name"]; assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+",t); print(t)' "$TMP/release.json")"
VERSION="${TAG#v}"
ZIP="ally-dsp-$VERSION.zip"
BASE="https://github.com/$REPO/releases/download/$TAG"
for FILE in "$ZIP" SHA256SUMS SHA256SUMS.minisig; do
  if [ -n "$OFFLINE_DIR" ]; then cp -- "$OFFLINE_DIR/$FILE" "$TMP/$FILE"
  else curl -fL --retry 3 "$BASE/$FILE" -o "$TMP/$FILE"; fi
done
# Only independently hash-pinned verifier source is executed before signature
# verification. The public key is pinned in this installer, outside the ZIP.
python3 - "$TMP" "$ZIP" "$PUBKEY" <<'PY'
import hashlib,pathlib,stat,sys,zipfile
root=pathlib.Path(sys.argv[1]); name=sys.argv[2]
with zipfile.ZipFile(root/name) as archive:
    entries=archive.infolist()
    if len(entries)>2500 or sum(x.file_size for x in entries)>512*1024*1024:
        raise SystemExit('Unexpected archive size')
    for item in entries:
        p=pathlib.PurePosixPath(item.filename)
        if p.is_absolute() or '..' in p.parts or '\\' in item.filename or not p.parts or p.parts[0]!='Ally DSP' or stat.S_ISLNK(item.external_attr>>16):
            raise SystemExit('Unsafe archive member')
    verifier=root/'verify'/'allydsp';verifier.mkdir(parents=True)
    (verifier/'__init__.py').write_text('')
    pins={'ed25519.py':'60b9c4475ab4d4a6b63bc1e9bb27a86b957f93d1e517c1c4f658c3054060c156',
          'minisign.py':'ba273cf3a71077f0f590b7f7aa667c82d9869b3ae66f49fab22c287f62e18373'}
    for file,pin in pins.items():
        raw=archive.read('Ally DSP/py_modules/allydsp/'+file)
        if hashlib.sha256(raw).hexdigest()!=pin:raise SystemExit('Verifier hash mismatch')
        (verifier/file).write_bytes(raw)
    sys.path.insert(0,str(verifier.parent))
    from allydsp.minisign import verify_bytes
    sums=(root/'SHA256SUMS').read_bytes()
    ok,reason=verify_bytes(sums,(root/'SHA256SUMS.minisig').read_text(),'untrusted comment: pinned fork key\n'+sys.argv[3]+'\n')
    if not ok:raise SystemExit('Signature rejected: '+reason)
    hashes={x.split()[-1].lstrip('*'):x.split()[0] for x in sums.decode().splitlines() if len(x.split())==2}
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=hashes.get(name):raise SystemExit('ZIP checksum mismatch')
    archive.extractall(root/'x')
print('Fork signature and checksum verified.')
PY
[ -f "$TMP/x/$PLUGIN_NAME/plugin.json" ] && [ -f "$TMP/x/$PLUGIN_NAME/dist/index.js" ] || exit 1
STAGE="$(mktemp -d "$PLUGIN_BASE/.ally-dsp-stage.XXXXXXXX")"
cp -a -- "$TMP/x/$PLUGIN_NAME/." "$STAGE/"
chown -R "$DECK_USER:$DECK_USER" "$STAGE"
# A game may have started while this release was downloaded and verified.
acquire_object_update_lock
guard_object_audio_sessions
mkdir -p "$USER_HOME/homebrew/ally-dsp-backups"
BACKUP="$(mktemp -d "$USER_HOME/homebrew/ally-dsp-backups/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")"
chmod 700 "$BACKUP"
SETTINGS="$USER_HOME/homebrew/settings/$PLUGIN_NAME"
[ ! -d "$SETTINGS" ] || cp -a -- "$SETTINGS" "$BACKUP/settings"
UNIT="$USER_HOME/.config/systemd/user/ally-dsp.service"
[ ! -f "$UNIT" ] || cp -a -- "$UNIT" "$BACKUP/ally-dsp.service"
systemctl is-active --quiet plugin_loader && LOADER_ACTIVE=1 || true
user_systemctl is-active --quiet ally-dsp.service && DSP_ACTIVE=1 || true
MUTATING=1
# Upstream unload leaves DSP active; stop its backend before replacement so
# the jack watcher cannot restart the old chain while files are moved.
[ "$LOADER_ACTIVE" != 1 ] || systemctl stop plugin_loader
if [ -f "$UNIT" ] || [ "$DSP_ACTIVE" = 1 ]; then user_systemctl stop ally-dsp.service; fi
original_descriptor_state backup
DESCRIPTORS_SNAPSHOTTED=1
[ ! -e "$PLUGIN_BASE/$PLUGIN_NAME" ] || mv -- "$PLUGIN_BASE/$PLUGIN_NAME" "$BACKUP/plugin"
mv -- "$STAGE" "$PLUGIN_BASE/$PLUGIN_NAME"
STAGE=""
REPLACED=1
# Discard cached upstream release URLs; this fork has a separate key/repo.
if [ -f "$SETTINGS/settings.json" ]; then
  python3 - "$SETTINGS/settings.json" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]);d=json.loads(p.read_text());d['update']={'autoCheck':d.get('update',{}).get('autoCheck',True)}
tmp=p.with_name('.settings-fork-install');tmp.write_text(json.dumps(d,indent=2)+'\n');os.chmod(tmp,p.stat().st_mode&0o777);os.chown(tmp,p.stat().st_uid,p.stat().st_gid);os.replace(tmp,p)
PY
fi
user_systemctl daemon-reload
# Preserve an already running engine even if Decky was deliberately stopped.
# The runtime facade prepares original routing before it starts the shared unit.
if [ "$DSP_ACTIVE" = 1 ]; then
  resume_engine
fi
# Decky startup prepares the selected engine, including its virtual sink.
[ "$LOADER_ACTIVE" != 1 ] || systemctl start plugin_loader
MUTATING=0
chown -R "$DECK_USER:$DECK_USER" "$BACKUP"
echo "Installed Ally DSP fork $VERSION. Backup: $BACKUP"
echo "Open Decky > Ally DSP. Converted settings and private original assets are preserved."
