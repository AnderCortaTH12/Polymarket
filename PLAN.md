# PLAN DE IMPLEMENTACIÓN — Polymarket Politics Dashboard

## Qué es este documento

Guía paso a paso para construir un sistema que monitoriza la sección **Politics** de Polymarket. Está pensado para dárselo a Claude Code como contexto y para que tú (desarrollador intermedio en Python) entiendas cada decisión.

**Objetivo final:** publicación en LinkedIn analizando si seguir a las "ballenas" de Polymarket Politics genera señales rentables. NO vamos a apostar — vamos a investigar.

**Repositorio:** [github.com/AnderCortaTH12/Polymarket](https://github.com/AnderCortaTH12/Polymarket)

---

## DECISIONES DE ARQUITECTURA

### ¿Por qué API y no scraping?

Polymarket expone 3 APIs REST públicas, sin API key, sin autenticación para lectura:

| API | URL base | Qué nos da |
|-----|----------|------------|
| **Gamma** | `gamma-api.polymarket.com` | Catálogo de eventos y mercados: precios, volumen, liquidez. Filtrable por `tag_slug=politics`. |
| **CLOB** | `clob.polymarket.com` | Series históricas de precio por token (outcome). WebSocket disponible para real-time. |
| **Data** | `data-api.polymarket.com` | Trades individuales, holders (posiciones abiertas) y wallets. La puerta a las ballenas. |

Scrapear el HTML sería más frágil (cambian el frontend sin aviso), más lento y probablemente contra sus términos de uso. El API da los mismos datos, estructurados y documentados.

**Límites a tener en cuenta:**
- Gamma cachea respuestas 30-60 segundos → nuestro "semi-real" no puede ser más rápido que eso.
- Rate limit general: ~15.000 requests / 10 segundos (generoso, no nos afecta en fase 1).
- Paginación: `limit` (máx. 500 por llamada) + `offset`.
- Algunos campos (`outcomes`, `outcomePrices`, `clobTokenIds`) vienen como strings JSON dentro del JSON → hay que parsearlos dos veces.

### ¿Por qué SQLite para almacenar?

- Cero infraestructura: es un fichero `.db` que se crea solo.
- Perfecto para acumular millones de filas en un portátil.
- Python lo incluye en la librería estándar (`sqlite3`).
- Cuando migremos a VPS, copiamos el fichero y ya.
- Si necesitamos análisis pesado más adelante, DuckDB lee SQLite directamente.

### ¿Por qué Streamlit para el dashboard?

- Se escribe en Python puro (mismo lenguaje que el resto del proyecto).
- En 50 líneas tienes una tabla interactiva con gráficas.
- Auto-refresco nativo.
- Cuando migres a VPS, Streamlit se sirve como web → accesible desde el móvil con la misma URL.
- Si algún día queremos algo más "producción", migramos a una web (Next.js/React), pero Streamlit nos deja validar la idea en horas, no semanas.

### ¿Dónde corre esto?

**Fase 1 (ahora):** en tu máquina local. Arrancas el colector y el dashboard manualmente.
**Fase 2 (cuando valides que funciona):** VPS barato (Hetzner ~4€/mes, o similar). El colector corre 24/7 con systemd, el dashboard se sirve por la misma máquina. Accesible desde tu móvil.
**Fase 3 (opcional):** si quieres publicarlo, le pones un dominio y un nginx delante del Streamlit.

---

## ESTRUCTURA DEL REPOSITORIO

```
polymarket-politics/
│
├── README.md                    # Descripción general del proyecto
├── PLAN.md                      # Este documento (contexto para Claude Code)
├── requirements.txt             # Dependencias Python
├── .gitignore                   # Excluye: *.db, __pycache__, .env, data/
├── .env.example                 # Variables de entorno (futuro: claves de Polygonscan, etc.)
│
├── src/
│   ├── __init__.py
│   │
│   ├── client/                  # Clientes de las APIs de Polymarket
│   │   ├── __init__.py
│   │   ├── gamma.py             # GET /events, /markets filtrado por politics
│   │   ├── clob.py              # GET /prices-history (series históricas)
│   │   └── data_api.py          # GET /trades, /holders, /positions (ballenas)
│   │
│   ├── collector/               # Captura periódica → SQLite
│   │   ├── __init__.py
│   │   ├── models.py            # Esquema de tablas SQLite
│   │   └── runner.py            # Loop principal: snapshot cada N segundos
│   │
│   ├── analysis/                # Tramos 3-4 (vacío por ahora, se llena después)
│   │   ├── __init__.py
│   │   ├── whales.py            # Identificación de ballenas
│   │   ├── wallets.py           # Cruce de wallets on-chain
│   │   └── signals.py           # Generación de señales
│   │
│   └── dashboard/               # Streamlit
│       ├── __init__.py
│       └── app.py               # Dashboard principal
│
├── data/                        # Bases de datos SQLite (NO se sube a git)
│   └── .gitkeep
│
├── notebooks/                   # Jupyter notebooks para exploración
│   └── 01_explorar_gamma.ipynb
│
└── tests/
    ├── __init__.py
    └── test_gamma.py            # Tests básicos del cliente
```

**Por qué esta estructura y no todo en un fichero:**
- `src/client/` separa la comunicación con APIs del resto → si Polymarket cambia un endpoint, solo tocas un fichero.
- `src/collector/` es independiente del dashboard → puede correr solo en el VPS sin Streamlit.
- `src/analysis/` está vacío a propósito → lo llenamos en tramos 3-4 sin reorganizar nada.
- `data/` fuera de git → las bases de datos pueden pesar gigas; no se versionan.

---

## FASES DE IMPLEMENTACIÓN

Cada fase tiene: qué hacer, por qué, qué ficheros toca, y cuándo está "hecho".

---

### FASE 0 — Setup del repo y entorno

**Repositorio:** `https://github.com/AnderCortaTH12/Polymarket`

**Qué hacer:**
1. Clonar el repo (`git clone https://github.com/AnderCortaTH12/Polymarket.git`).
2. Crear la estructura de carpetas de arriba dentro del clon.
3. Crear el `.gitignore` con: `*.db`, `__pycache__/`, `.env`, `data/*.db`, `*.pyc`.
4. Crear `requirements.txt` con las dependencias iniciales:
   - `requests` — para las llamadas HTTP a las APIs.
   - `pandas` — para manipular tablas de datos.
   - `streamlit` — para el dashboard.
5. Crear un entorno virtual (`python -m venv .venv`) e instalar dependencias.

**Por qué un entorno virtual:** para que las dependencias del proyecto no choquen con las de tu sistema. Claude Code lo crea y activa automáticamente.

**Hecho cuando:** puedes hacer `git clone`, `pip install -r requirements.txt` y no hay errores.

---

### FASE 1 — Cliente del Gamma API

**Qué hacer:** `src/client/gamma.py`

1. Función `get_politics_events()`:
   - GET a `https://gamma-api.polymarket.com/events`
   - Parámetros: `tag_slug=politics`, `active=true`, `closed=false`, `order=volume24hr`, `ascending=false`
   - Paginación con `limit=100` y `offset` incrementando hasta que venga una página con menos de 100 resultados.
   - Reintentos con backoff exponencial si recibe HTTP 429 (rate limit).

2. Función `flatten_markets(events)`:
   - Cada evento contiene N mercados. Aplana la estructura para tener una fila por mercado.
   - Parsea los campos que vienen como strings JSON: `outcomes`, `outcomePrices`, `clobTokenIds`.
   - Extrae los campos útiles: question, outcomes, precios, volumen 24h, liquidez, spread, fecha de cierre, URL.

3. Un `if __name__ == "__main__"` que llame a ambas funciones e imprima los 10 mercados con más volumen.

**Detalle técnico importante:** `outcomePrices` viene como `'["0.62","0.38"]'` (string de un array de strings de números). Hay que hacer `json.loads()` y luego `float()` sobre cada elemento.

**Hecho cuando:** ejecutas `python -m src.client.gamma` y ves en terminal una lista de mercados reales de política con sus probabilidades.

**Test:** `tests/test_gamma.py` — crea un evento falso con los campos como strings JSON y verifica que `flatten_markets` lo parsea correctamente. Esto no necesita internet.

---

### FASE 2 — Colector y SQLite

**Qué hacer:** `src/collector/models.py` + `src/collector/runner.py`

1. `models.py` — define el esquema:
   ```
   Tabla: snapshots
   Columnas:
     - ts (TEXT, ISO UTC) — cuándo se tomó el snapshot
     - market_id (TEXT) — ID único del mercado
     - condition_id (TEXT) — necesario para la Data API después
     - event_slug (TEXT)
     - question (TEXT)
     - outcomes (TEXT, JSON)
     - outcome_prices (TEXT, JSON)
     - best_bid, best_ask, spread (REAL)
     - last_trade_price (REAL)
     - volume_24h, volume_total, liquidity (REAL)
     - end_date (TEXT)
     PRIMARY KEY (ts, market_id)
     INDEX en (market_id, ts) — para consultas "dame la historia de este mercado"
   ```

2. `runner.py`:
   - Función `take_snapshot(db)` — llama a `get_politics_events()`, aplana, inserta en SQLite.
   - `main()` con argparse: `--loop N` para repetir cada N segundos (mínimo 30), o un solo snapshot si no se pasa.
   - Logging claro: cuántos mercados capturó, cuánto tardó, si hubo errores.

**Por qué la primary key es (ts, market_id):** cada snapshot es una foto del estado de cada mercado en un momento dado. Con esto puedes reconstruir la evolución completa de cualquier mercado.

**Hecho cuando:** ejecutas `python -m src.collector.runner --loop 60`, dejas 5 minutos, y al abrir la base de datos ves 5 snapshots × N mercados filas.

---

### FASE 3 — Cliente CLOB (histórico de precios)

**Qué hacer:** `src/client/clob.py`

1. Función `get_price_history(clob_token_id, interval, fidelity)`:
   - GET a `https://clob.polymarket.com/prices-history`
   - Parámetros: `market` (el token ID, NO el market ID — cuidado aquí), `interval` (1h/6h/1d/1w/1m/max), `fidelity` (resolución en minutos).
   - Devuelve lista de `{t: unix_timestamp, p: precio}`.

**Por qué esto es separado del Gamma:** Gamma te da el precio actual. CLOB te da la curva histórica. Para las gráficas del dashboard necesitas CLOB. Para las señales del tramo 4 necesitas tu propio histórico (collector), no CLOB, porque CLOB no guarda todo y tú controlas la granularidad.

**Hecho cuando:** le pasas un `clob_token_id` real (sacado de un evento de la fase 1) y te devuelve una serie temporal con sentido.

---

### FASE 4 — Dashboard Streamlit

**Qué hacer:** `src/dashboard/app.py`

El dashboard tiene 3 secciones:

**Sección 1 — Vista general (tabla):**
- Tabla de todos los mercados de política activos.
- Columnas: Mercado, Prob. (barra de progreso), Spread, Volumen 24h, Liquidez, Link.
- Ordenable por cualquier columna (por defecto: volumen 24h descendente).
- Buscador de texto (filtra por pregunta o título del evento).
- Filtro de volumen mínimo.
- Métricas en cabecera: total mercados, volumen 24h agregado, liquidez total, hora de última actualización.

**Sección 2 — Detalle de mercado:**
- Selector (dropdown) con la lista de mercados.
- Gráfica de precio histórico (datos de CLOB `/prices-history`).
- Selector de rango temporal: 1d, 1w, 1m, max.
- Si existe la base de datos del collector, superponer o mostrar debajo tu histórico propio.
- Métricas: probabilidad actual, volumen 24h, liquidez.
- Botón "Ver en Polymarket" que abre la URL.

**Sección 3 — Panel de control (sidebar):**
- Slider de auto-refresco (30-300 segundos).
- Toggle para activar/desactivar auto-refresco.
- Slider de "eventos a cargar" (50-500).
- Estado del collector: ¿existe la .db? ¿Cuántos snapshots tiene? ¿Último snapshot hace cuánto?

**Auto-refresco:** Streamlit tiene `st.rerun()`. Tras cada render, si el toggle está activo, esperamos N segundos y refrescamos. Los datos del API se cachean con `@st.cache_data(ttl=30)` para no repetir llamadas innecesarias.

**Hecho cuando:** abres el navegador, ves la tabla, buscas "trump", seleccionas un mercado y ves su gráfica actualizada.

---

### FASE 5 — Data API y esqueleto de ballenas (Tramo 2 del proyecto)

**Qué hacer:** `src/client/data_api.py` + `src/analysis/whales.py`

*Esta fase la detallamos cuando las 4 anteriores estén funcionando. Por ahora, solo el esqueleto:*

1. `data_api.py`:
   - `get_market_trades(condition_id)` — últimos trades de un mercado, incluye wallet proxy.
   - `get_market_holders(condition_id)` — mayores posiciones abiertas.
   - `get_user_positions(proxy_wallet)` — cartera completa de una wallet.

2. `whales.py`:
   - Identificar las N wallets con mayor posición en mercados de política.
   - Cruzar con historial on-chain vía Polygonscan API (necesitará API key gratuita).
   - Detectar wallets que comparten la misma fuente de fondos (mismo funding wallet).

---

### FASE 6 — Señales y backtest (Tramo 3-4 del proyecto)

*Se diseña cuando tengamos semanas de datos acumulados. Ideas iniciales:*

- Movimiento anómalo de probabilidad (>X% en <Y minutos).
- Entrada de volumen inusual (>Z desviaciones estándar sobre la media).
- "Smart money": cuando las ballenas identificadas en fase 5 abren posición, ¿el mercado se mueve después en su dirección?
- Divergencia: el precio dice una cosa pero las posiciones de los grandes dicen otra.

Backtest sobre el histórico de tu collector para responder: *si hubieras seguido estas señales, ¿habrías ganado dinero?*

---

## INSTRUCCIONES PARA CLAUDE CODE

Cuando le pases este plan a Claude Code, dale estas instrucciones:

```
Lee PLAN.md completo antes de empezar. Implementa fase por fase, en este orden.
Cada fase es un commit (o varios commits pequeños). No avances a la siguiente
fase sin que la actual funcione.

Convenciones:
- Python 3.11+
- Type hints en todas las funciones
- Docstrings breves explicando qué hace cada función
- Logging con el módulo `logging` (no prints sueltos)
- Las llamadas HTTP siempre con timeout (15s) y reintentos (3, backoff exponencial)
- Los campos que Polymarket devuelve como strings JSON se parsean en el cliente,
  nunca más arriba
- La base de datos SQLite se guarda en data/polymarket_politics.db
- No hardcodear URLs: definirlas como constantes al inicio del módulo

Para cada fase, al terminar:
1. Verifica que el código funciona (ejecuta el script/test)
2. Haz commit con mensaje descriptivo
3. Muéstrame el output
```

---

## DEPENDENCIAS

```
# requirements.txt
requests>=2.31        # HTTP client
pandas>=2.0           # Tablas de datos
streamlit>=1.35       # Dashboard
```

No añadir más dependencias a menos que sea estrictamente necesario. En particular:
- NO instalar ORMs (SQLAlchemy) — sqlite3 de la stdlib es suficiente.
- NO instalar frameworks de testing pesados — unittest de la stdlib vale.
- NO instalar librerías de gráficas aparte — Streamlit incluye las suyas (st.line_chart, st.bar_chart).

---

## GLOSARIO (para no perderte)

| Término | Qué es |
|---------|--------|
| **Evento** | Pregunta general: "¿Quién ganará las elecciones de 2028?" |
| **Mercado** | Outcome concreto tradeable dentro de un evento. Un evento binario tiene 1 mercado; uno multioutcome tiene N. |
| **Condition ID** | Identificador del mercado en los smart contracts. Necesario para la Data API. |
| **CLOB Token ID** | Identificador de cada outcome (Yes/No) en el libro de órdenes. Hay 2 por mercado binario. Necesario para el histórico de precios. |
| **Proxy wallet** | Polymarket crea una wallet proxy por cada usuario. Los trades van desde esta wallet, no desde la principal del usuario. |
| **outcomePrices** | Array de probabilidades implícitas. En un binario: `[0.62, 0.38]` = 62% Yes, 38% No. Suman ~1. |
| **Tag slug** | Etiqueta para filtrar: `politics`, `sports`, `crypto`, etc. |
| **Snapshot** | Foto del estado de todos los mercados en un instante. Lo que guarda el collector. |
