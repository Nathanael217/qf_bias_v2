"""
utils/cache.py — TTL-based caching untuk QF_BIAS collectors & engine.

Strategi dual-mode:
  1. Runtime Streamlit  → st.cache_data(ttl=...) — shared across reruns, thread-safe.
  2. Unit test / CLI    → functools.lru_cache + manual timestamp check via _LRUCache.

Keduanya transparan: gunakan @ttl_cache(ttl_seconds=...) di mana saja.
"""

from __future__ import annotations

import functools
import time
import logging
from typing import Any, Callable, TypeVar, ParamSpec

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# ---------------------------------------------------------------------------
# Deteksi runtime Streamlit
# ---------------------------------------------------------------------------

def _streamlit_available() -> bool:
    """True jika streamlit bisa diimport DAN kita sedang dalam sesi Streamlit."""
    try:
        import streamlit as st  # noqa: F401
        # st.cache_data hanya ada di Streamlit ≥ 1.18
        return hasattr(st, "cache_data")
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Fallback: LRU + TTL manual
# ---------------------------------------------------------------------------

class _LRUCache:
    """
    Wrapper lru_cache dengan TTL manual per-call.

    Cara kerja:
      - Setiap *unique args/kwargs* disimpan beserta timestamp terakhir.
      - Kalau TTL terlewati, lru_cache-nya di-bust (clear) dan fungsi dipanggil ulang.
      - Bukan per-key bust (lru_cache tidak support itu), tapi cukup untuk collectors
        yang biasanya dipanggil dengan argumen tetap.
    """

    def __init__(self, func: Callable[P, R], ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._last_call: dict[tuple[Any, ...], float] = {}
        # Wrap dengan lru_cache unlimited; TTL bust akan clear semua.
        self._cached = functools.lru_cache(maxsize=128)(func)
        self._func = func
        functools.update_wrapper(self, func)

    def _cache_key(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
        return args + tuple(sorted(kwargs.items()))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        key = self._cache_key(args, kwargs)
        now = time.monotonic()
        last = self._last_call.get(key, 0.0)

        if now - last >= self._ttl:
            # Bust seluruh cache (efek samping: keys lain juga expired)
            self._cached.cache_clear()
            self._last_call.clear()
            self._last_call[key] = now
            logger.debug("Cache miss (TTL bust) untuk %s — memanggil ulang.", self._func.__name__)

        return self._cached(*args, **kwargs)

    def cache_clear(self) -> None:
        """Manual clear untuk testing."""
        self._cached.cache_clear()
        self._last_call.clear()

    def cache_info(self) -> functools._CacheInfo:  # type: ignore[type-arg]
        return self._cached.cache_info()


# ---------------------------------------------------------------------------
# Decorator publik: ttl_cache
# ---------------------------------------------------------------------------

def ttl_cache(ttl_seconds: int) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator TTL-aware untuk collector/engine functions.

    Perilaku:
      - Di dalam sesi Streamlit   → delegasi ke ``st.cache_data(ttl=ttl_seconds)``.
      - Di luar Streamlit (test)  → pakai ``_LRUCache`` (lru_cache + bust manual).

    Usage::

        from config import TTL
        from utils.cache import ttl_cache

        @ttl_cache(TTL["prices"])
        def fetch_prices() -> dict:
            ...

    Args:
        ttl_seconds: Durasi cache dalam detik.

    Returns:
        Decorator yang membungkus fungsi dengan caching TTL.
    """
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        if _streamlit_available():
            import streamlit as st
            # st.cache_data mengembalikan wrapper baru setiap kali dipanggil;
            # kita cache hasilnya agar dekorasi hanya terjadi sekali.
            wrapped = st.cache_data(ttl=ttl_seconds)(func)
            logger.debug(
                "ttl_cache: %s → st.cache_data(ttl=%d)", func.__name__, ttl_seconds
            )
            return wrapped  # type: ignore[return-value]
        else:
            wrapped_lru = _LRUCache(func, ttl_seconds)
            logger.debug(
                "ttl_cache: %s → _LRUCache(ttl=%d) [fallback]",
                func.__name__, ttl_seconds,
            )
            return wrapped_lru  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Utility: force_refresh context manager (untuk tombol Refresh di app.py)
# ---------------------------------------------------------------------------

class _NoOp:
    """Context manager kosong; dipakai kalau st tidak tersedia."""
    def __enter__(self) -> "_NoOp":
        return self
    def __exit__(self, *_: Any) -> None:
        pass


def clear_all_caches() -> None:
    """
    Bersihkan semua cache Streamlit sekaligus (dipakai tombol Refresh di app.py).

    Di luar Streamlit: no-op (tidak ada state yang perlu dihapus).
    """
    if _streamlit_available():
        import streamlit as st
        st.cache_data.clear()
        logger.info("Semua st.cache_data dibersihkan via clear_all_caches().")
    else:
        logger.debug("clear_all_caches() dipanggil di luar Streamlit — no-op.")
