"""Filtrado de alertas por version del scoring (logica pura, sin Streamlit).

Mismo criterio que `backtest_runner.load_alerts`: por defecto se usan las
versiones scoring-compatibles vigentes (config.SCORING_COMPATIBLE_VERSIONS,
resuelto en cada llamada, nunca hardcodeado), pero el dashboard puede aislar
una version concreta o no filtrar en absoluto. Separado de `app.py` para poder
testear sin arrancar Streamlit (igual que `src/analysis/pnl.py`).
"""
from __future__ import annotations

import pandas as pd

from src import config

ALL_LABEL: str = "Todas"


def compatible_label(compatible: list[str] | None = None) -> str:
    """Etiqueta del selector para el grupo de versiones scoring-compatibles."""
    versions = compatible if compatible is not None else config.SCORING_COMPATIBLE_VERSIONS
    return f"Compatibles ({'+'.join(versions)})"


def version_filter_options(available_versions: list[str]) -> list[str]:
    """Opciones del selector: compatibles (default), cada version presente en los
    datos cargados y "Todas". Las versiones se resuelven contra
    config.SCORING_COMPATIBLE_VERSIONS en cada llamada, nunca hardcodeadas.
    """
    options = [compatible_label()]
    options.extend(sorted(set(available_versions)))
    options.append(ALL_LABEL)
    return options


def apply_version_filter(alerts: pd.DataFrame, choice: str) -> tuple[pd.DataFrame, int]:
    """Filtra `alerts` (columna `scoring_version`) segun la opcion elegida.

    `choice` es una de las devueltas por `version_filter_options`: la etiqueta
    de compatibles, "Todas", o una version concreta. Devuelve (df_filtrado,
    n_excluidas).
    """
    if alerts.empty:
        return alerts, 0
    if choice == compatible_label():
        mask = alerts["scoring_version"].isin(list(config.SCORING_COMPATIBLE_VERSIONS))
    elif choice == ALL_LABEL:
        mask = pd.Series(True, index=alerts.index)
    else:
        mask = alerts["scoring_version"] == choice
    excluded = int((~mask).sum())
    return alerts[mask], excluded
