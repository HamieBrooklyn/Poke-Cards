# TCG Tabletop

The separate `/tcg/` website mode supports two authenticated human players, 60-card
decks, live private state, complete matches, and persistent reconnection. This is
a **playable preview, not complete Pokémon TCG card coverage**. The existing
Discord/website combat engines and their six-card decks remain separate.

## Where to play

- Staging website: https://staging.pokepon.org/tcg/
- Staging API: https://api-staging.pokepon.org/api/tcg/me
- Source website: `~/Documents/GitHub/hamiebrooklyn.github.io/tcg/`
- New backend: `services/tcg_engine.py`, `services/tcg_catalog.py`,
  `services/tcg_matches.py`, and `web/tcg_api.py` under `poke_pon_bot/`.

Sign in with Discord, create a table, and share its eight-character code or invite
link. The second player joins, both choose 60-card decks, and both ready up. The
host starts. Global includes a starter-deck button, and the workshop can save and
reuse custom decks. The same button works with collection pools when the available
copies can supply that starter.

## Deck access

| Lobby setting | Each player's available copies |
| --- | --- |
| Global | Catalog cards without ownership restrictions, subject to deck construction limits |
| Owned | Only that player's owned copies, including Basic Energy |
| Owned + Mixed | Both players' combined quantities, independently available to each player |

Mixed is a toggle under Owned. Both players can use the same shared printing at
the same time. This is virtual access: inventory is neither transferred nor
consumed. Changing a deck or setting clears readiness. Start revalidates both
decks and snapshots the eligible quantities and full rules definitions in one
database transaction; later collection trades cannot alter an active game. Active and finished tables
use the saved collection pool; a rematch refreshes sharing. Closed tables revoke
collection-pool access.

The format is labeled **Casual all-catalog**. Catalog access is separate from
Standard or Expanded tournament legality; no tournament-legal claim is made.

## Rules and coverage

The engine records `physical-tcg-2026-02-20-v1`. Reference sources:

- https://tcg.pokemon.com/en-us/learn/
- https://professorprogram.pokemon.com/news/11473085
- https://play.pokemon.com/en-us/resources/documents/tcg-errata/

Implemented: 60-card setup, starting-player choice, revealed mulligans and
compensation, face-down Active/Bench setup, six Prizes, first-turn restrictions,
draw/bench/evolve/attach, supported Trainers, Energy costs without spending Energy
on ordinary attacks, printed Weakness/Resistance, retreat, all five Special
Conditions, Checkup, Knock Outs, multi-Prize rule boxes, promotion and Prize
choices, all core win conditions, six-Prize tiebreaker restart, surrender,
disconnect claims, and rematches. Card handlers include numerical attacks,
supported coin flips, Conditions, healing, recoil, drawing, Energy discard,
switch/gust, searches, and basic/Double Colorless Energy. Common Abilities now
include once-per-turn draws, drawing to a hand-size limit, self-healing, and
additional typed Basic Energy attachments. Supported Tools modify HP, retreat
costs or attack damage; supported Stadiums reduce retreat costs or heal eligible
Pokémon. These effects appear as server-validated actions on the table.

The initial staging import contains **20,485 catalog entries**, **20,444 matched
rules definitions**, and **4,272 supported printings**. See `TCG_COVERAGE.json` for
counts, source revision, and reasons. Missing data, unsupported attacks, most Abilities,
Ancient Traits, unimplemented Tool/Stadium effects, and advanced mechanics such as VSTAR/VMAX,
Tera and most other special rule boxes remain blocked. No fallback attacks or
invented effects are substituted. Unsupported entries are visible when the
workshop's “Playable cards only” filter is cleared.

Full card coverage remains outstanding. Add exact handlers and rule tests before
allowing a new effect. `normalize_card()` is deliberately conservative and matches
complete text rather than substrings. The engine also validates descriptors before
starting a match. Printed Potion uses the current 30-damage erratum across its
older printings.

## Persistence and privacy

Migration `065_tcg_mode` adds `tcg_card_definitions`, `tcg_lobbies`,
`tcg_saved_decks`, and `tcg_commands`. It does not change legacy Card columns.
Every match action is checked against its actor and expected lobby version. A
transactional compare-and-set plus unique per-player command receipts prevents
duplicate or competing moves. Reusing a command ID with different content fails.
Saved match state and private transition snapshots persist random outcomes and
choices for recovery/replay. Production randomness uses `SystemRandom`; tests
inject deterministic randomness.

HTTP and WebSockets use the same allowlisted per-viewer projection. Opponent hand
identifiers, both deck orders, unrevealed Prizes, original deck lists and private
choices never enter the other player's payload. Choice bounds derived from hidden
cards are also withheld from the opponent. Search reveals only the selected card
names to the opponent. No full-state broadcast from the legacy duel transport is
reused. Logs and private command snapshots must never be exposed as public API
responses.

Presence survives server restarts. After an opponent is absent for three minutes,
the remaining player may claim the game; both absent players do not automatically
produce a winner. A server restart grants a fresh grace period. Browser refreshes
restore the current private state and pending choice. Long-lived sockets close
when their signed session expires.

## Verification

```sh
.venv/bin/python -m pytest tests/test_tcg_engine.py tests/test_tcg_effects.py tests/test_tcg_catalog.py tests/test_tcg_api.py -q
```

The 53-check targeted suite covers rule outcomes, deck validation, access pools, sealed
information, HTTP/WebSocket authentication, illegal actors, idempotence, concurrent
commands, restart recovery, and complete non-surrender matches. The browser QA
also completed a real-card match from create/join through deck saving, setup,
Energy Search, alternating attacks, a Knock Out, Prize selection and result. Both
sessions received the correct private view, and refresh resumed a pending search.
An additional real-card scenario verified Giant Cape increases Audino from 90 to
110 HP, Rough Seas enters the shared Stadium area, and Hearing draws once before
becoming unavailable. Desktop and 390px mobile tables were visually checked.

For isolated browser testing (no production/staging credentials or inventories):

```sh
.venv/bin/python scripts/serve-tcg-test.py
# Deterministic real-card Ability/Tool/Stadium scenario:
.venv/bin/python scripts/serve-tcg-test.py --effects
```

It binds only `127.0.0.1:8891`, creates a disposable database, and prints two test
login links using separate localhost/127.0.0.1 sessions. The test-login routes are
only in that standalone harness; they are never registered by the bot.

## Import and staging deployment

Full rules data comes from the existing catalog's upstream dataset:
https://github.com/PokemonTCG/pokemon-tcg-data

```sh
git clone --depth 1 https://github.com/PokemonTCG/pokemon-tcg-data.git /tmp/pokepon-tcg-data
POKEPON_ENV_FILE=.env.staging .venv/bin/alembic upgrade head
POKEPON_ENV_FILE=.env.staging .venv/bin/python -m poke_pon_bot.scripts.import_tcg_definitions \
  --source-dir /tmp/pokepon-tcg-data --write --report docs/TCG_COVERAGE.json
bash scripts/start-bot-staging.sh
```

The importer only enriches existing catalog rows. It records the upstream revision
and does not modify owned collections. Run it after future catalog syncs so new
printings acquire complete metadata; unsupported effects remain blocked. There is
currently no automatic enrichment hook in the legacy catalog sync.

The feature registers routes only for `pokepon_runtime=staging` or an explicit
`WEB_TCG_ENABLED=1`. Production must remain gated until separately authorized.
Follow `docs/STAGING.md` and the website's `docs/WEBSITE_STAGING.md`. The browser
client is static DOM/CSS/JavaScript; no Phaser build is required for this separate
mode. Legacy Phaser/Discord activity builds remain untouched.

When the website source contains unrelated uncommitted user edits, stage only the
TCG files and sync those committed files to the staging site rather than invoking
the deployment script's `git add -A` over unrelated work.
