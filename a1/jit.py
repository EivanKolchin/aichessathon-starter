"""Typed boundary for Numba; compilation is in memory and single-threaded."""

from collections.abc import Callable
from typing import cast

from numba import njit


def compiled[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    return cast(Callable[P, R], njit(cache=False)(function))


def pinned[**P, R](signature: object) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Compile one signature at import instead of specialising on call-site constants.

    Numba gives an integer or boolean constant at a call site a type of its own, so a helper
    reached once with a literal and once with a variable is compiled twice for one behaviour.
    Naming the signature compiles it once and converts the constants instead. Use it only
    where every caller passes these types: the dispatcher accepts no other after this.
    """

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        return cast(Callable[P, R], njit(signature, cache=False)(function))

    return decorate
