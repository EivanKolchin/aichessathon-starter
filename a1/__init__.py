"""Original compiled-search experiment; independent of the working A0 entrypoint."""

import os

# Select before any a1 module imports Numba. LLVM's default O3 exceeded the game init limit
# in local trials; O1 trades optimisation work for startup margin. Offline ablations may
# explicitly set NUMBA_OPT before importing this package. No compiled cache is shipped.
os.environ.setdefault("NUMBA_OPT", "1")

__version__ = "0.2.2"
