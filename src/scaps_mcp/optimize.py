"""Bounded Nelder-Mead search used for curve fitting and figure-of-merit optimisation."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

Evaluate = Callable[[list[list[float]]], Awaitable[list[float]]]


@dataclass(frozen=True)
class Variable:
    key: str
    low: float
    high: float
    start: float | None = None
    log: bool = False

    def __post_init__(self) -> None:
        if not self.low < self.high:
            raise ValueError(f"{self.key}: min must be below max")
        if self.log and self.low <= 0:
            raise ValueError(f"{self.key}: a log-scaled range must be positive")
        if self.start is not None and not self.low <= self.start <= self.high:
            raise ValueError(f"{self.key}: start must lie within [min, max]")

    def to_unit(self, value: float) -> float:
        if self.log:
            return (math.log10(value) - math.log10(self.low)) / (math.log10(self.high) - math.log10(self.low))
        return (value - self.low) / (self.high - self.low)

    def from_unit(self, u: float) -> float:
        u = min(1.0, max(0.0, u))
        if self.log:
            return 10 ** (math.log10(self.low) + u * (math.log10(self.high) - math.log10(self.low)))
        return self.low + u * (self.high - self.low)


def _clip(point: Sequence[float]) -> list[float]:
    return [min(1.0, max(0.0, c)) for c in point]


async def nelder_mead(
    evaluate: Evaluate, start: Sequence[float], max_evaluations: int, tolerance: float = 1e-4
) -> tuple[list[float], float, int]:
    """Minimise over the unit box. Returns the best point, its value and the evaluation count."""
    n = len(start)
    simplex = [_clip(start)]
    for i in range(n):
        point = list(simplex[0])
        point[i] = point[i] + 0.25 if point[i] <= 0.75 else point[i] - 0.25
        simplex.append(point)
    values = await evaluate(simplex)
    count = len(simplex)

    while count < max_evaluations:
        order = sorted(range(n + 1), key=lambda i: values[i])
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        spread = abs(values[-1] - values[0])
        size = max(max(abs(a - b) for a, b in zip(p, simplex[0], strict=True)) for p in simplex[1:])
        if math.isfinite(values[-1]) and spread < tolerance and size < 1e-3:
            break
        centroid = [sum(p[j] for p in simplex[:-1]) / n for j in range(n)]
        worst = simplex[-1]
        reflected = _clip([c + (c - w) for c, w in zip(centroid, worst, strict=True)])
        [fr] = await evaluate([reflected])
        count += 1
        if fr < values[0] and count < max_evaluations:
            expanded = _clip([c + 2 * (c - w) for c, w in zip(centroid, worst, strict=True)])
            [fe] = await evaluate([expanded])
            count += 1
            simplex[-1], values[-1] = (expanded, fe) if fe < fr else (reflected, fr)
        elif fr < values[-2]:
            simplex[-1], values[-1] = reflected, fr
        else:
            contracted = _clip([c + 0.5 * (w - c) for c, w in zip(centroid, worst, strict=True)])
            [fc] = await evaluate([contracted])
            count += 1
            if fc < values[-1]:
                simplex[-1], values[-1] = contracted, fc
            else:
                best = simplex[0]
                simplex = [best] + [[b + 0.5 * (p - b) for p, b in zip(q, best, strict=True)] for q in simplex[1:]]
                values = [values[0], *await evaluate(simplex[1:])]
                count += n
    i = min(range(len(values)), key=lambda k: values[k])
    return simplex[i], values[i], count


def interpolate(xs: Sequence[float], ys: Sequence[float], x: float) -> float | None:
    points = sorted((a, b) for a, b in zip(xs, ys, strict=True) if a is not None and b is not None)
    if not points or x < points[0][0] or x > points[-1][0]:
        return None
    for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
        if x1 <= x <= x2:
            return y1 if x2 == x1 else y1 + (y2 - y1) * (x - x1) / (x2 - x1)
    return points[-1][1]


def fit_error(
    sim_x: Sequence[float], sim_y: Sequence[float], x: Sequence[float], y: Sequence[float], log: bool
) -> float:
    """Normalised RMS deviation of a simulated curve from measured points (log10 |y| when log is set)."""
    residuals = []
    scale = max(abs(v) for v in y) or 1.0
    for xi, yi in zip(x, y, strict=True):
        si = interpolate(sim_x, sim_y, xi)
        if si is None:
            continue
        if log:
            if si == 0 or yi == 0:
                continue
            residuals.append(math.log10(abs(si)) - math.log10(abs(yi)))
        else:
            residuals.append((si - yi) / scale)
    if len(residuals) < 3:
        return math.inf
    return math.sqrt(sum(r * r for r in residuals) / len(residuals))
