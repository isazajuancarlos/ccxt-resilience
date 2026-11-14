# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Juan Carlos Isaza

"""ccxt-resilience — endurece un cliente ccxt legítimo contra los falsos
positivos de un WAF (Cloudflare) y los errores transitorios de red.

Dos herramientas, independientes entre sí:

    harden(exchange)        Ajusta User-Agent de navegador, Accept-Language y un
                            timeout holgado sobre un cliente ccxt ya construido,
                            para que el WAF challengue menos a algo que parece
                            un navegador.

    with_retry(fn, ...)     Ejecuta una llamada reintentando SOLO ante errores
                            anti-bot/transitorios (403/Cloudflare/DDoS/timeout),
                            con backoff exponencial + jitter. Los errores reales
                            (auth, fondos, lógica) se re-lanzan en el acto.

Alcance y ética: esto endurece TU cuenta contra la API OFICIAL del exchange —
mitiga falsos positivos del WAF sobre un cliente legítimo. No evade control de
acceso ni resuelve challenges; si el WAF insiste, se re-lanza el error.

Nacido de un bot de trading propio (OKX, tras rachas de 403 de Cloudflare) y
extraído como librería. La técnica es genérica a cualquier exchange ccxt detrás
de un WAF; el ajuste fino está probado contra OKX.
"""
from __future__ import annotations

import random
import time
from functools import wraps

__all__ = [
    "harden",
    "with_retry",
    "retry",
    "is_transient_error",
    "DEFAULT_USER_AGENT",
    "TRANSIENT_MARKERS",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_BASE_S",
    "DEFAULT_MAX_S",
    "__version__",
]

__version__ = "0.1.1"

# ---------------------------------------------------------------------------
# Defaults. Ajustables por argumento en cada llamada; estos son solo el piso.
# ---------------------------------------------------------------------------
DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_S = 1.0
DEFAULT_MAX_S = 6.0

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Subcadenas (en minúsculas) que delatan un bloqueo anti-bot o un transitorio de
# red en el mensaje de la excepción. La detección por mensaje es el fallback
# cuando no hay tipos ccxt disponibles o el error viene envuelto.
TRANSIENT_MARKERS = (
    "403", "forbidden", "cloudflare", "just a moment", "ddos",
    "429", "too many requests", "rate limit", "temporarily unavailable",
    "service unavailable", "challenge",
)


def _ccxt_retryable_types():
    """Tipos de ccxt que son transitorios, si ccxt está instalado. Defensivo:
    getattr por si cambian entre versiones, y ccxt es dependencia BLANDA — la
    detección por mensaje funciona sin él."""
    try:
        import ccxt  # noqa: WPS433 (import diferido a propósito)
    except Exception:
        return ()
    return tuple(
        c for c in (
            getattr(ccxt, "NetworkError", None),        # base de los transitorios
            getattr(ccxt, "DDoSProtection", None),      # 403/429 Cloudflare
            getattr(ccxt, "RateLimitExceeded", None),
            getattr(ccxt, "RequestTimeout", None),
            getattr(ccxt, "ExchangeNotAvailable", None),
        ) if c is not None
    )


def is_transient_error(exc: BaseException) -> bool:
    """True si la excepción parece un bloqueo anti-bot o un transitorio de red
    (reintetable). Los errores reales —auth, fondos, lógica— dan False: no se
    reintentan a lo bobo, porque reintentar solo gasta y enmascara el problema.

    Clasifica por tipo ccxt cuando puede, y si no, por marcadores en el mensaje.
    """
    tipos = _ccxt_retryable_types()
    if tipos and isinstance(exc, tipos):
        return True
    msg = str(getattr(exc, "args", "") or exc).lower()
    return any(m in msg for m in TRANSIENT_MARKERS)


def with_retry(fn, *args,
               attempts: int | None = None,
               base: float | None = None,
               max_s: float | None = None,
               is_retryable=is_transient_error,
               _sleep=time.sleep,
               _rand=random.random,
               **kwargs):
    """Ejecuta ``fn(*args, **kwargs)`` reintentando ante errores transitorios,
    con backoff exponencial ``min(max_s, base * 2**i)`` + jitter ``rand()*base``.

    - Si el error NO es reintetable (``is_retryable`` da False), se re-lanza en
      el acto: nada de reintentar un fallo de auth o de lógica.
    - Si se agotan los intentos, se re-lanza la ÚLTIMA excepción, para que el
      manejador del call site (tu try/except de fail-safe) siga funcionando.

    ``is_retryable`` es inyectable: pásale tu propio predicado para otras APIs.
    ``_sleep`` y ``_rand`` son costuras de test (permiten no dormir de verdad y
    fijar el jitter).
    """
    attempts = DEFAULT_ATTEMPTS if attempts is None else attempts
    base = DEFAULT_BASE_S if base is None else base
    max_s = DEFAULT_MAX_S if max_s is None else max_s
    attempts = max(1, int(attempts))
    ultima = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — clasificamos abajo, no tragamos
            ultima = e
            if not is_retryable(e):
                raise
            if i >= attempts - 1:
                break
            espera = min(max_s, base * (2 ** i)) + _rand() * base
            _sleep(espera)
    raise ultima


def retry(attempts: int | None = None,
          base: float | None = None,
          max_s: float | None = None,
          is_retryable=is_transient_error):
    """Versión decorador de :func:`with_retry`.

    >>> @retry(attempts=4)
    ... def fetch_balance():
    ...     return exchange.fetch_balance()
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            return with_retry(fn, *args,
                              attempts=attempts, base=base, max_s=max_s,
                              is_retryable=is_retryable, **kwargs)
        return wrapper
    return deco


def harden(exchange, *,
           user_agent: str = DEFAULT_USER_AGENT,
           accept_language: str = "en-US,en;q=0.9",
           min_timeout_ms: int = 15000,
           enabled: bool = True):
    """Endurece un cliente ccxt ya construido contra los challenges del WAF:
    User-Agent de navegador, cabecera Accept-Language y un timeout mínimo
    holgado. Devuelve el mismo objeto, para poder encadenar::

        ex = harden(ccxt.okx({...}))

    Nunca rompe la construcción del exchange: si algo falla al fijar los
    atributos, los deja como estaban. Con ``enabled=False`` es un no-op (útil
    para desactivarlo por configuración sin ramificar el código de llamada).
    """
    if not enabled:
        return exchange
    try:
        exchange.userAgent = user_agent
        exchange.headers = {**(getattr(exchange, "headers", None) or {}),
                            "Accept-Language": accept_language}
        actual = int(getattr(exchange, "timeout", 10000) or 10000)
        exchange.timeout = max(actual, min_timeout_ms)
    except Exception:
        pass
    return exchange
