# Mac Startup Guide

After a reboot, three services need to be running for the full Poké-Pon stack.

## Prerequisites

| Dependency | Install |
|---|---|
| Python 3.14+ | Already at `/Library/Frameworks/Python.framework/` |
| cloudflared | `brew install cloudflared` |
| GitHub Actions Runner | Pre-configured at `actions-runner/` in the repo |

All config lives in `/Users/hamie/Developer/Poke-Cards/.env`.

---

## 1. Discord Bot (port 8080)

The bot is registered as a launchd service (`org.pokepon.bot`) with `RunAtLoad` and `KeepAlive`, so it **should start automatically on login**. If it didn't:

```bash
# Check if it's running
pgrep -lf "poke_pon_bot"

# If not, load the launchd service
launchctl load ~/Library/LaunchAgents/org.pokepon.bot.plist

# Or start manually (foreground — useful for debugging)
cd /Users/hamie/Developer/Poke-Cards
bash scripts/run-bot.sh

# Or start manually (background)
cd /Users/hamie/Developer/Poke-Cards
nohup bash scripts/run-bot.sh >> logs/bot.log 2>&1 &
```

To restart:

```bash
cd /Users/hamie/Developer/Poke-Cards
bash scripts/stop-bot.sh
sleep 2
nohup bash scripts/run-bot.sh >> logs/bot.log 2>&1 &
```

Verify: `curl -s http://localhost:8080/api/events` should return JSON.

**Staging** (port 8081, separate DB and bot token): see [STAGING.md](./STAGING.md). Quick start: `bash scripts/start-bot-staging.sh` then `bash scripts/verify-staging.sh`.

---

## 2. Cloudflare Tunnel (api.pokepon.org)

Exposes `localhost:8080` as `https://api.pokepon.org` so the GitHub Pages website can reach the bot API.

```bash
# Check if it's running
pgrep -lf cloudflared

# If not, start it (background)
nohup cloudflared tunnel run pokepon-api >> /Users/hamie/Developer/Poke-Cards/logs/cloudflared.log 2>&1 &
```

The tunnel config is at `~/.cloudflared/config.yml`:

```yaml
tunnel: a2503459-23b6-454c-866f-7f36b7bc1ef7
credentials-file: /Users/hamie/.cloudflared/a2503459-23b6-454c-866f-7f36b7bc1ef7.json

ingress:
  - hostname: api.pokepon.org
    service: http://localhost:8080
  - hostname: api-staging.pokepon.org
    service: http://localhost:8081
  - service: http_status:404
```

Verify: `curl -s https://api.pokepon.org/api/events` should return the same as localhost.

---

## 3. GitHub Actions Runner (self-hosted CI/CD)

Runs CI workflows when PRs are opened or merged.

```bash
# Check if it's running
pgrep -lf "Runner.Listener"

# If not, start it (background)
cd /Users/hamie/Documents/GitHub/Poke-Cards/actions-runner
nohup ./run.sh >> _diag/runner.log 2>&1 &
```

Verify: Go to https://github.com/HamieBrooklyn/Poke-Cards/settings/actions/runners — the runner `Mac-mini-som-tillhor-Alexander` should show as **Idle** (green).

---

## Quick Start (all three at once)

Copy-paste this block to start everything after a fresh reboot:

```bash
cd /Users/hamie/Developer/Poke-Cards

# 1. Bot
pgrep -qf "poke_pon_bot" || nohup bash scripts/run-bot.sh >> logs/bot.log 2>&1 &

# 2. Cloudflare Tunnel
pgrep -qf cloudflared || nohup cloudflared tunnel run pokepon-api >> logs/cloudflared.log 2>&1 &

# 3. Actions Runner
pgrep -qf "Runner.Listener" || (cd /Users/hamie/Documents/GitHub/Poke-Cards/actions-runner && nohup ./run.sh >> _diag/runner.log 2>&1 &)

# Wait and verify
sleep 15
echo "Bot:        $(pgrep -c -f 'poke_pon_bot' 2>/dev/null || echo 0) process(es)"
echo "Cloudflare: $(pgrep -c -f 'cloudflared'  2>/dev/null || echo 0) process(es)"
echo "Runner:     $(pgrep -c -f 'Runner.Listener' 2>/dev/null || echo 0) process(es)"
curl -s http://localhost:8080/api/events | head -c 80 && echo
```

---

## Stopping Everything

```bash
cd /Users/hamie/Developer/Poke-Cards

# Bot
bash scripts/stop-bot.sh

# Cloudflare Tunnel
pkill -f cloudflared

# Actions Runner
cd /Users/hamie/Documents/GitHub/Poke-Cards/actions-runner
./svc.sh stop 2>/dev/null || pkill -f "Runner.Listener"
```

---

## Deploying New Code

After pushing/merging to `master`:

```bash
cd /Users/hamie/Developer/Poke-Cards
bash scripts/deploy-mac.sh
```

This pulls latest code, installs deps, runs migrations, and restarts the bot. The tunnel and runner don't need restarting for code changes.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Website shows "Network Error" | Check bot is running (`pgrep -lf poke_pon_bot`) and tunnel is up (`pgrep -lf cloudflared`) |
| Bot starts but Discord commands don't work | Check `logs/bot.log` for token or intent errors |
| Port 8080 already in use | `bash scripts/stop-bot.sh` kills anything on that port |
| Tunnel shows 530 error | Tunnel is running but bot isn't — start the bot first |
| Runner shows "Offline" on GitHub | Start it with `./run.sh` in the actions-runner directory |
| Database migration errors | `cd /Users/hamie/Developer/Poke-Cards && .venv/bin/alembic upgrade head` |
| Python venv broken after OS update | `rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |

---

## File Locations

| What | Path |
|---|---|
| Bot source | `/Users/hamie/Developer/Poke-Cards/` |
| Bot venv | `/Users/hamie/Developer/Poke-Cards/.venv/` |
| Bot config | `/Users/hamie/Developer/Poke-Cards/.env` |
| Staging config | `/Users/hamie/Developer/Poke-Cards/.env.staging` |
| Database | `/Users/hamie/Developer/Poke-Cards/data/poke_cards.db` |
| Staging database | `/Users/hamie/Developer/Poke-Cards/data/poke_cards_staging.db` |
| Bot logs | `/Users/hamie/Developer/Poke-Cards/logs/bot.log` |
| Staging logs | `/Users/hamie/Developer/Poke-Cards/logs/bot-staging.log` |
| Deploy logs | `/Users/hamie/Developer/Poke-Cards/logs/deploy.log` |
| Launchd plist | `~/Library/LaunchAgents/org.pokepon.bot.plist` |
| Cloudflare config | `~/.cloudflared/config.yml` |
| Cloudflare credentials | `~/.cloudflared/a2503459-*.json` |
| Actions Runner | `/Users/hamie/Documents/GitHub/Poke-Cards/actions-runner/` |
| Website repo | `/Users/hamie/Documents/GitHub/hamiebrooklyn.github.io/` |
