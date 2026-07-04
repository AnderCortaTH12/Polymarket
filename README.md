# Polymarket Politics Dashboard

Sistema que monitoriza la sección **Politics** de Polymarket vía sus APIs REST
públicas (Gamma, CLOB, Data). Objetivo de investigación: analizar si seguir a
las "ballenas" de Polymarket Politics genera señales rentables. **No apostamos —
investigamos.**

Ver [PLAN.md](PLAN.md) para la arquitectura y el detalle de cada fase.

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

```bash
# Listar los mercados de política con más volumen
python -m src.client.gamma

# Tomar snapshots periódicos a la base de datos SQLite
python -m src.collector.runner --loop 60

# Lanzar el dashboard
streamlit run src/dashboard/app.py
```

## Estructura

- `src/client/`   — clientes de las APIs de Polymarket (gamma, clob, data_api)
- `src/collector/`— captura periódica de snapshots a SQLite
- `src/analysis/` — identificación de ballenas y señales (FASE 5-6, futuro)
- `src/dashboard/`— dashboard Streamlit
- `data/`         — bases de datos SQLite (no versionadas)
