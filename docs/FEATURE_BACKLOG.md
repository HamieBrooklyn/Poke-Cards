# Feature backlog

Ideas that fit Poké Pon as a Discord-first TCG economy with a growing web app. Captured from a planning session so we do not lose track of them.

**Status key:** `done` · `partial` · `planned` · `deferred`

---

## High impact (builds on what we have)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | **Seasonal set chase + shared progress bar** | **partial** | v1 shipped: featured set drop boost, community bar, personal binder reward (80%), `/setchase`, home panel, `SET_CHASE_*` env. Community participation payout (auto 💎 when bar fills) + `/dev set_chase` test commands also done. Still open: DM on payout, richer mission tie-ins. |
| 2 | **Missions on the website + push when complete** | **done** | `/missions/` page, `GET/PATCH /api/me/missions`, claim API, `notify_missions` pref + Discord DM on complete. |
| 3 | **Wishlist → market alerts** | **done** | Discord DM when a wishlisted card is auction-listed or added to a trade; optional max ₽/💎 caps in Settings; hooks on auction create + trade update/offer. |
| 4 | **Discord-native duels** | planned | Web duels removed (auth/UX pain). Reuse deck editor + escrow as `/duel challenge @user` with buttons — skip Phaser client. |
| 5 | **Grading as a prestige loop** | shipped | `/grade` + web grading; slab badges on trades/auctions/leaderboards/`/coll`; **Top graded** LB; shop sell +2%/grade above 6 (max +8% at 10). |

---

## Economy & balance

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 6 | **Crystal sinks that feel good** | shipped | `/cd` **Reroll 8 💎**; **`/auction spotlight`** (12 💎); leaderboard frames (11 variants, preview + select on Profile → Cosmetics). |
| 7 | **Shop bundles tied to events** | planned | Stripe shop + weekend/set events → limited bundles (“Weekend luck booster”, starter crystal bundle). |
| 8 | **Bulk sell / market intel on web** | **partial** | Bulk select + sell on collection page done. Still open: price history, suggested list price, “similar cards sold for X”. |

---

## Social & retention

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 9 | **Guild / server milestones** | shipped | Per-server pack + trade stats, milestone announcements, `/servermilestones`, web **Server** tab + milestone bar, optional top trader/collector roles. |
| 10 | **Referrals 2.0** | done | `/referral` on Discord; first `/cd` rewards inviter + invitee; milestone **25** 💎 at 10 packs (cap 3). Profile + home copy updated. |
| 11 | **Tutorial quest chain** | done | 5-step DM quest: balance → daily → drop → collection → open pack. **40** 💎 total; Member role on completion. Legacy steps auto-mapped. |

---

## Web app

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 12 | **Unified activity feed** | **done** | `/activity/` page, `GET /api/me/activity` — drops, web trades, auction win/sale, mission claims, weekend luck banner. |
| 13 | **Notifications (opt-in)** | **done** | Discord prefs wired (trades, outbid, missions, wishlist, referrals, daily, vote, card drop); inbox API + 🔔 panel; optional browser alerts (`notify_browser`); hourly engagement reminders. |
| 14 | **Mobile polish pass** | done | Audit tap targets, sticky actions, copy card ID everywhere on collection/trades/auctions. |

---

## Ops & trust

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 15 | **Production deploy parity with staging** | partial | Staging (`api-staging`, seed scripts, verify) in place. Automate prod the same way + health dashboard (bot up, DB revision, last catalog sync). |
| 16 | **Admin / mod tools** | partial | `/dev` exists (economy, drops, set chase sim, etc.). Expand: refund auction, cancel stuck trade, grant event luck, audit user economy. |
| 17 | **Event scheduler** | done | DB-backed scheduled events: luck boost, double daily, free/set spotlight; `/dev event` CRUD; `GET /api/events` `game_events`; env weekend luck fallback. |

---

## If you only pick three (original recommendation)

| Priority | Feature | Why |
|----------|---------|-----|
| 1 | Wishlist + auction/trade alerts | Uses existing systems; directly boosts trading |
| 2 | Discord-native duels | PvP without web auth pain |
| 3 | Missions + activity on website | One loop across Discord and pokepon.org |

---

## Avoid for now

- Rebuilding **web duels** until Discord duels feel solid
- New currencies or battle pass complexity before crystal sinks exist
- Huge new minigames before auction/trade/mission loops feel tight on mobile

---

## Set chase (feature #1) — implemented scope

For reference, what shipped for the seasonal set chase:

| Piece | Where |
|-------|--------|
| Models + migrations | `poke_pon_bot/models/set_chase.py`, `048_set_chase`, `049_set_chase_community` |
| Service | `poke_pon_bot/services/set_chase.py` |
| Discord | `/setchase`, `/setchase claim` |
| Drop hook | Featured-set `/cd` claims increment community bar + register participants |
| Drop boost | Set chase theme in `drops.py` |
| Missions | Daily `claim_set` uses featured set when active |
| API | `GET /api/events` (`set_chase`), `POST /api/me/set-chase/claim` |
| Website | Home panel — `hamiebrooklyn.github.io` `home-events.js` |
| Config | `SET_CHASE_*`, `SET_CHASE_COMMUNITY_REWARD_CRYSTALS` in `.env.example` |
| Dev testing | `/dev set_chase simulate`, `reset`, `status` |

**Reward model**

| Reward | Trigger | Default (staging) |
|--------|---------|-------------------|
| Community participation | Shared bar hits 100%; claimed ≥1 featured-set card from `/cd` | 15 💎 (auto, once) |
| Personal binder | Own ≥80% of set unique printings; manual claim | 50 💎 + ₽2,500 |

---

*Last updated: 2026-05-28 — update status rows as features ship.*
