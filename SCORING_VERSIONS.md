# Versiones del Scoring (2026-01 a presente)

Cada versión marca un cambio en cómo se decide si un trade genera alerta. El backtest exluye v1-v4 por defecto y usa solo v5+.

| Versión | Fecha      | Cambio                                                                       | Rationale                                                                 |
|---------|------------|------------------------------------------------------------------------------|---------------------------------------------------------------------------|
| **v1**  | 2026-01    | Perfilado activo (win_rate del perfil de la wallet vía Polygonscan)         | Fase 1 inicial. Asume win_rate fiable.                                    |
| **v2**  | 2026-02    | track_record: win_rate activo sobre source Polymarket (/positions)          | Intento de mejorar v1: si el win_rate NO es fiable, cambia la fuente.     |
| **v3**  | 2026-04    | track_record: desactivado (peso=0, nunca activo en scoring)                 | Descubrimiento: /positions tiene sesgo de supervivencia. win_rate falso.  |
| **v4**  | 2026-05    | RELEVANCE_TIERS: veto de relevancia económica escalonado por precio (Fase 2) | Prices extremos (0.01-0.35): requieren tamaño mínimo dinámico ($500-1.5k).|
| **v5**  | 2026-06    | Score NORMALIZADO por componentes evaluables (Fase 2.2)                     | Equipara el "peso" de diferentes branches del scoring (0-100 en todas).   |
| **v6**  | 2026-07    | PRICE_CEILING_VETO: veto por techo de precio (>= 0.96 o <= 0.04)            | Precio extremo = recorrido máximo < 4%. No hay espacio para trading info. |

## Detalles de v6 (Veto por techo de precio)

**Problema:** Análisis post-incidente de 3.544 alertas v5 reveló que 2.700 (76%) estaban a precio >= 0.96, con recorrido máximo < 4% antes de resolución. Patrón: market-making / arbitraje (BUY y SELL casi simultáneos al mismo precio y tamaño), no trading informado.

**Cambio:** Se agregan dos vetoes simétricos en `src/config.py`:
```python
PRICE_CEILING_VETO: float = 0.96   # precio >= esto -> veto
PRICE_FLOOR_VETO: float = 0.04     # precio <= esto -> veto (simetrico)
```

**Impacto:** Trade a precio 0.998 o 0.02 se descarta sin perfilar ni puntuar (mismo camino que el veto de relevancia económica). El trade se loguea en el contador `vetoed_by_price`.

**No afecta:** Rango medio (0.04 < precio < 0.96). Trades a precio 0.70 se procesan igual que en v5.

## Tabla wallet_trades (v6)

**Objetivo:** Reconstruir un win_rate REAL desde trades inmutables (a diferencia de `/positions`, que solo devuelve posiciones vivas y tiene sesgo de supervivencia).

**Uso futuro:**
- Señal BALLENA_VENDE: detectar cuándo la wallet que generó una alerta salió de la posición
- win_rate v2: calcular desde trades + resoluciones reales, no desde posiciones

**Estructura:** Nueva tabla en el esquema (src/analysis/wallet_trades.py):
```sql
CREATE TABLE wallet_trades (
    wallet TEXT, condition_id TEXT, transaction_hash TEXT,
    ts TEXT, side TEXT, trade_side TEXT, price REAL,
    size_shares REAL, size_usd REAL, outcome_index INTEGER,
    PRIMARY KEY (transaction_hash, wallet)
);
```

**Población:**
1. **Backfill manual:** `backfill_wallet(wallet)` desde src/analysis/wallet_trades_sync.py
2. **Refresco automático:** Cuando el detector genera alerta para wallet sin sync reciente (TTL=24h)

**Consulta:** `get_wallet_exit(wallet, condition_id, after_ts)` devuelve el primer SELL después de after_ts o None.

## Backtest: qué cambió

Filtro por defecto (en backtest_runner.py):
- **v5+:** incluidas (ok)
- **v1-v4:** excluidas (legacy)

Con v6, los trades a precio >= 0.96 o <= 0.04 no generan alerta (vetados antes de puntuar). Esto reduce el volumen de alertas v5→v6 en ~8-10% (los 2.700 trades de precio alto).

## Migración a v6

**Automática:** Las alertas nuevas se etiquetan con v6 al guardarlas. No hay migración de datos.

**Backtest:** Si necesitas comparar v5 vs v6:
```python
# v5 original
alerts_v5 = [a for a in all_alerts if a["scoring_version"] == "v5"]

# v6 con el mismo patrón de 3.544 trades
alerts_v6 = [a for a in all_alerts if a["scoring_version"] == "v6"]

# Diferencia: aproximadamente 2.700 menos (los vetados por precio)
print(f"v5: {len(alerts_v5)}, v6: {len(alerts_v6)}, diferencia: {len(alerts_v5) - len(alerts_v6)}")
```
