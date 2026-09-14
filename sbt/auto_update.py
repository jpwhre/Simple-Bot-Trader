"""Auto-update — check, install (opt-in / heads-up), relaunch, return to state.

DESIGN (user decisions 2026-08-09):
  - Runs ONLY while the app is open — NO background process, ever.
  - Mid-trade updates are FINE: return-to-state restores started/stopped +
    position on the relaunch (API cost basis).
  - Jump to the NEWEST release (semver; GitHub `releases/latest` excludes
    pre-releases). Metadata-only outbound to GitHub — the approved privacy
    exception (no user data).
  - Default = CHECK + NOTIFY ONLY (`auto_update_install` OFF). Turning install
    on (or clicking "Install now") does AUTO-INSTALL WITH A HEADS-UP dialog.
  - AUTO ROLLBACK: the previous version is backed up before applying. If the
    new version never takes effect (startup version mismatch) or crashes at
    startup, the app restores the backup and — with the opt-in report
    transport on — files a report so the developer knows.

Version lives in one canonical place: `sbt/__init__.py __version__`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

from . import paths

# ==== SHIP-TIME CONFIG (the main repo + release assets) ====
UPDATES_OWNER = 'jpwhre'
UPDATES_REPO = 'simple-bot-trader'
# The release must ship an asset named <ASSET_PREFIX><tag>.zip (files at the
# zip root: main.py, sbt/, run.sh). Built by the ship process.
ASSET_PREFIX = 'simple-bot-trader-'

_STATE_FILE = os.path.join(paths.CONFIG_DIR, 'update_state.json')
_CHECK_INTERVAL_S = 4 * 3600   # check at launch + every 4h while the app is open
_HEADS_UP_S = 10               # heads-up countdown before auto-install


def app_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def current_version():
    try:
        from . import __version__
        return str(__version__)
    except Exception:
        return '0.0.0'


# ==== SIGNED UPDATES (user, 2026-08-16: "How to force signed updates?") ====
# Ship-time: embed the Ed25519 PUBLIC key hex here (generated + kept ONLY on
# the dev machine by tools/sign_release.py). When non-empty, a release MUST
# carry `SBT-DIGEST:` + `SBT-SIGNATURE:` in its notes, signed by the dev's
# private key — a compromised GitHub account cannot forge a signature, so it
# cannot push a malicious update even though it can upload assets and GitHub
# will happily compute a matching digest for them. Empty (dev build, pre-ship)
# = fall back to GitHub's asset digest (today's behavior) so update flows stay
# testable before the key exists.
SIGNING_PUBLIC_KEY = '7debae06d485554131770feefffc9b706dcf2e8f609f68444656e653f4e759b5'


def verify_release_signature(digest_hex, signature_hex, public_key_hex=None):
    """True when `signature_hex` is a valid Ed25519 signature of
    `digest_hex` by the embedded (or given) public key. Public for tests and
    the dev-side signing tool round-trip."""
    pk_hex = (public_key_hex or SIGNING_PUBLIC_KEY or '').strip()
    if not pk_hex or not digest_hex or not signature_hex:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pk_hex))
        pk.verify(bytes.fromhex(signature_hex.strip()),
                  digest_hex.strip().lower().encode('ascii'))
        return True
    except Exception:
        return False


def _parse_signed_digest(body):
    """'SBT-DIGEST: <hex>' and 'SBT-SIGNATURE: <hex>' from release notes.
    Returns (digest, signature) or (None, None)."""
    digest = signature = None
    for line in (body or '').splitlines():
        s = line.strip()
        if s.upper().startswith('SBT-DIGEST:'):
            digest = s.split(':', 1)[1].strip().lower() or None
        elif s.upper().startswith('SBT-SIGNATURE:'):
            signature = s.split(':', 1)[1].strip() or None
    return digest, signature


def is_security_release(body):
    """True when the release notes carry 'SBT-SECURITY: 1' (accepts
    1/true/yes, case-insensitive). SECURITY releases FORCE-INSTALL on every
    user regardless of update settings (user, 2026-08-16: 'temp patches need
    to force install — a fix that merely MIGHT get installed leaves users
    vulnerable')."""
    for line in (body or '').splitlines():
        s = line.strip()
        if s.upper().startswith('SBT-SECURITY:'):
            return s.split(':', 1)[1].strip().lower() in ('1', 'true', 'yes')
    return False


def install_is_mandatory(security, auto_install_setting):
    """The install decision: a SECURITY release is ALWAYS mandatory; a normal
    release installs automatically only when the user opted in."""
    return bool(security) or bool(auto_install_setting)


def asset_url_from(data):
    """The <ASSET_PREFIX>*.zip asset's download URL from a release API dict
    (or None when the release has no asset)."""
    for a in (data.get('assets') or []):
        name = a.get('name', '')
        if name.startswith(ASSET_PREFIX) and name.endswith('.zip'):
            return (a.get('browser_download_url') or a.get('url'))
    return None


def parse_semver(v):
    """'v1.2.3' / '1.2.3' -> (1,2,3); invalid -> None."""
    try:
        v = str(v).strip().lstrip('vV')
        parts = v.split('.')
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0,
                int(parts[2]) if len(parts) > 2 else 0)
    except Exception:
        return None


def is_newer(available, current):
    a, c = parse_semver(available), parse_semver(current)
    return bool(a and c and a > c)


def check_for_update(urlopen=None):
    """Return (tag, asset_url, digest, security) of the newest release, or
    (None, None, None, False). `digest` is the (signed or GitHub) sha256 for
    the asset; `security` is True for SBT-SECURITY releases, which
    FORCE-INSTALL regardless of user settings. Metadata-only; `urlopen`
    injectable for tests."""
    if not (UPDATES_OWNER and UPDATES_REPO):
        return None, None, None, False
    url = (f'https://api.github.com/repos/{UPDATES_OWNER}/{UPDATES_REPO}'
           f'/releases/latest')
    try:
        req = urllib.request.Request(url, headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': f'SimpleBotTrader/{current_version()}',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        uo = urlopen or urllib.request.urlopen
        with uo(req, timeout=15) as resp:
            if resp.status != 200:
                return None, None, None, False
            data = json.loads(resp.read().decode('utf-8', 'ignore'))
        tag = (data.get('tag_name') or '').lstrip('v')
        if not tag:
            return None, None, None, False
        # SIGNED UPDATES: when the ship build embeds a public key, the
        # release notes MUST carry a dev-signed digest (fail closed — a
        # missing/invalid signature means NO update is offered, so a
        # compromised GitHub cannot push code to shipped users).
        body = data.get('body') or ''
        sig_digest, signature = _parse_signed_digest(body)
        security = is_security_release(body)
        if SIGNING_PUBLIC_KEY:
            if not verify_release_signature(sig_digest, signature):
                return None, None, None, False
            return (tag, asset_url_from(data), sig_digest, security)
        digest = None
        for a in (data.get('assets') or []):
            name = a.get('name', '')
            if name.startswith(ASSET_PREFIX) and name.endswith('.zip'):
                # GitHub returns 'digest' as 'sha256:<hex>'; keep just the hex
                d = (a.get('digest') or '')
                if d.startswith('sha256:'):
                    digest = d[len('sha256:'):].strip().lower()
                break
        return (tag, asset_url_from(data), digest, security)
    except Exception:
        return None, None, None, False


# Max size for a release asset (bytes) — a huge/malicious asset must not
# exhaust memory or disk.
MAX_ASSET_BYTES = 100 * 1024 * 1024


def download_asset(asset_url, urlopen=None, expected_sha256=None):
    """Download the release zip to a temp file (capped at MAX_ASSET_BYTES).
    When `expected_sha256` is given, the download is verified against it and
    discarded on mismatch (a tampered / MITM'd / corrupt asset never installs).
    Returns the temp path or None."""
    try:
        uo = urlopen or urllib.request.urlopen
        with uo(asset_url, timeout=60) as resp:
            data = resp.read(MAX_ASSET_BYTES + 1)
            if len(data) > MAX_ASSET_BYTES:
                return None
        if expected_sha256:
            import hashlib
            actual = hashlib.sha256(data).hexdigest()
            if actual.lower() != expected_sha256.lower():
                return None
        fd, path = tempfile.mkstemp(prefix='sbt_update_', suffix='.zip')
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        return path
    except Exception:
        return None


def _clear_pycache(root):
    for dirpath, dirnames, _ in os.walk(root):
        for d in list(dirnames):
            if d == '__pycache__':
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)


def extract_over(zip_path, target):
    """Extract a release zip over `target` (code files replaced; unrelated user
    files in the app dir untouched). Clears __pycache__ so stale bytecode never
    shadows the new source. Returns True on success.

    SAFE EXTRACTION (no zip-slip): NEVER uses `extractall` — a malicious or
    malformed release member like `../../x` could write outside the target.
    Absolute paths and any `..` traversal are rejected outright."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                name = (info.filename or '').replace('\\', '/')
                # zip-slip guards: reject absolute paths, drive letters, and
                # any parent traversal
                if name.startswith('/') or (len(name) >= 2 and name[1] == ':'):
                    return False
                if '..' in name.split('/'):
                    return False
                dest = os.path.join(target, *name.split('/'))
                if info.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, 'wb') as out:
                    shutil.copyfileobj(src, out)
        _clear_pycache(target)
        return True
    except Exception:
        return False


# ---- backup / rollback -----------------------------------------------------

def backup_path(app_dir_=None):
    return (app_dir_ or app_dir()) + '.bak'


def backup_app(app_dir_=None):
    src = app_dir_ or app_dir()
    dst = src + '.bak'
    try:
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(
            src, dst,
            ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc',
                                          'tests', 'PLAN.md', 'bugs.md',
                                          'AGENTS.md'))
        return True
    except Exception:
        return False


def restore_backup(app_dir_=None):
    """Copy the backup over the app dir (merge, never delete unrelated user
    files). Returns True on success."""
    src = (app_dir_ or app_dir()) + '.bak'
    dst = app_dir_ or app_dir()
    try:
        if not os.path.isdir(src):
            return False
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target = dst if rel == '.' else os.path.join(dst, rel)
            os.makedirs(target, exist_ok=True)
            for fn in files:
                shutil.copy2(os.path.join(root, fn), os.path.join(target, fn))
        _clear_pycache(dst)
        return True
    except Exception:
        return False


# ---- update state (survives the relaunch + crash) --------------------------

def _read_state():
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _write_state(state):
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        with open(_STATE_FILE, 'w') as f:
            json.dump(state, f)
        paths.secure_file(_STATE_FILE)
    except Exception:
        pass


def mark_update_applied(new_version):
    _write_state({'applied': new_version, 'previous': current_version(),
                  'ts': int(time.time()), 'status': 'pending'})


def verify_after_launch():
    """Called at startup. If an update was applied but never verified:
      - running the NEW version -> mark ok, drop the backup
      - not running the new version -> roll back to the backup
    Returns 'ok' | 'rolled_back' | 'rollback_failed' | 'none'."""
    st = _read_state()
    if st.get('status') != 'pending':
        return 'none'
    if current_version() == st.get('applied'):
        _write_state(dict(st, status='ok'))
        try:
            shutil.rmtree(backup_path(), ignore_errors=True)
        except Exception:
            pass
        return 'ok'
    restored = restore_backup()
    _write_state(dict(st, status='rolled_back'))
    return 'rolled_back' if restored else 'rollback_failed'


def _load_settings_safe():
    try:
        from .config import load_settings
        return load_settings()
    except Exception:
        return {}


def _write_rollback_report(st, restored):
    try:
        d = paths.CRASH_DIR
        os.makedirs(d, exist_ok=True)
        fn = os.path.join(d, f"update_{time.strftime('%Y%m%d_%H%M%S')}_crash.log")
        with open(fn, 'w') as f:
            f.write('update crashed at startup\n')
            f.write(f'from: {st.get("previous")}\n')
            f.write(f'to:   {st.get("applied")}\n')
            f.write(f'rollback: {"restored" if restored else "FAILED"}\n')
        return fn
    except Exception:
        return ''


def maybe_rollback_on_crash():
    """Called from the crash hook. If a pending update's NEW version just
    crashed, restore the backup so the next launch runs the previous version,
    and (opt-in) report the failure so the developer knows. Returns True if it
    rolled back."""
    st = _read_state()
    if st.get('status') != 'pending':
        return False
    if current_version() != st.get('applied'):
        return False  # not the post-update build
    restored = restore_backup()
    _write_state(dict(st, status='rolled_back', reason='startup_crash'))
    try:
        from . import report_transport
        s = _load_settings_safe()
        if s.get('report_opt_in', False):
            report_transport.send_in_background(
                _write_rollback_report(st, restored), 'update',
                f'update to v{st.get("applied")} crashed at startup — '
                f'{"rolled back to v" + str(st.get("previous")) if restored else "rollback FAILED"}',
                1, s)
    except Exception:
        pass
    return restored


# ---- install + relaunch ----------------------------------------------------

def install_update(tag, asset_url, urlopen=None, expected_sha256=None):
    """Download, back up, extract, mark pending. Returns True -> relaunch.
    The asset is integrity-checked against `expected_sha256` when provided."""
    if not asset_url:
        return False
    zip_path = download_asset(asset_url, urlopen, expected_sha256)
    if not zip_path:
        return False
    try:
        backup_app()
        if not extract_over(zip_path, app_dir()):
            return False
        mark_update_applied(tag)
        return True
    finally:
        try:
            os.remove(zip_path)
        except Exception:
            pass


def relaunch():
    """Spawn a fresh instance (same profile args) and return True. The caller
    quits this instance. No background process persists — this is the update's
    hand-off to the reopened app, which returns to state."""
    try:
        main_py = os.path.join(app_dir(), 'main.py')
        args = [sys.executable, main_py] + _keep_args()
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        subprocess.Popen(args, **kwargs)
        return True
    except Exception:
        return False


def _keep_args():
    """Preserve --config-dir (profile); drop --if-was-launched (the relaunch
    is explicit, not a boot check)."""
    out, i, argv = [], 0, sys.argv[1:]
    while i < len(argv):
        a = argv[i]
        if a == '--if-was-launched':
            i += 1
            continue
        if a == '--config-dir':
            i += 1
            if i < len(argv):
                out += ['--config-dir', argv[i]]
            i += 1
            continue
        out.append(a)
        i += 1
    return out
