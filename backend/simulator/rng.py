"""Deterministic random streams.

Each stop and each bus gets its own stream, derived from the run seed and a name. An action
applied to one bus then cannot shift the random draws of every other stop and bus, which
keeps what-if comparisons fair (common random numbers) and runs reproducible.
"""

import math
import random


def stream(seed: int, name: str) -> random.Random:
    # str seeds are hashed with SHA-512 by CPython: stable across runs and platforms.
    return random.Random(f"vazhisync:{seed}:{name}")  # noqa: S311  (simulation, not crypto)


def poisson(rng: random.Random, lam: float) -> int:
    """Poisson draw (Knuth for small means, normal approximation for large ones)."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def lognormal_factor(rng: random.Random, sigma: float) -> float:
    """Multiplicative noise with mean 1.0."""
    if sigma <= 0:
        return 1.0
    return rng.lognormvariate(-(sigma**2) / 2, sigma)
