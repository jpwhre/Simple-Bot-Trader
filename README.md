# Simple Bot Trader

A **never-take-a-loss** crypto trading bot: dip-buy → trailing-stop → DCA exit,
with a fee-adjusted no-loss floor. Exchange API is the source of truth for cost
basis, balances and fees (no DB-driven guessing). Runs on Linux, macOS and
Windows as a small desktop app.

## Features

- **Dip-buy with fee-aware bounce** — buys on a genuine dip below a fee-adjusted
  floor, never chases a bounce that the taker fee makes unreachable.
- **Trailing-stop no-loss exit** — sells only above `entry + fee + trailing% +
  $0.05`; DCA re-buys on dips while holding and stops at the (fee-adjusted) entry.
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

- **`Could not load the Qt platform plugin "xcb"`** — the Qt GUI is missing a
  system library (most often `libxcb-cursor0` on Qt 5.15). On Ubuntu/Mint:

  ```bash
  sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0
  ```

  `setup.sh` detects and offers this automatically during install.

## License

See `EULA.txt` shipped with the app.