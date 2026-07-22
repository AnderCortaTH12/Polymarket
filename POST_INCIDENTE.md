# Arreglos post-incidente (2026-07-22): Disco lleno en VPS

Tras el incidente del 22-julio-2026 donde el disco del VPS se llenó al 100% por snapshots sin filtrar en el collector (~74.3M filas, 33GB), se implementaron 4 arreglos estructurales.

## 1. Filtro compartido de política (bug raíz)

**Problema:** El collector escribía snapshots para ~14.000 mercados mientras que el detector solo procesaba ~40-5.400 mercados de política reales. El servidor Gamma devuelve más mercados de los que debería al filtrar por `tag_slug=politics` (bug conocido).

**Solución:** Función `get_political_condition_ids()` en `src/client/gamma.py` que obtiene el set definitivo de condition_ids de política. Tanto collector como detector la usan ahora, garantizando exactamente los mismos mercados.

```python
from src.client.gamma import get_political_condition_ids
politics_ids = get_political_condition_ids()  # set de ~40-5000 condition_ids
```

**Cambios:**
- `src/client/gamma.py`: Nueva función `get_political_condition_ids(events=None, only_tradeable=True)`
- `src/collector/runner.py`: Filtra snapshots solo para mercados en `politics_ids`
- `src/realtime/stream.py`: Usa la función compartida en `refresh_context()`

**Tests:** 4 nuevos en `tests/test_gamma.py` (TestPoliticalConditionIds)

## 2. Retención + purga automática de snapshots

**Problema:** Sin límite de retención, los snapshots acumulaban indefinidamente, llenando el disco.

**Solución:** Módulo `src/collector/retention.py` con `purge_old_snapshots(conn, days=30)` que borra filas más antiguas que N días (defecto 30).

**Uso:**

```bash
# Snapshot único sin retención
python -m src.collector.runner

# Loop con retención automática (30 días por defecto)
python -m src.collector.runner --loop 60 --retention-days 30

# Retención personalizada
python -m src.collector.runner --loop 60 --retention-days 14
```

La purga se ejecuta automáticamente **una vez al día** (no en cada iteración de 60s) para evitar overhead. La próxima ejecución se calcula para las 00:00 UTC.

**Tests:** 3 nuevos en `tests/test_retention.py` (TestPurgeOldSnapshots)

## 3. Rotación de logs (detector.log)

**Problema:** El archivo `logs/detector.log` crecía sin límite.

**Solución:** `RotatingFileHandler` en la configuración de logging con:
- **maxBytes:** 20 MB por archivo
- **backupCount:** 5 archivos rotados (detector.log, detector.log.1, ..., detector.log.5)

El archivo se rota automáticamente cuando alcanza 20 MB, y se mantienen los últimos 5 rotados (~100 MB total de histórico).

**Ubicación:** `src/realtime/stream.py` → función `configure_logging()`

**Tests:** 1 nuevo en `tests/test_stream.py` (TestLoggingConfiguration)

## 4. Alerta de disco por Telegram (85%)

**Problema:** Sin alertas, el equipo no se enteraba del disco lleno hasta que era demasiado tarde.

**Solución:** Módulo `src/system/health.py` que:
1. Chequea uso de disco cada 10 minutos (desde el heartbeat del detector)
2. Si supera 85%, envía alerta por Telegram
3. Deduplica alertas: máximo 1 cada 6 horas del mismo problema

**Integración:** Automática en el detector. El heartbeat loop incluye un contador que chequea disco cada 600s.

```python
# En src/realtime/stream.py._heartbeat_loop()
if self._disk_check_count >= 10:  # cada 600s / 60s
    await self._check_disk_and_alert()
```

**Tests:** 8 nuevos en `tests/test_health.py` (check_disk_usage, should_alert_disk_usage, log_disk_alert)

## Esquema de BD: cambios

Se agregaron columnas opcionales a `service_health` para registrar alertas del sistema:
- `service` (TEXT): nombre del servicio (ej. "disk_monitor")
- `status` (TEXT): ok, warning, error
- `message` (TEXT): detalles de la alerta

Se migran automáticamente con `_ensure_service_health_columns()` al conectar.

## Verificación

```bash
# Ejecutar todos los tests (175 totales)
python -m pytest tests/ -q

# Tests específicos por arreglo
python -m pytest tests/test_gamma.py::TestPoliticalConditionIds -v
python -m pytest tests/test_retention.py -v
python -m pytest tests/test_stream.py::TestLoggingConfiguration -v
python -m pytest tests/test_health.py -v
```

## Pasos al reactivar el collector

1. **Verificar filtro:** El nuevo snapshot debe registrar ~10-50 mercados (política), NO ~14.000.
   ```bash
   python -m src.collector.runner  # un solo snapshot de prueba
   sqlite3 data/polymarket_politics.db "SELECT COUNT(DISTINCT condition_id) FROM snapshots;"
   ```

2. **Iniciar loop con retención:**
   ```bash
   python -m src.collector.runner --loop 60 --retention-days 30 &
   ```

3. **Monitorear disco:**
   - Ver logs en `logs/detector.log` (rotados cada 20 MB)
   - Recibir alertas en Telegram si uso > 85%
   - Verificar BD: `sqlite3 data/polymarket_politics.db "SELECT * FROM service_health WHERE service='disk_monitor' ORDER BY ts DESC LIMIT 10;"`

## Rollback

Si es necesario revertir:
```bash
git log --oneline | head -5
git revert <commit-hash>  # revertir de forma segura sin perder histórico
```

**NOT:** No usar `git reset --hard` en producción; usa `git revert` para mantener auditoría.
