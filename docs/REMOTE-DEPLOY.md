# Remote deploy (Slack → PR → Mac restart)

After you merge a fix on GitHub (from Slack `@cursor`, phone, or desktop), the Mac can **pull, migrate, and restart** the bot automatically so player data stays in `data/poke_cards.db`.

## Architecture

```text
Slack @cursor → PR → merge to master on GitHub
                         ↓
              (automation on Mac mini)
                         ↓
         git pull → pip → alembic → restart bot
```

Only **one** bot process should run on the Mac. Do not start `python -m poke_pon_bot` from a phone/cloud workspace.

---

## Step 0 — Repo location (important on macOS)

If the project lives under **`~/Documents`**, **`~/Desktop`**, or **`~/Downloads`**, **launchd cannot run the bot** there:

```text
Operation not permitted
getcwd: cannot access parent directories
```

Terminal works; background **launchd** does not (macOS privacy).

**Fix:** move the repo to a normal folder, then reinstall the service:

```bash
mkdir -p ~/Developer
mv ~/Documents/GitHub/Poke-Cards ~/Developer/Poke-Cards
cd ~/Developer/Poke-Cards
# reopen this folder in Cursor
./scripts/install-bot-service.sh
```

**Alternative:** skip launchd and keep running the bot in **Terminal.app** (deploy script still works for pull/migrate; restart manually or use `deploy-mac.sh` without launchd).

---

## Step 1 — Bot service on the Mac (recommended)

From the repo on the Mac (prefer `~/Developer/Poke-Cards`, not `~/Documents/...`):

```bash
chmod +x scripts/run-bot.sh scripts/deploy-mac.sh scripts/install-bot-service.sh
./scripts/install-bot-service.sh
```

This installs **launchd** label `org.pokepon.bot` (logs in `logs/bot.log`). Reboots and deploy restarts are handled cleanly.

To stop the service:

```bash
launchctl bootout gui/$(id -u)/org.pokepon.bot
```

---

## Step 2 — Pick an automation trigger

### Option A — GitHub Actions self-hosted runner (best after merge)

Runs deploy **immediately** when `master` is pushed.

1. GitHub → **Poke-Cards** → **Settings** → **Actions** → **Runners** → **New self-hosted runner** → macOS.
2. On the Mac mini, in the repo folder, run the commands GitHub shows (download runner, configure, start).
3. Merge a PR to `master` — workflow [`.github/workflows/deploy-mac.yml`](../.github/workflows/deploy-mac.yml) runs `./scripts/deploy-mac.sh`.

The Mac must be on and the runner process running (install as a service per GitHub’s docs).

### Option B — Cron poll (simplest, ~5 min delay)

No GitHub runner. Checks every few minutes for new commits.

```bash
crontab -e
```

Add (adjust path):

```cron
*/5 * * * * /Users/hamie/Documents/GitHub/Poke-Cards/scripts/deploy-mac.sh --check >> /Users/hamie/Documents/GitHub/Poke-Cards/logs/deploy-cron.log 2>&1
```

`--check` only deploys when `origin/master` is ahead of local `HEAD`.

### Manual deploy (SSH from phone)

```bash
cd /Users/hamie/Documents/GitHub/Poke-Cards
./scripts/deploy-mac.sh
```

---

## What `deploy-mac.sh` does

1. Lock (skip if a deploy is already running)
2. `git pull --ff-only origin master` (branch overridable with `POKEPON_DEPLOY_BRANCH`)
3. `pip install -r requirements.txt`
4. `alembic upgrade head`
5. Restart bot (launchd kickstart, or background `run-bot.sh`)

Logs: `logs/deploy.log`, `logs/bot.log`.

---

## Slack + Cursor workflow

1. In Slack: `@cursor fix … open PR` (channel default repo = Poke-Cards).
2. Review PR on GitHub (phone).
3. **Merge to `master`.**
4. Automation on the Mac runs deploy (Option A or B).
5. Confirm: `tail -f logs/deploy.log` or check bot online in Discord.

Website-only changes still need a separate push to `hamiebrooklyn.github.io` (see workspace rule).

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `POKEPON_DEPLOY_BRANCH` | `master` | Branch to pull |
| `POKEPON_DEPLOY_REMOTE` | `origin` | Git remote |
| `POKEPON_LAUNCHD_LABEL` | `org.pokepon.bot` | launchd label |

---

## Safety notes

- **Fast-forward only** — if local Mac has unpushed commits, `git pull --ff-only` fails (protects you from overwriting work).
- **SQLite** — same `data/poke_cards.db`; deploy does not wipe data.
- **Avoid deploy loops** — don’t push commits from the Mac runner that re-trigger deploy unless intended.
- **Discord rate limits** — frequent restarts can hit login limits; batch merges when possible.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Workflow queued forever | Self-hosted runner offline — start runner service on Mac |
| `pull --ff-only` failed | SSH to Mac, commit or stash local changes, redeploy |
| Bot still old behavior | Check `logs/deploy.log`; confirm merge hit `master` |
| Two bots | Stop phone/cloud bot; only Mac should run token |
| Service won’t start | Check `logs/bot.log`, `.env` exists, `DISCORD_TOKEN` set |
| `Operation not permitted` in `logs/bot.log` | Repo is under `~/Documents` — move to `~/Developer` (see Step 0) |
