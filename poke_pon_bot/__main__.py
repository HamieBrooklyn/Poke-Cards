"""python -m poke_pon_bot"""

from __future__ import annotations

import logging

from poke_pon_bot.env_loader import load_pokepon_dotenv
from poke_pon_bot.client import run_bot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    load_pokepon_dotenv()
    run_bot()


if __name__ == "__main__":
    main()
