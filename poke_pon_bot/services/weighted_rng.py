"""Weighted random selection."""

from __future__ import annotations

import random
from typing import TypeVar

T = TypeVar("T")


def weighted_choice(
    rng: random.Random,
    items: list[tuple[T, float]],
) -> T:
    """Pick one item with probability proportional to its non-negative weight."""
    if not items:
        raise ValueError("weighted_choice requires at least one item")

    filtered = [(x, w) for x, w in items if w > 0]
    if not filtered:
        raise ValueError("all weights are zero or negative")

    total = sum(w for _, w in filtered)
    threshold = rng.random() * total
    upto = 0.0
    for x, w in filtered:
        upto += w
        if upto >= threshold:
            return x
    return filtered[-1][0]
