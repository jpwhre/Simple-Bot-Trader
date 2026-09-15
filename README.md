# Simple Bot Trader

A **never-take-a-loss** crypto trading bot: dip-buy → trailing-stop → DCA exit,
with a fee-adjusted no-loss floor. Exchange API is the source of truth for cost
basis, balances and fees (no DB-driven guessing). Runs on Linux, macOS and
Windows as a small desktop app.

## Features

- **Dip-buy with fee-aware bounce** — buys on a genuine dip below a fee-adjusted
  floor, never chases a bounce that the taker fee makes unreachable.
- **Take-profit + trailing-stop exit** — holds below a take-profit mark (`entry +
  take-profit %`); once reached, a trailing stop arms at the peak and closes the
  trade on a pullback — never below the fee-adjusted entry (no loss). DCA keeps
  re-buying dips while holding and stops at the (fee-adjusted) entry.
- **Multi-exchange** — Coinbase Advanced Trade for live trading; any CCXT
  exchange (Kraken, Binance, …) via the same engine.
- **API-driven** — true cost basis, balances and minimums come from the exchange.
- **Privacy by design** — zero data collection; the only outbound calls are to
  the exchange API, GitHub releases (update checks) and opt-in crash reports.

## Install (Linux / macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/jpwhre/Simple-Bot-Trader/main/install.sh | bash
```

Installs to `~/Simple-Bot-Trader`: verifies the release's **sha256 + Ed25519
signature**, unpacks, creates a virtualenv, pins dependencies and adds a
desktop launcher. Re-run the same command any time to update to the latest
signed release, or pin a version:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/jpwhre/Simple-Bot-Trader/main/install.sh) --tag v0.0.1 --dir ~/bot
```

**What the signature does:** every release is signed with the developer's
Ed25519 key. A tampered download, a MITM, or a compromised publisher's release
asset without the key cannot install. Only the public key ships; the private
key never leaves the developer machine. (The one thing a signature cannot do is
protect the *install.*sh bootstrap itself — `curl | bash` always means "trust
that URL." Review it once: it is short, stable, and changes rarely.)

## Manual install

Downloads → Releases → `simple-bot-trader-<tag>.zip`:

- **Linux / macOS:** extract, then run `bash setup.sh` and `./run.sh`.
- **Windows:** extract, then run `setup.bat`.

## Run

```bash
cd ~/Simple-Bot-Trader
./run.sh                      # live bot (or launch from the desktop icon)
./run.sh --config-dir ~/other-bot   # isolated profile = separate exchange
```

Requirements: Python 3.9+ (current released Python, including 3.14, is supported).

**Why a virtual environment?** The installer builds a private `venv/` folder next
to the app. Modern Linux/macOS block `pip install` to the system Python
(PEP-668 "externally managed"), and the bot pins exact dependency versions that
must not collide with other software. `run.sh` uses the venv automatically —
there is nothing extra to activate.

## Autostart & the "remote" lock

- **Return-to-state:** the installer enables OS autostart (Linux systemd, macOS
  LaunchAgent, Windows Startup folder). After a reboot, the bot reopens itself
  **if it was running when the machine went down** (it never pops open a bot you
  had closed). These autostart hooks run at **login**; Linux/macOS boot-time
  relaunch additionally needs a desktop session with **auto-login** (common on a
  dedicated bot box). A power loss mid-run is fine — the app resumes from the
  exchange (API cost basis).
- **Remote-desktop lock:** the bot refuses to trade when it detects it was
  started from a remote session (SSH/X11, RDP, VNC…) — a safety gate against a
  hijacked or unlocked remote session. When launched normally in a **local**
  session (or by the autostart hooks above), it trades freely and the gate stays
  intact for later remote access. `SBT_ALLOW_REMOTE=1` disables this gate — only
  set it on a machine you fully trust, and understand it then also protects you
  from nothing when someone else controls that box remotely.
- **A virtual environment is NOT a "remote".** The venv is just an isolated
  dependencies folder on disk; it has no effect on the remote-detection/blocking
  logic.

## Updates

- **In-app:** the bot checks GitHub for new signed releases (opt-out enabled for
  auto-install; update offering is signed and verified before install).
- **CLI:** re-run the one-liner above.
- **Git:** `git -C ~/Simple-Bot-Trader pull` — only if you installed from the
  repo rather than a signed release.

## Bug reports

Reports are scrubbed (OS + reason + version only, nothing else) and open in
GitHub Issues of the **private** report repository. No details are sent without
your review.

## Troubleshooting

- **`getcwd: cannot access parent directories`** at install — your terminal was
  parked in a folder that was deleted before you ran the command. Harmless (the
  installer uses absolute paths); `cd ~` clears it if the message bothers you.

- **`Could not load the Qt platform plugin "xcb"`** — the Qt GUI is missing a
  system library (most often `libxcb-cursor0` on Qt 5.15). On Ubuntu/Mint:

  ```bash
  sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0
  ```

  `setup.sh` detects and offers this automatically during install.

- **Account Balance shows 0 (with the price feed "connected")** — the price
  feed is public, but balances need a *signed* API call. A zero balance with a
  connected websocket usually means the stored Coinbase key is invalid (the
  app now validates keys when you save, and shows "API key error — re-add").
  Fix: **Settings → Add API → Load from file…** and pick the downloaded
  `cdp_api_key_<name>.json` — the name and key fill in automatically. Both
  Coinbase key types are supported: ECDSA (PEM) and Ed25519 (the newer bare
  base64 download).

## License

See `EULA.txt` shipped with the app.