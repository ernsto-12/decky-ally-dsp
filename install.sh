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
    python3 -c 'from allydsp import dsp_runtime; dsp_runtime.ensure_unit(); assert dsp_runtime.start(), "Audio engine failed to resume"'
}
TMP="$(mktemp -d)"
STAGE=""
BACKUP=""
MUTATING=0
LOADER_ACTIVE=0
DSP_ACTIVE=0
REPLACED=0
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
    user_systemctl daemon-reload || true
    [ "$DSP_ACTIVE" != 1 ] || resume_engine || echo "The previous audio engine could not resume; inspect the saved backup." >&2
    [ "$LOADER_ACTIVE" != 1 ] || systemctl start plugin_loader || true
  fi
  [ -z "$STAGE" ] || rm -rf -- "$STAGE"
  rm -rf -- "$TMP"
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
