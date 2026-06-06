"""Load the correct dotenv file for prod vs staging."""

from __future__ import annotations

import os

from dotenv import load_dotenv


def load_pokepon_dotenv() -> None:
    """Respect ``POKEPON_ENV_FILE`` when set (staging scripts / launchd)."""
    env_file = (os.environ.get("POKEPON_ENV_FILE") or "").strip()
    if env_file:
        load_dotenv(env_file, override=True)
    else:
        load_dotenv()
