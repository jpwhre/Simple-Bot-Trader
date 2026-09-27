# Simple Bot Trader

A **never-take-a-loss** crypto trading bot: dip-buy → trailing-stop → DCA exit,
with a fee-adjusted no-loss floor. **Global by design:** multi-exchange
(Coinbase Advanced Trade + any CCXT-supported exchange) and multi-language —
the UI auto-detects your OS/keyboard language and translates itself. Exchange
API is the source of truth for cost basis, balances and fees (no DB-driven
guessing). Runs on Linux, macOS and Windows as a small desktop app.

## Features

- **Dip-buy with fee-aware bounce** — buys on a genuine dip below a fee-adjusted
  floor, never chases a bounce that the taker fee makes unreachable.
- **Take-profit + trailing-stop exit** — holds below a take-profit mark (`entry +
  take-profit %`); once reached, a trailing stop arms at the peak and closes the
  trade on a pullback — never below the fee-adjusted entry (no loss). DCA keeps
  re-buying dips while holding and stops at the (fee-adjusted) entry.
- **Multi-exchange** — add a native **Coinbase Advanced Trade** key *or* a
  **CCXT** key for any exchange CCXT supports (Kraken, Binance, …) — one
  engine, one UI.
- **Real-time vs polled feeds** — Coinbase uses a native **websocket** (live
  spot). All other exchanges run through CCXT's REST adapter, which polls the
  feed at a **2.5 s** interval; a faster/streaming CCXT feed is not yet
  implemented.
- **Auto-translation** — the UI reads your **OS/keyboard locale** and switches
  languages on the fly (en, es, pt, fr, de, zh; English fallback). Detection
  is fully local — nothing is sent to any detection service.
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

**Native installers (in development):** Windows builds a `setup.exe` (Inno
Setup — desktop icon + Start Menu + built-in uninstaller) and macOS an `.app`
+ dmg on every release tag via GitHub Actions; Linux uses a `tools/build_deb.sh`
`.deb` (`dpkg -r simple-bot-trader` to uninstall). Updates for the native
installs are installer-driven: download the new signed installer and run it.

## Autostart & return-to-state

- **Return-to-state:** the installer enables OS autostart (Linux systemd, macOS
  LaunchAgent, Windows Startup folder). After a reboot, the bot reopens itself
  **if it was running when the machine went down** (it never pops open a bot you
  had closed). These autostart hooks run at **login**; Linux/macOS boot-time
  relaunch additionally needs a desktop session with **auto-login** (common on a
  dedicated bot box). A power loss mid-run is fine — the app resumes from the
  exchange (API cost basis).
- **API key permission check:** at Add API, keys are checked live where the
  exchange exposes it — Coinbase (`GET /key_permissions`) and Binance /
  Binance.US (`sapi/v1/account/apiRestrictions`). Keys that can **initiate
  transfer / withdrawal of funds** are **rejected**, and keys that can't trade
  are rejected. Exchanges with no permission endpoint (Kraken, OKX, Bybit, …)
  can't be verified by any API — the app warns (never use a key that can move
  funds) and lets you proceed.

## Updates

- **In-app:** the bot checks GitHub for new signed releases (opt-out enabled for
  auto-install; update offering is signed and verified before install).
- **CLI:** re-run the one-liner above.
- **Git:** `git -C ~/Simple-Bot-Trader pull` — only if you installed from the
  repo rather than a signed release.

## Bug reports

Reports are scrubbed (OS + reason + version only, nothing else), **encrypted
end-to-end and tamper-evident**, and open in GitHub Issues of the **private**
report repository — neither GitHub nor anyone with access to that repo can
read or alter a payload. No details are sent without your review.

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
  feed is public, but balances need a *signed* API call; the two use separate
  paths, so a connected feed does not confirm the key is valid. A zero balance
  usually means the stored API key is invalid or its permissions don't allow
  reading balances.
  - **Coinbase:** the app validates keys when you save and shows "API key
    error — re-add". Fix: **Settings → Add API → Load from file…** and pick the
    downloaded `cdp_api_key_<name>.json` — the name and key fill in
    automatically. Both Coinbase key types are supported: ECDSA (PEM) and
    Ed25519 (the newer bare base64 download).
  - **CCXT exchanges (Kraken, Binance, …):** check that the exchange id is
    spelled exactly as CCXT expects (an unknown id is rejected at Add API),
    that the key has **read + trade** permissions (never one that can withdraw
    funds), and remember the balance line runs on the **2.5 s REST poll** —
    give it one or two poll cycles to appear.

## Security

Vulnerability reports are handled privately — see
[`SECURITY.md`](SECURITY.md) for the reporting policy and the private bug
inbox. Do not open public issues containing sensitive data.

## License

See `EULA.txt` shipped with the app.