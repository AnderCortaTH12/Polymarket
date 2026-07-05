# FASE 6 — DETECCIÓN DE TRADING INFORMADO (SEÑALES)

Documento de diseño para Claude Code. Leer completo antes de escribir código.
Prerequisitos: Fases 0-5 funcionando, bugs del backlog de PLAN.md resueltos
(filtro politics, valor en $, Yes/No visible). NO empezar esta fase si esos
bugs siguen abiertos — las señales heredarían datos mal calculados.

---

## 1. OBJETIVO

Detectar, en segundos (no minutos), trades en mercados de política de
Polymarket que muestren el patrón de alguien operando con información
privilegiada, y registrar cada detección con timestamp para poder
backtestearla después. NO ejecutamos apuestas: solo detectamos, registramos
y medimos.

La hipótesis a validar con backtest: "si hubiera tomado la misma posición
que el trade flaggeado, N minutos después de detectarlo, al precio de ese
momento, ¿habría ganado dinero neto?"

---

## 2. POR QUÉ POLLING NO SIRVE Y QUÉ USAMOS EN SU LUGAR

Polling cada 5 min pierde la ventana: los casos reales documentados
(Maduro, Irán) muestran que el precio se ajusta en minutos tras la entrada
del insider. La solución tiene dos partes:

a) **WebSocket en vez de polling.** Polymarket expone un feed de trades en
   vivo: `wss://ws-live-data.polymarket.com`. Cada trade llega empujado en
   el momento en que ocurre, con la proxy wallet del trader. La suscripción
   requiere un mensaje con `action: "subscribe"` en el envelope (verificar
   formato exacto del protocolo al implementar; hay repos públicos que lo
   usan como referencia, ej. pselamy/polymarket-insider-tracker).
   Si el WebSocket cambia de formato o se cae, fallback: polling agresivo
   del endpoint /trades de la Data API cada 15-30s solo sobre los mercados
   con actividad reciente.

b) **Reloj de volumen en vez de reloj de pared** (concepto VPIN, de
   Easley/López de Prado/O'Hara). Las métricas de flujo NO se calculan
   "cada X minutos" sino "cada X dólares negociados" por mercado: se
   acumulan trades en cubos de igual volumen (ej. cubos de $5.000 por
   mercado, ajustable) y al cerrarse cada cubo se calcula el desequilibrio
   compra/venta. Cuando el mercado está muerto no se computa nada; cuando
   entra dinero fuerte, el muestreo se acelera solo. Esto responde
   exactamente al problema de "medir cada 5 min no es suficiente".

---

## 3. ARQUITECTURA: DOS CAPAS

### Capa lenta — perfiles de wallet (batch, cada 4-6 horas)

Proceso: `src/analysis/profiles.py`, ejecutado por cron/scheduler.
Para cada wallet vista en mercados de política (holders + trades
históricos), calcula y guarda en SQLite:

| Campo | Cálculo | Por qué |
|---|---|---|
| wallet_age_days | Fecha del primer trade o primera tx de funding (Polygonscan) | Wallets de horas/días de vida son la huella nº1 en los casos reales |
| total_volume_usd | Suma de sus trades en $ | Contexto de tamaño |
| n_markets | Nº de mercados distintos en los que ha operado | Concentración: el insider opera en 1-3 mercados, no en 50 |
| concentration | HHI o % del mercado principal sobre su volumen total | Ídem |
| win_rate | % de posiciones resueltas a su favor (solo mercados ya resueltos) | Un 98% de aciertos con >20 apuestas es estadísticamente imposible por suerte |
| n_resolved | Nº de posiciones resueltas (para saber si win_rate es significativo) | Un 100% con 2 apuestas no significa nada |
| longshot_wins | Nº de aciertos en posiciones tomadas a probabilidad <35% | Ganar longshots repetidamente = información, no suerte |
| funding_cluster_id | Del análisis de funding ya implementado (Fase 5.3) | Wallets hermanas comparten sospecha |
| avg_trade_size_usd | Media de sus trades | Para detectar tamaño anómalo relativo a sí misma |

Tabla: `wallet_profiles` (wallet PK, campos de arriba, updated_at).
El cálculo de win_rate requiere posiciones históricas resueltas: usar
Data API /positions + estado de resolución de mercados vía Gamma.

### Capa rápida — scoring en tiempo real (proceso permanente)

Proceso nuevo: `src/realtime/stream.py`. Es un servicio que:

1. Se conecta al WebSocket y se suscribe a trades.
2. Filtra: solo trades de mercados de política (mantener en memoria el
   set de condition_ids de política, refrescado cada 10 min desde Gamma).
3. Para cada trade entrante, calcula el SCORE (sección 4) en memoria:
   consulta el perfil precomputado de la wallet (lectura SQLite/caché) +
   estado del cubo de volumen del mercado. Milisegundos, sin llamadas
   HTTP en el camino crítico.
4. Si score >= umbral, escribe una fila en la tabla `alerts` con TODO el
   contexto (ver sección 5). Nunca borra alertas: son el dataset del
   backtest.
5. Mantiene por mercado el cubo de volumen VPIN-style: acumula trades
   firmados (compra Yes = +, venta Yes = -) hasta cerrar el cubo, calcula
   el imbalance del cubo, guarda en tabla `volume_buckets`.

Requisitos de robustez del servicio:
- Reconexión automática del WebSocket con backoff si se cae.
- Heartbeat: escribir cada minuto una fila en tabla `service_health`
  (ts, trades_procesados, ws_conectado) para que el dashboard muestre si
  el detector está vivo.
- Logging a fichero, no solo consola.
- Un solo proceso; no threads complejos. asyncio con websockets o
  aiohttp es suficiente.

---

## 4. EL SCORE COMPUESTO

Cada trade entrante recibe una puntuación 0-100 sumando componentes.
Los pesos iniciales son hipótesis razonables; el backtest los ajustará.
IMPLEMENTAR LOS PESOS COMO CONFIGURACIÓN (dict en un config.py o tabla),
no hardcodeados dispersos por el código.

| Componente | Condición | Puntos (inicial) |
|---|---|---|
| Wallet fresca | wallet_age_days < 7 (extra si < 2) | +25 (+10) |
| Sin perfil | wallet nunca vista antes (ni perfil) Y trade > $2.500 | +20 |
| Tamaño anómalo absoluto | trade > $10.000 en un solo mercado | +15 |
| Longshot con convicción | trade > $2.500 a probabilidad < 0.35 | +20 |
| Track record sospechoso | win_rate > 0.8 con n_resolved >= 10 | +20 |
| Cluster conocido | funding_cluster_id con >1 wallet | +10 |
| Concentración | concentration > 0.7 con total_volume > $20k | +10 |
| Flujo tóxico | imbalance del cubo de volumen actual > 0.75 en la misma dirección que el trade **Y** volumen del cubo >= MIN_BUCKET_VOLUME_FOR_TOXICITY_USD | +15 |
| Insensibilidad al precio | misma wallet acumulando el mismo lado en >=3 trades mientras el precio le sube en contra (ventana: últimos 3 cubos) | +15 |

**Nota sobre "flujo tóxico" (condición de volumen mínimo, ya implementada):**
un cubo con muy poco dinero acumulado puede dar un imbalance de ±1 (100%
direccional) por puro ruido estadístico — con $50 de un solo trade es trivial
que "todo" vaya en una dirección, y no significa nada. Por eso el componente
solo puntúa si `bucket_volume_usd >= MIN_BUCKET_VOLUME_FOR_TOXICITY_USD`
(definido en `src/config.py`, actualmente 1500$; el criterio es en $, no en
número de trades, para ser consistente con que los cubos se definen por
volumen en $). Si la dirección es fuerte pero el cubo no alcanza ese volumen,
el componente aporta 0 puntos y se marca `flujo_toxico_silenciado = True` en
el `ScoreBreakdown`, independientemente de cuán extremo sea el imbalance —
así queda trazado en la alerta por qué no puntuó, no solo que dio 0.

Umbral inicial de alerta: score >= 50. Guardar el score desglosado por
componente en la alerta (JSON), no solo el total — sin eso no se puede
ajustar pesos después.

IMPORTANTE — problema de tasa base: apuestas grandes y concentradas son
normales entre traders experimentados. El objetivo NO es minimizar falsos
negativos sino generar un dataset honesto de alertas con score, y que el
backtest diga qué combinaciones de componentes tienen valor predictivo.
No presumir de "detectar insiders": detectamos anomalías compatibles con
trading informado. Ese lenguaje también en docstrings y dashboard.

---

## 5. ESQUEMA DE DATOS NUEVO

```sql
CREATE TABLE wallet_profiles (
  wallet TEXT PRIMARY KEY,
  wallet_age_days REAL, total_volume_usd REAL, n_markets INTEGER,
  concentration REAL, win_rate REAL, n_resolved INTEGER,
  longshot_wins INTEGER, funding_cluster_id TEXT,
  avg_trade_size_usd REAL, updated_at TEXT
);

CREATE TABLE alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                -- momento de la DETECCIÓN (crítico para backtest)
  condition_id TEXT, market_question TEXT,
  wallet TEXT, side TEXT,          -- outcome comprado (Yes/No u otro)
  trade_size_usd REAL,
  price_at_detection REAL,         -- precio del outcome al detectar
  score_total INTEGER,
  score_breakdown TEXT,            -- JSON: {componente: puntos}
  bucket_imbalance REAL
);

CREATE TABLE volume_buckets (
  condition_id TEXT, bucket_seq INTEGER,
  ts_open TEXT, ts_close TEXT,
  volume_usd REAL, signed_imbalance REAL,  -- (-1..+1)
  price_open REAL, price_close REAL,
  PRIMARY KEY (condition_id, bucket_seq)
);

CREATE TABLE service_health (
  ts TEXT PRIMARY KEY, trades_processed INTEGER, ws_connected INTEGER
);
```

Nota de implementación: la tabla `alerts` real añade además las columnas
`username` (name/pseudonym del trader, resuelve el pendiente de username del
backlog) y `transaction_hash` (referencia auditable on-chain). El
`score_breakdown` incluye también `flujo_toxico_silenciado` (bool) para la
traza descrita en la sección 4.

---

## 6. BACKTEST (src/analysis/backtest.py)

Se ejecuta bajo demanda sobre las tablas `alerts` + histórico de precios
(CLOB /prices-history y/o snapshots propios). Para cada alerta:

1. Precio de entrada simulado = precio del outcome a los **D minutos**
   después de `ts` de la alerta (D configurable: probar 1, 5, 15, 60).
   NUNCA el precio en el momento de la alerta — ese es el error que
   invalida la mayoría de análisis publicados.
2. Resultado = payout del mercado al resolverse (1 o 0) o precio a
   horizonte fijo (24h, 72h) si se quiere medir sin esperar resolución.
3. Aplicar un coste de slippage conservador (ej. 1-2% + el spread
   registrado) sobre la entrada.
4. Métricas de salida: retorno medio por alerta, mediana, % alertas
   rentables, retorno agrupado POR COMPONENTE del score y por tramo de
   score (50-60, 60-75, 75+), y curva de capital simulada.
5. Salida: tabla + un informe markdown generado en reports/ con fecha.

Set de validación adicional: recopilar manualmente (yo, el usuario, lo
haré) 3-5 casos públicos documentados (Maduro enero 2026, mercados de
Irán feb-jun 2026) con wallet y fechas, y comprobar si el detector,
corrido retroactivamente sobre los trades históricos de esos mercados
(Data API /trades pagina hacia atrás), los habría flaggeado ANTES del
movimiento grande de precio. Si no pilla los casos obscenos conocidos,
los pesos están mal.

---

## 7. INTEGRACIÓN CON EL DASHBOARD

Pestaña nueva "Alertas":
- Tabla de alertas recientes (ts, mercado, wallet —con username si ya está
  implementado—, lado, tamaño, score, desglose expandible).
- Filtro por score mínimo y por mercado.
- Indicador de salud del detector (último heartbeat, trades/min).
- Sin emojis; coherente con el rediseño de interfaz del backlog.

---

## 8. ORDEN DE IMPLEMENTACIÓN Y CRITERIOS DE "HECHO"

1. **Capa lenta primero** (`profiles.py` + tabla wallet_profiles).
   Hecho cuando: perfiles calculados para todas las wallets del top 200
   por volumen, con win_rate verificable a mano contra 2-3 perfiles
   públicos de polymarket.com.
2. **Cubos de volumen** sobre datos históricos (sin WebSocket todavía):
   implementar el bucketing y el imbalance leyendo trades históricos de
   la Data API. Hecho cuando: para un mercado activo, la serie de cubos
   reproduce visiblemente los momentos de actividad.
3. **Servicio WebSocket** (`stream.py`) con scoring y tabla alerts.
   Hecho cuando: corre 24h seguidas sin caerse, con heartbeats continuos
   y alertas generadas con desglose completo.
4. **Backtest** con al menos 1 semana de alertas acumuladas + validación
   retroactiva sobre los casos públicos conocidos.
5. **Pestaña del dashboard.**

Un commit por paso mínimo. Tests: bucketing y scoring son funciones puras
— tests unitarios sin red obligatorios.

## 9. CONVENCIONES

Las mismas de PLAN.md (type hints, logging, timeouts, reintentos, URLs
como constantes, sin dependencias nuevas salvo `websockets` o `aiohttp`
para el stream — elegir una y justificarla en el commit).

---

## ESTADO DE IMPLEMENTACIÓN

- Paso 1 (capa lenta / perfiles): implementado — `src/analysis/profiles.py`.
- Paso 2 (cubos de volumen / VPIN): implementado — `src/analysis/buckets.py`.
- Paso 3a (scoring puro + pesos en config + tabla alerts): implementado —
  `src/analysis/scoring.py`, `src/config.py`, `src/realtime/storage.py`.
- Paso 3b (servicio WebSocket): implementado — `src/realtime/stream.py`.
  Dependencia nueva elegida: `websockets`.
- Paso 4 (backtest) y Paso 5 (pestaña dashboard): pendientes.
