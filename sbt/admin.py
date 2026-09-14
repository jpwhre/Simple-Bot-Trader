"""Dev/admin build marker.

The user runs a DEVELOPMENT build; the shipped build is the app only. Dev-only
features (the right-click widget inspector, the full in-app log) are enabled by
default when the app is running a DEV build and disabled in the shipped copy.

Detection is automatic: the dev build lives in a folder that carries dev-only
markers (tests/, PLAN.md, AGENTS.md, bugs.md) which the ship copy deliberately
excludes. Explicit override via SBT_ADMIN:
  - SBT_ADMIN=1  force admin features ON  (e.g. inspect a ship copy for debugging)
  - SBT_ADMIN=0  force admin features OFF (e.g. smoke-test the ship copy in-place)
  - unset        auto-detect from the app directory

Everything else (full on-disk log file, scrubbed opt-in reports) is identical
in both builds — this only changes what the developer sees in the UI.
"""
import os

ADMIN_ENV = 'SBT_ADMIN'

# Files/dirs that only ever exist in the DEV build, never in the ship copy.
_DEV_MARKERS = ('tests', 'PLAN.md', 'AGENTS.md', 'bugs.md')


def _app_dir():
    """The app root = the folder holding sbt/ (parent of this module)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_admin():
    """True when dev-only features should be enabled. Override via SBT_ADMIN;
    otherwise auto-detect by dev markers next to the app."""
    env = os.environ.get(ADMIN_ENV)
    if env == '1':
        return True
    if env == '0':
        return False
    base = _app_dir()
    try:
        for m in _DEV_MARKERS:
            if os.path.exists(os.path.join(base, m)):
                return True
    except Exception:
        pass
    return False
