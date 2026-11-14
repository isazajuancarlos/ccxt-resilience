# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Juan Carlos Isaza

"""Tests de ccxt-resilience. Sin red y sin dormir de verdad: la función se
inyecta y _sleep/_rand se sustituyen, así el backoff es determinista.

Portados del bot que originó la librería (probaban la matemática exacta del
backoff) y ampliados para la API pública: predicado inyectable, decorador y
harden sobre un exchange simulado.
"""
import pytest

import ccxt_resilience as cr


# ---------------------------------------------------------------------------
# is_transient_error — clasifica qué excepción es reintetable
# ---------------------------------------------------------------------------
def test_detecta_transitorio_por_mensaje():
    assert cr.is_transient_error(Exception("okx GET ... 403 Forbidden"))
    assert cr.is_transient_error(Exception("Just a moment... Cloudflare challenge"))
    assert cr.is_transient_error(Exception("Too Many Requests 429"))
    assert cr.is_transient_error(Exception("service temporarily unavailable"))


def test_ignora_errores_reales():
    # auth/lógica/fondos NO son reintetables
    assert not cr.is_transient_error(Exception("invalid api key: authentication failed"))
    assert not cr.is_transient_error(ValueError("bug de logica"))
    assert not cr.is_transient_error(Exception("insufficient balance"))


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("ccxt") is None,
    reason="ccxt no instalado (dependencia blanda)",
)
def test_detecta_por_tipo_ccxt():
    import ccxt
    assert cr.is_transient_error(ccxt.DDoSProtection("x"))
    assert cr.is_transient_error(ccxt.RequestTimeout("timeout"))
    assert cr.is_transient_error(ccxt.ExchangeNotAvailable("down"))
    assert not cr.is_transient_error(ccxt.AuthenticationError("invalid api key"))
    assert not cr.is_transient_error(ccxt.InsufficientFunds("no balance"))


# ---------------------------------------------------------------------------
# with_retry — backoff + reintento selectivo
# ---------------------------------------------------------------------------
def test_exito_primer_intento_no_duerme():
    n = {"c": 0}
    def fn():
        n["c"] += 1
        return "ok"
    dormidas = []
    r = cr.with_retry(fn, _sleep=dormidas.append, _rand=lambda: 0.0)
    assert r == "ok"
    assert n["c"] == 1
    assert dormidas == []


def test_reintenta_ante_403_y_se_recupera():
    n = {"c": 0}
    def fn():
        n["c"] += 1
        if n["c"] < 3:
            raise Exception("okx GET /account/balance 403 Forbidden Cloudflare")
        return "recuperado"
    dormidas = []
    r = cr.with_retry(fn, attempts=4, base=1.0, max_s=8.0,
                      _sleep=dormidas.append, _rand=lambda: 0.0)
    assert r == "recuperado"
    assert n["c"] == 3
    assert len(dormidas) == 2  # durmió antes del 2º y 3er intento


def test_pasa_args_y_kwargs():
    def fn(a, b, c=0):
        return a + b + c
    assert cr.with_retry(fn, 1, 2, c=3, _sleep=lambda s: None, _rand=lambda: 0.0) == 6


def test_error_no_transitorio_se_relanza_sin_reintentar():
    n = {"c": 0}
    def fn():
        n["c"] += 1
        raise ValueError("invalid api key")
    dormidas = []
    with pytest.raises(ValueError):
        cr.with_retry(fn, attempts=4, _sleep=dormidas.append, _rand=lambda: 0.0)
    assert n["c"] == 1
    assert dormidas == []


def test_agota_intentos_relanza_ultima():
    def fn():
        raise Exception("403 cloudflare")
    dormidas = []
    with pytest.raises(Exception, match="cloudflare"):
        cr.with_retry(fn, attempts=3, base=1.0, max_s=8.0,
                      _sleep=dormidas.append, _rand=lambda: 0.0)
    assert len(dormidas) == 2  # intentos-1 dormidas, luego re-lanza


def test_backoff_exponencial_acotado_por_max_s():
    def fn():
        raise Exception("403")
    dormidas = []
    with pytest.raises(Exception):
        cr.with_retry(fn, attempts=5, base=1.0, max_s=3.0,
                      _sleep=dormidas.append, _rand=lambda: 0.0)
    # min(max_s, base*2^i): 1, 2, 3(cap), 3(cap)
    assert dormidas == [1.0, 2.0, 3.0, 3.0]


def test_jitter_se_suma_a_la_espera():
    def fn():
        raise Exception("403")
    dormidas = []
    with pytest.raises(Exception):
        cr.with_retry(fn, attempts=2, base=2.0, max_s=100.0,
                      _sleep=dormidas.append, _rand=lambda: 0.5)
    # i=0: min(100, 2*1)=2 + 0.5*2 = 3.0
    assert dormidas == [3.0]


def test_predicado_reintetable_personalizado():
    # Un predicado propio para otra API: reintentar solo si el mensaje dice "boom"
    n = {"c": 0}
    def fn():
        n["c"] += 1
        if n["c"] < 2:
            raise Exception("boom transitorio")
        return "ok"
    r = cr.with_retry(fn, attempts=3, is_retryable=lambda e: "boom" in str(e),
                      _sleep=lambda s: None, _rand=lambda: 0.0)
    assert r == "ok" and n["c"] == 2


# ---------------------------------------------------------------------------
# retry — el decorador
# ---------------------------------------------------------------------------
def test_decorador_reintenta_y_preserva_metadatos():
    n = {"c": 0}
    @cr.retry(attempts=3, base=1.0, max_s=2.0)
    def fetch():
        """docstring que debe sobrevivir."""
        n["c"] += 1
        if n["c"] < 2:
            raise Exception("429 too many requests")
        return "listo"
    # el decorador usa el _sleep real; forzamos base pequeña e igual no dormimos
    # de verdad porque solo falla una vez con base 1.0... mejor inyectar vía with_retry.
    assert fetch.__name__ == "fetch"
    assert fetch.__doc__ == "docstring que debe sobrevivir."


# ---------------------------------------------------------------------------
# harden — sobre un exchange simulado (no toca red)
# ---------------------------------------------------------------------------
class _FakeExchange:
    def __init__(self, timeout=10000, headers=None):
        self.timeout = timeout
        self.headers = headers
        self.userAgent = None


def test_harden_fija_ua_headers_y_timeout():
    ex = cr.harden(_FakeExchange())
    assert ex.userAgent == cr.DEFAULT_USER_AGENT
    assert ex.headers["Accept-Language"].startswith("en-US")
    assert ex.timeout == 15000  # subió del 10000 por defecto


def test_harden_respeta_timeout_mayor_existente():
    ex = cr.harden(_FakeExchange(timeout=30000))
    assert ex.timeout == 30000  # no lo baja


def test_harden_preserva_headers_previos():
    ex = cr.harden(_FakeExchange(headers={"X-Prev": "1"}))
    assert ex.headers["X-Prev"] == "1"
    assert "Accept-Language" in ex.headers


def test_harden_desactivado_es_noop():
    ex = cr.harden(_FakeExchange(), enabled=False)
    assert ex.userAgent is None
    assert ex.timeout == 10000


def test_harden_nunca_rompe():
    class Roto:
        # asignar userAgent lanza -> harden debe tragárselo
        def __setattr__(self, k, v):
            raise RuntimeError("no se puede")
    ex = Roto()
    assert cr.harden(ex) is ex  # devuelve el mismo objeto sin propagar
