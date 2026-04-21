"""python -m poke_pon_bot"""

from __future__ import annotations

from dotenv import load_dotenv

from poke_pon_bot.client import run_bot


def main() -> None:
    load_dotenv()
    run_bot()


if __name__ == "__main__":
    main()
