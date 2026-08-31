# Deployment files

| File | What it is for |
|---|---|
| `../Dockerfile` | Container image. No dependencies to install, so it is a copy and a `USER`. |
| `../docker-compose.yml` | One-command run. Publishes to `127.0.0.1` only by default. |
| `trading-bot.service` | systemd unit for a Linux box or VPS, with a locked-down sandbox. |

See [`../SETUP.md`](../SETUP.md) for the full guide, including reaching the app
from a phone and exposing it safely over the internet.

## The one rule

The app is **read-only advice** and holds no broker credentials, but your
journal and configuration are still yours. Whenever it listens on anything other
than `127.0.0.1`, set a token:

```bash
export TRADING_BOT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
```

Without one, anyone who can reach the port can read and write your journal.
