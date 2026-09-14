"""HMAC hash-chain for the events log — tamper detection.

Each log line carries an HMAC-SHA256 suffix derived from the previous hash,
the line content, and a per-user secret (the API key fingerprint). Tampering
with any chained line (edit, delete, insert) breaks the chain → the crash
report includes "LOG CHAIN: BROKEN at line N".

Design decisions (user 2026-08-18):
  - Secret = SHA256(exchange|cred) from the user's own API key (option 2).
  - Chain state persisted to config/chain_<logfile>.json (per-profile).
  - Chain disabled until an API key exists (no risk window at first-run).
  - The chain position (start line / prev hash / count) is derived from the
    LOG FILE itself at init, with per-line verification — self-healing after
    a crash between checkpoints, and honest about a key change (a chain made
    with a different secret is treated as pre-chain prefix, not "tampered").
  - Pre-chain lines (written before the feature existed, or while the chain
    was disabled) are skipped by verify() by design — they are NOT tamper
    evidence. Deleting one still breaks the chain transitively (the first
    chained line shifts and its hash no longer follows genesis).
  - Whole-file deletion cannot be detected in-app (the log lives in the OS
    temp dir, which legitimately clears on reboot); the state file records
    the last checkpoint count so init can flag a mid-file strip.
"""
import hashlib
import hmac
import json
import os
import re
import threading

_HMAC_RE = re.compile(r'\|hmac:([0-9a-f]{64})$')
# Rolling crash/error-report retention (user, 2026-08-20):
#  - DEV build (is_admin) NEVER deletes — the developer must always be able
#    to see every error that ever happened ("I can't see errors if they are
#    not shown to me").
#  - SHIP build: keep the newest 200. Rationale: error reports throttle to
#    1 per phase per 6h, so even a catastrophically broken bot produces
#    ~4/day/phase -> 200 reports ≈ 50 days of worst-case spam, at ~2-5 KB
#    each ≈ 1 MB total — invisible disk cost, months of diagnostics.
#  - The events LOG itself is not affected: it lives in the OS temp dir
#    (cleared by the OS on reboot) and the in-app log is per-cycle by design.
_MAX_CRASH_REPORTS = 200  # rolling crash/error report retention (ship builds)
_GENESIS = b'0' * 32     # 32 bytes of ASCII '0'


def _derive_secret(exchange, cred):
    """HMAC secret from the API key fingerprint.  Full 32-byte SHA256 (not
    the truncated 16-char lock fingerprint).  Returns hex string."""
    if not cred:
        return None
    raw = hashlib.sha256(f'{exchange}|{cred}'.encode()).digest()
    return raw.hex()


class LogChain:
    """Append-only HMAC chain for a single log file.

    Usage::

        chain = LogChain(log_path, secret_hex)
        chain.append('Bot started')           # writes line + |hmac:...
        ok, broken_at, total = chain.verify() # full replay
    """

    def __init__(self, log_path, secret_hex, state_dir=None):
        self.log_path = log_path
        self.secret = bytes.fromhex(secret_hex) if secret_hex else None
        self._state_dir = state_dir or os.path.dirname(log_path)
        # state file is per-log (multiple chains in same dir don't collide)
        _log_name = os.path.basename(log_path).replace('.', '_')
        self._state_path = os.path.join(self._state_dir,
                                        f'chain_{_log_name}.json')
        self._lock = threading.Lock()
        self._prev_hash = _GENESIS
        self._line_count = 0   # number of CHAINED lines in the file
        self._start_line = 0   # 0-based index of the first chained line
        self._stripped = 0     # chained lines missing vs the last checkpoint
        self._reset = False    # first chained line failed (key change/tamper)
        self._init_from_file()

    # -- init / state -----------------------------------------------------

    def _init_from_file(self):
        """Derive the chain position from the log itself (self-healing).

        Scans the file once, verifying each chained line against the current
        secret: the first VERIFIED chained line is the chain start, the last
        one supplies the next prev_hash. A first chained line that does not
        verify (chain made with a different API key) restarts the chain at
        end-of-file instead of crying tamper.
        """
        start = None
        prev = _GENESIS
        count = 0
        last_good = None
        total = 0
        try:
            if os.path.exists(self.log_path):
                with open(self.log_path) as f:
                    for i, raw in enumerate(f):
                        total += 1
                        m = _HMAC_RE.search(raw.rstrip('\n'))
                        if not m:
                            continue
                        if start is None:
                            start, prev = i, _GENESIS
                        content = raw[:m.start()]
                        expected = hmac.new(
                            self.secret, f'{prev.hex()}|{content}'.encode(),
                            hashlib.sha256).hexdigest()
                        if expected != m.group(1):
                            if count == 0:
                                # first chained line does not verify under
                                # this secret: either the API key changed
                                # (legit restart of the chain) or someone
                                # tampered with the first chained line.
                                # We cannot tell these apart in-app — say so
                                # honestly instead of claiming OK.
                                start = None
                                last_good = None
                                self._reset = True
                            break  # mid-chain tamper: trust up to last_good
                        count += 1
                        last_good = bytes.fromhex(m.group(1))
                        prev = last_good
        except Exception:
            start = None
            last_good = None
            count = 0
            total = 0
        if start is not None and last_good is not None:
            self._start_line = start
            self._line_count = count
            self._prev_hash = last_good
        else:
            self._start_line = total  # everything present is pre-chain
            self._line_count = 0
            self._prev_hash = _GENESIS
        # strip detection: the state file remembers the last checkpoint; if
        # the file still exists but holds FEWER chained lines than the
        # checkpoint recorded, lines were stripped/truncated out of it.
        try:
            if os.path.exists(self._state_path) and \
                    os.path.exists(self.log_path):
                with open(self._state_path) as f:
                    st = json.load(f)
                gone = int(st.get('line_count', 0)) - self._line_count
                if gone > 0:
                    self._stripped = gone
        except Exception:
            pass

    def _save_state(self):
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            with open(self._state_path, 'w') as f:
                json.dump({'prev_hash': self._prev_hash.hex(),
                           'line_count': self._line_count}, f)
        except Exception:
            pass

    # -- append --------------------------------------------------------------

    def _hmac(self, line):
        msg = f'{self._prev_hash.hex()}|{line}'.encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def append(self, line):
        """Append a log line.  With a secret the line carries an |hmac: suffix
        (the chain); without one it is written PLAIN (chain disabled — the
        line is never dropped)."""
        try:
            if not self.secret:
                with open(self.log_path, 'a') as f:
                    f.write(f'{line}\n')
                return line
            with self._lock:
                h = self._hmac(line)
                with open(self.log_path, 'a') as f:
                    f.write(f'{line}|hmac:{h}\n')
                self._prev_hash = bytes.fromhex(h)
                self._line_count += 1
                if self._line_count % 50 == 0:  # checkpoint every 50 lines
                    self._save_state()
        except Exception:
            pass  # logging must never raise into the engine
        return line

    def flush(self):
        """Persist chain state (called at shutdown)."""
        if self.secret:
            with self._lock:
                self._save_state()

    # -- verify ----------------------------------------------------------

    def verify(self):
        """Replay the log file and verify the HMAC chain.

        Lines before the chain start (pre-feature / disabled-chain lines) are
        skipped by design. Returns (ok, broken_at_line, total_lines);
        broken_at_line is 1-indexed; None when ok=True.
        """
        if not self.secret:
            return True, None, 0  # chain disabled — nothing to verify
        if not os.path.exists(self.log_path):
            return True, None, 0
        prev = _GENESIS
        line_no = 0
        with open(self.log_path) as f:
            for raw in f:
                line_no += 1
                if line_no - 1 < self._start_line:
                    continue  # pre-chain prefix
                raw = raw.rstrip('\n')
                m = _HMAC_RE.search(raw)
                if not m:
                    return False, line_no, line_no  # no HMAC = broken
                stored_h = m.group(1)
                content = raw[:m.start()]
                msg = f'{prev.hex()}|{content}'.encode()
                expected = hmac.new(self.secret, msg,
                                    hashlib.sha256).hexdigest()
                if not hmac.compare_digest(stored_h, expected):
                    return False, line_no, line_no
                prev = bytes.fromhex(stored_h)
        return True, None, line_no

    def integrity_report(self):
        """Human-readable integrity summary for crash reports."""
        if not self.secret:
            return 'LOG CHAIN: DISABLED (no API key)'
        if self._reset:
            return ('LOG CHAIN: RESET — first chained line does not verify '
                    'under this API key (key changed, or first chained line '
                    'tampered). Chain restarted at end of file.')
        ok, broken_at, total = self.verify()
        note = ''
        if self._stripped:
            note = (f' — WARNING: {self._stripped} chained lines missing vs '
                    'the last checkpoint (log stripped/truncated)')
        if ok:
            return (f'LOG CHAIN: OK (file {total} lines; '
                    f'{self._line_count} chained lines verified){note}')
        return f'LOG CHAIN: BROKEN at line {broken_at} of {total}{note}'


def cleanup_crash_reports(crash_dir, max_reports=_MAX_CRASH_REPORTS):
    """Keep at most `max_reports` crash/error log files, deleting the oldest.
    Called at startup and after writing a new report.  NEVER deletes on the
    dev/admin build (the developer keeps every report forever).  Only ever
    point this at a directory the app owns (CRASH_DIR) — never a shared
    temp dir."""
    try:
        try:
            from .admin import is_admin
            if is_admin():
                return  # dev machine: keep everything, always
        except Exception:
            pass
        if not os.path.isdir(crash_dir):
            return
        files = []
        for fn in os.listdir(crash_dir):
            if fn.startswith(('crash_', 'error_')) and fn.endswith('.log'):
                fp = os.path.join(crash_dir, fn)
                files.append((os.path.getmtime(fp), fp))
        if len(files) <= max_reports:
            return
        files.sort()  # oldest first
        for _, fp in files[:len(files) - max_reports]:
            try:
                os.remove(fp)
            except Exception:
                pass
    except Exception:
        pass
