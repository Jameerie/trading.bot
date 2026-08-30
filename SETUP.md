# Setup guide

Get the advisor running and reachable from whatever device you actually trade on.

**Requirement: Python 3.11 or newer. That is the whole list** — the app has no
third-party dependencies, so there is nothing to install and nothing to build.

---

## 1. Fastest possible start

```bash
git clone https://github.com/Jameerie/trading.bot
cd trading.bot
export PYTHONPATH=src

python3 -m trading_bot --config config/default.toml serve --open
```

Your browser opens on `http://127.0.0.1:8787`. That is the whole install.

Verify it works:

```bash
make test     # 390 tests, offline, ~25 seconds
make demo     # scan + backtest + calibrate on the bundled data
```

> The bundled data in `data/samples/` is **synthetic** — a seeded random walk so
> the demo runs offline. It is not a market. Replace it with your broker's real
> history before believing any number the tool prints (step 5).

---

## 2. Use it from your phone

Two options, depending on whether you need it outside your home network.

### Same wifi — 30 seconds, no accounts

```bash
# Generate a token first. Do not skip this.
export TRADING_BOT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

python3 -m trading_bot --config config/default.toml serve --host 0.0.0.0
```

The server prints both URLs on start:

```
On this machine   http://127.0.0.1:8787/?token=...
On your network   http://192.168.1.42:8787/?token=...
                  (open this on your phone, same wifi)
```

Open the network URL on your phone. The token is in the link, so there is
nothing to type.

### Anywhere — via a tunnel

Keep the server on loopback and let a tunnel handle the exposure. This avoids
opening a port on your router.

```bash
# Terminal 1
export TRADING_BOT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
echo "token: $TRADING_BOT_TOKEN"
python3 -m trading_bot --config config/default.toml serve

# Terminal 2 — pick whichever you have
cloudflared tunnel --url http://localhost:8787
# or:  ssh -R 80:localhost:8787 serveo.net
# or:  tailscale serve 8787          (private to your own devices - best option)
```

**Tailscale is the one to prefer.** It keeps the app on a private network
between your own devices, so it is never exposed to the public internet at all.

### Install it as an app

Once the page is open on your phone:

- **iOS Safari** — Share → *Add to Home Screen*
- **Android Chrome** — ⋮ → *Install app* / *Add to Home screen*

It then launches full-screen with its own icon, like a native app. The shell
works offline; scans do not, deliberately — a cached signal priced against a
market that has since moved is a wrong answer, not a degraded one.

---

## 3. Run it permanently

### Docker

```bash
docker compose up -d
open http://127.0.0.1:8787
```

Compose publishes to `127.0.0.1` only. To reach it from other devices, change
the mapping in `docker-compose.yml` to `"8787:8787"` and set `TRADING_BOT_TOKEN`
in the `environment:` block.

### On a VPS or always-on Linux box

```bash
sudo mkdir -p /opt/trading.bot && sudo chown -R "$USER" /opt/trading.bot
git clone https://github.com/Jameerie/trading.bot /opt/trading.bot
sudo useradd --system --home /opt/trading.bot advisor
sudo chown -R advisor /opt/trading.bot

sudo cp deploy/trading-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/trading-bot.service   # set the token
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
```

The unit binds to loopback and runs under a sandboxed non-root user. Put nginx
or Caddy in front of it for TLS:

```nginx
location / {
    proxy_pass http://127.0.0.1:8787;
    proxy_set_header Host $host;
}
```

---

## 4. Security — read this before exposing anything

The app **cannot place a trade**. It holds no broker credentials and has no
order-placement code. So the risk is not that someone drains your account
through it — it is that someone reads your setups and journal, or writes junk
into your performance record.

| Situation | What to do |
|---|---|
| `--host 127.0.0.1` (default) | Nothing. Only your machine can reach it. |
| `--host 0.0.0.0` on home wifi | **Set `TRADING_BOT_TOKEN`.** |
| Reachable from the internet | Set a token **and** terminate TLS at a proxy, or use Tailscale. |

```bash
export TRADING_BOT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
```

The server warns on startup if you bind to a public interface with no token.

Data-provider API keys come from the environment, never the config file:

```bash
export TRADING_BOT_API_KEY="your-key"   # only needed for live REST data
```

`/api/settings` reports whether a key exists. It never returns its value.

---

## 5. Point it at real data

Everything above runs on synthetic data. To get numbers that mean anything:

### Option A — CSV export from your broker (recommended)

Export OHLCV history from MT4/MT5, TradingView, or Dukascopy and drop it in:

```
data/samples/EURUSD_H1.csv
```

Headers are flexible — `timestamp/time/date/gmt time`, `open/o/<open>`, and so
on are all recognised, as are the usual vendor date formats and epoch seconds.

```bash
python3 -m trading_bot data --inspect data/samples/EURUSD_H1.csv
```

### Option B — live REST data

Get a free key from [twelvedata.com](https://twelvedata.com), then:

```bash
export TRADING_BOT_API_KEY="your-key"
```

```toml
# config/default.toml
[data]
source = "rest"
provider = "twelvedata"
```

### Then calibrate before you trust it

```bash
python3 -m trading_bot --config config/default.toml calibrate --csv data/samples/EURUSD_H1.csv
python3 -m trading_bot --config config/default.toml risk --from-backtest --csv data/samples/EURUSD_H1.csv
```

`calibrate` shows what each selectivity threshold really bought you
out-of-sample. `risk` turns the measured win rate into a position size — sized
from the **lower bound** of the confidence interval, not the observed rate.

---

## 6. Configuration

Everything lives in `config/default.toml`. Unknown keys are a hard error, so a
typo surfaces immediately rather than leaving you trading a setting you thought
you had changed.

```toml
[account]
balance = 10000.0
risk_per_trade_pct = 1.0     # run `risk` before raising this

[risk]
min_risk_reward = 4.0        # cannot be lowered - the loader refuses

[strategy]
min_confluence = 0.70        # the quality dial; run `calibrate` before changing
sessions = ["london", "newyork"]

[data]
source = "csv"
symbols = ["EURUSD", "GBPUSD", "USDJPY"]
```

Copy it to make your own, then pass `--config my.toml`.

---

## 7. Daily use

**In the browser** — open the app, hit **Scan**. Each symbol shows either a
signal card (entry, stop, target, lot size, and the reasoning) or a "no setup"
card with how close it came. Tick *Journal signals* to record what you were
advised.

When a trade finishes, go to **Journal**, type the price you actually exited at,
and hit *Close*. That is what turns the tool from a signal generator into
something that knows whether it is any good — the **Live performance** panel
compares your real results against the backtest, with the same confidence
interval treatment.

**From the terminal**, the same thing:

```bash
python3 -m trading_bot scan                                  # what to do now
python3 -m trading_bot journal --open                         # what is still running
python3 -m trading_bot journal --close EURUSD@2024-05-01T14:00:00+00:00 --exit 1.0865
python3 -m trading_bot journal                                # live performance
```

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ModuleNotFoundError: trading_bot` | `export PYTHONPATH=src` from the repo root. |
| `no CSV for EURUSD H1` | No data file. Run `python3 -m trading_bot data --generate`, or add your own export. |
| Phone cannot reach the server | You are on loopback. Restart with `--host 0.0.0.0` and set a token. Check the firewall allows 8787. |
| `401 unauthorized` | A token is set. Use the full URL the server printed, including `?token=...`. |
| Scan finds nothing, ever | Working as designed — it is selective. Lower `strategy.min_confluence`, then re-read the win rate. |
| `INSUFFICIENT DATA` on every backtest | Fewer than 30 trades. Use more bars or more symbols. The tool will not claim a win rate from a small sample. |
| Port already in use | `serve --port 9000`. |
| UI looks stale after an update | The service worker cached the old shell. Hard-refresh, or clear site data. |

---

## What next

- [`TESTING.md`](TESTING.md) — how the test suite is built and how to run it
- [`CLAUDE.md`](CLAUDE.md) — architecture and the rules that keep results honest
- [`README.md`](README.md) — what the system does and why it reports the way it does
