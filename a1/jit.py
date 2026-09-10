"""Typed boundary for Numba; compilation is in memory and single-threaded."""

from collections.abc import Callable
from typing import cast

from numba import njit


def compiled[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    return cast(Callable[P, R], njit(cache=False)(function))
