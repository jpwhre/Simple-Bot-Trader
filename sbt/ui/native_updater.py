"""Native (frozen) silent self-update with A/B rollback.

For PyInstaller bundles (Windows setup.exe / Linux .deb / macOS .dmg), the
generic zip flow is useless: the app can't replace its own frozen binary. This
module implements the update strategy for native installs:

  1. fetch the release for a tag and VERIFY it:
       - SBT-NOTES-SIGNATURE (Ed25519, the same key as update signatures) over
         the release notes body, AND
       - `SBT-SHA256: <asset> <hex>` for the platform's installer asset.
     Fail-closed: unsigned/forged notes or a missing hash -> decline (a
     compromised GitHub account cannot forge a valid signature, so it cannot
     push a malicious native installer either).
  2. snapshot the CURRENT app folder -> <temp>/sbt_bak_<oldver> (rollback
     copy; the user-approved approach is a full temp snapshot).
  3. write a small native watcher (Windows .bat / Linux .sh / macOS .sh) and
     detach it, then the current app quits.
  4. the watcher runs the installer (Inno /VERYSILENT, pkexec dpkg -i, or
     dmg+ditto), relaunches the NEW app with `--update-verify=<tag>`, and
     polls for a health marker (`update_ok_<tag>.mrk` in the config dir).
  5. new app writes that marker ~8s after a healthy start (engine + UI up).
  6. on success  -> delete the backup, done.
     on timeout/crash -> restore the backup, mark the version SKIPPED, drop a
     `update_failed_<tag>.mrk` (the relaunched app files the encrypted report +
     skip entry), and relaunch the OLD build. Never re-offers the broken build.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from .. import paths
from .. import auto_update

# Asset file names per kind (versioned, from the release).
_ASSET_NAMES = {
    'windows': 'SimpleBotTrader-Setup-{tag}.exe',
    'linux':   'SimpleBotTrader-{tag}.deb',
    'macos':   'SimpleBotTrader-{tag}.dmg',
}


def frozen():
    return bool(getattr(sys, 'frozen', False))


def kind():
    if os.name == 'nt':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    return 'linux'


def _fetch_release(tag, urlopen=None):
    """Fetch release body/notes for `tag` from GitHub API."""
    url = (f'https://api.github.com/repos/{auto_update.UPDATES_OWNER}'
           f'/{auto_update.UPDATES_REPO}/releases/tags/v{tag}')
    uo = urlopen or urllib.request.urlopen
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'SimpleBotTrader/native-updater',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    with uo(req, timeout=15) as resp:
        if resp.status != 200:
            return None
        return json.loads(resp.read().decode('utf-8', 'ignore'))


# ---- release-notes signing (per-asset hashes) -------------------------------

def _notes_digest(body):
    """Canonical digest over the notes body EXCLUDING the SBT-NOTES-* lines
    (so the digest value can live inside the body it signs)."""
    import hashlib
    lines = [ln for ln in body.splitlines()
             if not ln.startswith(('SBT-NOTES-DIGEST:', 'SBT-NOTES-SIGNATURE:'))]
    return hashlib.sha256(('\n'.join(lines) + '\n').encode('utf-8')).hexdigest()


def verify_notes_signature(body, public_key_hex=None):
    """True when the release notes carry a valid SBT-NOTES-SIGNATURE over the
    body's SBT-NOTES-DIGEST, signed by the embedded (shipped) key."""
    from .. import auto_update
    digest = signature = None
    for ln in body.splitlines():
        if ln.startswith('SBT-NOTES-DIGEST:'):
            digest = ln.split(':', 1)[1].strip()
        elif ln.startswith('SBT-NOTES-SIGNATURE:'):
            signature = ln.split(':', 1)[1].strip()
    if not (digest and signature):
        return False
    if _notes_digest(body) != digest:
        return False
    key = public_key_hex or auto_update.SIGNING_PUBLIC_KEY
    return auto_update.verify_release_signature(digest, signature, key)


def parse_asset_hashes(body):
    """Parse `SBT-SHA256: <asset-name> <hex>` lines from notes."""
    out = {}
    for ln in body.splitlines():
        if ln.startswith('SBT-SHA256:'):
            parts = ln.split(None, 2)
            if len(parts) == 3:
                out[parts[1]] = parts[2].strip().lower()
    return out


class Plan:
    def __init__(self, tag, kind_, asset_url, sha256, body, reason=''):
        self.tag = tag
        self.kind = kind_
        self.asset_url = asset_url
        self.sha256 = sha256
        self.body = body
        self.reason = reason

    @property
    def ok(self):
        return bool(self.asset_url and self.sha256)


def build_plan(tag, urlopen=None):
    """Verify + build a native update plan for `tag`. Returns Plan (ok True)
    or a Plan with reason set (ok False) explaining why it was declined."""
    rel = _fetch_release(tag, urlopen)
    if not rel:
        return Plan(tag, kind(), '', '', '', 'release not found')
    body = rel.get('body') or ''
    if not verify_notes_signature(body):
        return Plan(tag, kind(), '', '', '', 'release notes not signed')
    hashes = parse_asset_hashes(body)
    name = _ASSET_NAMES[kind()].format(tag=tag)
    url = sha = ''
    for a in (rel.get('assets') or []):
        if a.get('name') == name:
            url = a.get('browser_download_url') or a.get('url')
            sha = hashes.get(name, '')
            break
    if not url:
        return Plan(tag, kind(), '', '', body, f'no {name} asset')
    if not sha:
        return Plan(tag, kind(), url, '', body, f'no signed SBT-SHA256 for {name}')
    return Plan(tag, kind(), url, sha, body)


def download_verified(plan, urlopen=None):
    """Download the installer and verify it matches the signed sha256.
    Returns the local path, or None on mismatch/error."""
    import hashlib
    uo = urlopen or urllib.request.urlopen
    try:
        req = urllib.request.Request(plan.asset_url, headers={
            'User-Agent': 'SimpleBotTrader/native-updater'})
        with uo(req, timeout=120) as resp:
            data = resp.read()
    except Exception:
        return None
    if hashlib.sha256(data).hexdigest().lower() != plan.sha256.lower():
        return None
    fd, path = tempfile.mkstemp(prefix='sbt_native_', suffix=os.path.splitext(plan.asset_url)[1] or '.bin')
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    return path


# ---- backup + health marker -------------------------------------------------

def app_root():
    return os.path.dirname(os.path.abspath(sys.executable))


def backup_dir(tag):
    b = os.path.basename(app_root()) or 'simple-bot-trader'
    return os.path.join(tempfile.gettempdir(), f'sbt_bak_{b}_{tag}')


def snapshot_backup(tag):
    dst = backup_dir(tag)
    try:
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(app_root(), dst,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        return True
    except Exception:
        return False


def marker_ok(tag):
    return os.path.join(paths.CONFIG_DIR, f'update_ok_{tag}.mrk')


def marker_failed(tag):
    return os.path.join(paths.CONFIG_DIR, f'update_failed_{tag}.mrk')


def mark_update_ok(tag):
    """Called by the NEW app after a healthy start (--update-verify=<tag>)."""
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        with open(marker_ok(tag), 'w') as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def mark_update_failed(tag):
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        with open(marker_failed(tag), 'w') as f:
            f.write('rollback requested\n')
    except Exception:
        pass


def _bat_text(plan, installer_path):
    app = app_root()
    bak = backup_dir(plan.tag)
    cfg = paths.CONFIG_DIR
    exe = os.path.join(app, 'simple-bot-trader.exe')
    return f'''@echo off
setlocal
start /wait "" "{installer_path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /SP-
start "" "{exe}" --if-was-launched --update-verify={plan.tag}
set /a n=0
:waitok
if exist "{marker_ok(plan.tag)}" goto ok
timeout /t 2 /nobreak >nul
set /a n+=1
if %n% lss 45 goto waitok
echo.Update failed - restoring backup
if exist "{bak}" xcopy /e /h /k /y "{bak}\\*.*" "{app}\\" >nul 2>&1
echo.rollback > "{cfg}\\update_failed_{plan.tag}.mrk"
start "" "{exe}" --if-was-launched
goto done
:ok
if exist "{bak}" rmdir /s /q "{bak}"
:done
endlocal
'''


def _sh_text(plan, installer_path, os_type):
    app = app_root()
    bak = backup_dir(plan.tag)
    cfg = paths.CONFIG_DIR
    if os_type == 'linux':
        install = f'pkexec dpkg -i "{installer_path}"'
        wrapper = os.path.join(app, 'simple-bot-trader.sh')
        restore = (f'if [ -d "{bak}" ]; then '
                   f'pkexec sh -c \'rm -rf "{app}" && cp -a "{bak}" "{app}"\'; '
                   f'fi')
    else:  # macos
        mnt = '/tmp/sbt_mnt'
        install = (f'hdiutil attach "{installer_path}" -nobrowse '
                   f'-mountpoint "{mnt}" >/dev/null 2>&1 && '
                   f'ditto "{mnt}/Simple Bot Trader.app" '
                   f'"/Applications/Simple Bot Trader.app" && '
                   f'hdiutil detach "{mnt}" >/dev/null 2>&1')
        wrapper = ('open -a "Simple Bot Trader"')
        restore = (f'if [ -d "{bak}" ]; then '
                   f'rm -rf "/Applications/Simple Bot Trader.app" && '
                   f'ditto "{bak}"'
                   f'/SimpleBotTrader.app "/Applications/Simple Bot Trader.app"; fi')
    relaunch = f'{wrapper} --if-was-launched --update-verify={plan.tag}'
    return f'''#!/usr/bin/env bash
{install}
{relaunch} &
for i in $(seq 1 45); do
  [ -f "{marker_ok(plan.tag)}" ] && {{ rm -rf "{bak}"; exit 0; }}
  sleep 2
done
{restore}
touch "{cfg}/update_failed_{plan.tag}.mrk"
{relaunch} &
exit 0
'''


def write_watcher(plan, installer_path):
    """Write the platform watcher script and return its path."""
    if plan.kind == 'windows':
        p = os.path.join(tempfile.gettempdir(),
                         f'sbt_upd_{plan.tag}.bat')
        with open(p, 'w') as f:
            f.write(_bat_text(plan, installer_path))
    else:
        p = os.path.join(tempfile.gettempdir(), f'sbt_upd_{plan.tag}.sh')
        with open(p, 'w') as f:
            f.write(_sh_text(plan, installer_path, plan.kind))
        os.chmod(p, 0o700)
    return p


def launch_watcher(script_path):
    """Detach the watcher and return True (the caller then quits)."""
    try:
        if os.name == 'nt':
            subprocess.Popen(['cmd', '/c', script_path], close_fds=True,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            subprocess.Popen(['/bin/bash', script_path], start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def run(tag, urlopen=None):
    """Top-level: validate plan, download+verify installer, snapshot backup,
    write+detach watcher. Returns (True, plan) when the callers must now quit
    so the watcher/installer can replace the running app; (False, plan) with
    plan.reason on refusal."""
    plan = build_plan(tag, urlopen)
    if not plan.ok:
        return False, plan
    installer = download_verified(plan, urlopen)
    if not installer:
        return False, Plan(plan.tag, plan.kind, plan.asset_url, '',
                           plan.body, 'downloaded installer failed sha256 check')
    if not snapshot_backup(plan.tag):
        return False, Plan(plan.tag, plan.kind, plan.asset_url, plan.sha256,
                           plan.body, 'could not snapshot current version')
    script = write_watcher(plan, installer)
    if not launch_watcher(script):
        return False, plan
    return True, plan