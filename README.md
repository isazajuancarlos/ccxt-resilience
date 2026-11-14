# ccxt-resilience

Endurece un cliente [ccxt](https://github.com/ccxt/ccxt) **legítimo** contra dos
molestias reales de operar contra un exchange:

1. **Falsos positivos del WAF.** Muchos exchanges ponen Cloudflare delante de su
   REST y, a ratos, challenguean con `403` a peticiones perfectamente válidas de
   tu propia cuenta.
2. **Errores transitorios de red** (`429`, timeouts, `ExchangeNotAvailable`) que
   se curan solos si esperas un momento y reintentas.

Son dos herramientas pequeñas y sin estado, que puedes usar por separado.

> **Alcance y ética.** Esto endurece **tu** cuenta contra la API **oficial** del
> exchange: mitiga falsos positivos del WAF sobre un cliente legítimo. **No evade
> control de acceso** ni resuelve challenges — si el WAF insiste, el error se
> re-lanza. Nació de un bot de trading propio sobre OKX tras varias rachas de
> `403` de Cloudflare, y se extrajo como librería. La técnica es genérica a
> cualquier exchange ccxt detrás de un WAF; el ajuste fino está probado contra OKX.

## Instalación

```bash
pip install ccxt-resilience          # ccxt es opcional
pip install "ccxt-resilience[ccxt]"  # trae ccxt para clasificar por tipo de excepción
```

`ccxt` es una **dependencia blanda**: sin él la librería funciona clasificando
los errores por el mensaje; con él, además reconoce los tipos de excepción de
ccxt (`DDoSProtection`, `RequestTimeout`, …).

## Uso

### 1. `harden` — que el WAF challengue menos

```python
import ccxt
from ccxt_resilience import harden

exchange = harden(ccxt.okx({
    "apiKey": ...,
    "secret": ...,
    "password": ...,
}))
```

`harden` ajusta un `User-Agent` de navegador, la cabecera `Accept-Language` y un
timeout mínimo holgado sobre un cliente **ya construido**. Devuelve el mismo
objeto (encadenable) y nunca rompe la construcción del exchange.

### 2. `with_retry` — reintentar solo lo que se debe

```python
from ccxt_resilience import with_retry

# Reintenta ante 403/Cloudflare/DDoS/timeout con backoff exponencial + jitter.
balance = with_retry(exchange.fetch_balance)

# Con argumentos y parámetros propios:
ohlcv = with_retry(exchange.fetch_ohlcv, "BTC/USDT", timeframe="1m",
                   attempts=4, base=1.0, max_s=8.0)
```

Lo importante es lo que **no** reintenta: un error de autenticación, de fondos o
de lógica se re-lanza en el acto. Reintentar un fallo real solo gasta tiempo y
enmascara el problema. Y si se agotan los intentos, se re-lanza la **última**
excepción, para que tu `try/except` de fail-safe siga gobernando.

Backoff: la espera del intento `i` es `min(max_s, base * 2**i) + rand()*base`.

También como decorador:

```python
from ccxt_resilience import retry

@retry(attempts=4)
def fetch_balance():
    return exchange.fetch_balance()
```

### Otras APIs (no solo exchanges)

`with_retry` acepta tu propio predicado de reintento:

```python
with_retry(mi_llamada, is_retryable=lambda e: "boom" in str(e))
```

## Configuración

Los defaults (`attempts=3`, `base=1.0 s`, `max_s=6.0 s`) se pasan por argumento
en cada llamada; no hay estado global. El `User-Agent`, `Accept-Language` y el
timeout mínimo de `harden` también son argumentos.

## Por qué reintentar de forma selectiva

Un `retry` que reintenta *cualquier* excepción es una trampa: convierte un error
de credenciales en una espera de 30 segundos que termina igual de mal, y esconde
bugs de lógica detrás de reintentos. Aquí la clasificación
(`is_transient_error`) es explícita, inspeccionable y sustituible.

## Tests

```bash
pip install "ccxt-resilience[test]"
pytest
```

17 tests, sin red y sin dormir de verdad (`_sleep`/`_rand` inyectados): el
backoff es determinista. Portados del bot que originó la librería —donde
probaron la matemática exacta del backoff en producción— y ampliados.

## Licencia

Apache-2.0.
