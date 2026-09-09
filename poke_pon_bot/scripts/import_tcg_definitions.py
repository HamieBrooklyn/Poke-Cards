"""Import full TCG rules metadata without changing legacy Card rows.

Obtain the public source with:
  git clone --depth 1 https://github.com/PokemonTCG/pokemon-tcg-data.git /tmp/pokepon-tcg-data
Preview coverage:
  .venv/bin/python -m poke_pon_bot.scripts.import_tcg_definitions --source-dir /tmp/pokepon-tcg-data
Write staging after migrating:
  .venv/bin/python -m poke_pon_bot.scripts.import_tcg_definitions --source-dir /tmp/pokepon-tcg-data --write

The explicit environment file defaults to .env.staging; neither catalog rows nor
collection inventories are modified. Source cards absent from the global catalog
are ignored. Unmatched catalog entries remain visible but unplayable.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess

from dotenv import load_dotenv
from sqlalchemy import select

from poke_pon_bot.config import load_settings
from poke_pon_bot.db.session import async_session_factory, create_engine_from_url
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.tcg_card import TcgCardDefinition
from poke_pon_bot.services.tcg_catalog import coverage_report, normalize_card


def read_source(path: Path) -> tuple[dict[str, dict], str]:
    files = sorted((path / "cards" / "en").glob("*.json"))
    if not files:
        raise ValueError("Source directory must contain cards/en/*.json from PokemonTCG/pokemon-tcg-data.")
    data, digest = {}, hashlib.sha256()
    for file in files:
        content = file.read_bytes()
        digest.update(content)
        for card in json.loads(content):
            if card["id"] in data:
                raise ValueError("Duplicate source printing: " + card["id"])
            data[card["id"]] = card
    try:
        revision = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = digest.hexdigest()
    return data, "pokemontcg-data:" + revision


async def run(args: argparse.Namespace) -> dict:
    if not args.env_file.is_file():
        raise ValueError("The selected environment file does not exist.")
    load_dotenv(args.env_file, override=True)
    settings = load_settings(require_discord_token=False)
    if "staging" not in args.env_file.name and not args.allow_production:
        raise ValueError("Production imports require --allow-production.")
    data, version = read_source(args.source_dir)
    engine = create_engine_from_url(settings.database_url)
    try:
        factory = async_session_factory(engine)
        async with factory() as session:
            cards = list((await session.scalars(select(Card))).all())
            definitions = {card.tcg_card_id: normalize_card(card, data.get(card.tcg_card_id)) for card in cards}
            report = coverage_report(definitions)
            report.update({
                "source": "https://github.com/PokemonTCG/pokemon-tcg-data",
                "data_version": version,
                "source_cards": len(data),
                "matched_catalog_cards": sum(card.tcg_card_id in data for card in cards),
                "missing_metadata": [card.tcg_card_id for card in cards if card.tcg_card_id not in data],
                "written": bool(args.write),
            })
            if args.write:
                existing = {row.tcg_card_id: row for row in (await session.scalars(select(TcgCardDefinition))).all()}
                changed = 0
                for card in cards:
                    raw = data.get(card.tcg_card_id)
                    if raw is None:
                        continue
                    row = existing.get(card.tcg_card_id)
                    if row is None:
                        session.add(TcgCardDefinition(tcg_card_id=card.tcg_card_id, data=raw, data_version=version))
                        changed += 1
                    elif row.data != raw or row.data_version != version:
                        row.data, row.data_version = raw, version
                        changed += 1
                await session.commit()
                report["changed_definitions"] = changed
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            return report
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env.staging"))
    parser.add_argument("--write", action="store_true", help="Persist definitions (default: coverage preview only).")
    parser.add_argument("--allow-production", action="store_true", help="Allow an explicit production import.")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
