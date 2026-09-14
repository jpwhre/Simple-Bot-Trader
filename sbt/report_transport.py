"""Report transport — OPT-IN GitHub upload of crash/error reports.

DESIGN (user decisions 2026-08-09):
  - **Option A**: the app carries a fine-grained GitHub PAT scoped to
    `Issues: Write` on ONLY the dedicated reports repo. Zero hosted infra;
    works on Linux/Windows/macOS.
  - Reports go to a **dedicated (private) repo** (`simple-bot-trader-bugs`) so
    bug spam never touches the main repo and the token is scoped to just that repo.
  - **Opt-in, default OFF** (privacy-first). When on, reports auto-send with a
    log the user can review; the report file also stays locally.
  - Nothing is sent until `REPORTS_TOKEN` is configured (**ship time** — there
    is no repo yet on the first build). The dev can set `SBT_REPORTS_TOKEN` in
    the environment for local testing.
  - Sending never blocks the app (daemon thread), is throttled per phase
    (6h window, shared with the engine), and on failure the report is kept in a
    small pending queue retried at the next launch.

The token is a BUILD-TIME constant — never stored in settings.json (that file
is user-owned config). Rotate per release if it is ever abused.
"""
import json
import os
import platform
import threading
import time
import urllib.error
import urllib.request

from . import paths

# ==== SHIP-TIME CONFIG (set when the reports repo + token exist) ====
REPORTS_OWNER = 'jpwhre'
REPORTS_REPO = 'simple-bot-trader-bugs'
# Fine-grained PAT scoped to Issues: Write on REPORTS_OWNER/REPORTS_REPO ONLY.
# Empty = transport disabled (first build has no repo yet).
REPORTS_TOKEN = os.environ.get('SBT_REPORTS_TOKEN', '')

_PENDING_FILE = os.path.join(paths.CONFIG_DIR, 'pending_reports.json')
_SEND_WINDOW_S = 6 * 3600   # at most one send per phase per 6h (per launch)

# in-memory throttle bookkeeping for the current process
_last_send = {}


def is_configured():
    return bool(REPORTS_TOKEN) and bool(REPORTS_OWNER) and bool(REPORTS_REPO)


def version():
    try:
        from . import __version__
        return str(__version__)
    except Exception:
        return 'dev'


def build_payload(phase, detail, count, report_path, settings=None):
    """Scrubbed issue title + body + Ed25519 signature.  PRIVACY (user,
    2026-08-09): the SENT report is OS + reason + version + chain integrity
    ONLY — no transaction details, no language.  The full report file (with
    the diagnostic tail) stays LOCAL in crash_reports/ for the user to review;
    it is never included in what is sent."""
    title = f'[bot error] {phase}'
    # chain integrity status (HMAC tamper detection)
    try:
        from . import engine
        chain_status = engine._log_chain_integrity()
    except Exception:
        chain_status = 'LOG CHAIN: UNKNOWN'
    body = [
        f'**App version:** {version()}',
        f'**OS:** {platform.system()} {platform.release()} ({platform.machine()})',
        f'**Phase:** {phase}',
        f'**Detail:** {detail}',
        f'**Occurrence:** #{count}',
        f'**Chain:** {chain_status}',
        '',
        '[Full report with the diagnostic log is kept locally in '
        'crash_reports/ — the sent report is OS + reason + version only.]',
    ]
    text = '\n'.join(body)
    if settings:
        text = _redact(text, settings)
    # Ed25519 signature (unsigned in dev builds where SIGNING_PUBLIC_KEY is empty)
    try:
        from . import report_signing
        sig = report_signing.sign_report(text)
    except Exception:
        sig = ''
    return title, text, sig


def _redact(text, settings):
    """Safety net: never leak key material into a report body."""
    for k in ('api_key_name', 'api_key', 'private_key', 'private_key_pem',
              'secret', 'password', 'ccxt_api_key', 'ccxt_secret',
              'ccxt_password'):
        v = settings.get(k)
        if isinstance(v, str) and v:
            text = text.replace(v, '[redacted]')
    return text


def send_report_to_github(title, body, token=None, owner=None, repo=None,
                          signature=''):
    """POST an issue to the reports repo.  Returns True on created/ok.
    Pure stdlib (urllib) — no new dependency for the shipped app.
    When `signature` is non-empty, appends SBT-REPORT-SIGNATURE to the body.
    E2E encryption: body is RSA-4096 + AES-256-GCM encrypted before sending
    (GitHub sees ciphertext only — dev's private key decrypts)."""
    token = REPORTS_TOKEN if token is None else token
    owner = REPORTS_OWNER if owner is None else owner
    repo = REPORTS_REPO if repo is None else repo
    if not token or not owner or not repo:
        return False
    if signature:
        body = f'{body}\n\nSBT-REPORT-SIGNATURE: {signature}'
    # E2E encryption: encrypt body before sending
    try:
        from . import e2e
        encrypted = e2e.encrypt_report_json(body)
        if encrypted:
            body = encrypted
    except Exception:
        pass  # graceful degradation — send unencrypted if encryption fails
    url = f'https://api.github.com/repos/{owner}/{repo}/issues'
    payload = json.dumps({'title': title, 'body': body}).encode('utf-8')
    req = urllib.request.Request(
        url, data=payload, method='POST', headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json',
        })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


# ---- pending queue (survives a restart: failures retry next launch) --------

def enqueue_pending(path, phase, detail, count):
    """Record a report that still needs sending. Called BEFORE the send attempt
    so a crash or network failure never loses it."""
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        pending = _read_pending()
        if not any(p.get('path') == path for p in pending):
            pending.append({'path': path, 'phase': phase,
                            'detail': detail, 'count': count})
            _write_pending(pending)
    except Exception:
        pass


def _read_pending():
    try:
        if os.path.exists(_PENDING_FILE):
            with open(_PENDING_FILE) as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _write_pending(pending):
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        with open(_PENDING_FILE, 'w') as f:
            json.dump(pending, f)
        paths.secure_file(_PENDING_FILE)
    except Exception:
        pass


def _clear_pending(path):
    pending = [p for p in _read_pending() if p.get('path') != path]
    _write_pending(pending)


def send_pending_reports(settings=None, send=send_report_to_github):
    """Send every queued report (called at startup). Throttled per phase.
    `send` is injectable for tests. Never blocks the caller."""
    if not is_configured():
        return
    for item in _read_pending():
        path = item.get('path', '')
        phase = item.get('phase', 'unknown')
        if not path or not os.path.exists(path):
            _clear_pending(path)
            continue
        now = time.time()
        if now - _last_send.get(phase, 0.0) < _SEND_WINDOW_S:
            continue
        title, body, sig = build_payload(phase, item.get('detail', ''),
                                         item.get('count', 1), path, settings)
        try:
            if send(title, body, signature=sig):
                _last_send[phase] = now
                _clear_pending(path)
        except Exception:
            pass  # keep pending for the next launch


def send_in_background(path, phase, detail, count, settings=None):
    """Queue + attempt the send on a daemon thread (never blocks the bot)."""
    enqueue_pending(path, phase, detail, count)
    if not is_configured():
        return
    threading.Thread(target=_worker,
                     args=(path, phase, detail, count, settings),
                     daemon=True).start()


def _worker(path, phase, detail, count, settings):
    try:
        now = time.time()
        if now - _last_send.get(phase, 0.0) < _SEND_WINDOW_S:
            return  # throttled this launch (queue entry stays for later)
        title, body, sig = build_payload(phase, detail, count, path, settings)
        if send_report_to_github(title, body, signature=sig):
            _last_send[phase] = now
            _clear_pending(path)
        # on failure the queue entry stays -> retried at the next launch
    except Exception:
        pass
