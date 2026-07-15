"""Configuracion del scoring de la Fase 6 (pesos y umbrales editables).

Centraliza aqui los pesos de cada componente del score y los umbrales que los
disparan, para poder ajustarlos con el backtest sin tocar la logica. Las claves
de SCORE_WEIGHTS coinciden con los campos de `ScoreBreakdown`.
"""
from __future__ import annotations

# Pesos de cada componente del score (0-100 en total, ajustables por backtest).
SCORE_WEIGHTS: dict[str, int] = {
    "wallet_fresca": 25,          # wallet con pocos dias de vida
    "wallet_fresca_extra": 10,    # extra si es MUY fresca (< VERY_FRESH_DAYS)
    "sin_perfil": 20,             # wallet nunca vista + trade relevante
    "tamano_anomalo": 15,         # trade absoluto muy grande
    "longshot": 20,              # conviccion en un outcome improbable
    # track_record DESACTIVADO (peso 0) en la Fase 1c: la fuente del win_rate no
    # es valida. El endpoint /positions de Polymarket solo devuelve posiciones
    # VIVAS; todo lo cerrado (ganado y cobrado, o perdido a $0) desaparece, asi
    # que el win_rate calculado desde ahi mide una urna de la que se han retirado
    # todas las derrotas (sale ~1.0). No es un bug de parametros: el dato no
    # existe en la fuente. NO reactivar sin reconstruir antes el win_rate desde
    # los TRADES cruzados con la resolucion real (ver "Limitacion conocida" en
    # FASE6.md). El campo se conserva en ScoreBreakdown (siempre 0) por esquema.
    "track_record": 0,           # DESACTIVADO: win_rate no calculable desde /positions
    "cluster": 10,               # funder compartido con otras wallets
    "concentracion": 10,         # opera casi solo en un mercado
    "flujo_toxico": 15,          # el cubo de volumen empuja fuerte en su direccion
    "insensibilidad_precio": 15,  # acumula el mismo lado aunque el precio le sube en contra
}

# Umbrales que disparan cada componente.
FRESH_WALLET_DAYS: float = 7.0
VERY_FRESH_WALLET_DAYS: float = 2.0
NO_PROFILE_MIN_TRADE_USD: float = 2_500.0
BIG_TRADE_USD: float = 10_000.0
# Tramos de RELEVANCIA ECONOMICA por precio. UNA SOLA tabla, dos usos (Fase 2):
#   a) el VETO de relevancia (portero): si trade_usd < suelo del tramo, el trade
#      NO se puntua ni puede generar alerta (ver relevance_floor y stream.py).
#   b) el componente `longshot` del score (solo para price <= 0.35): puntua si
#      trade_usd >= suelo de su tramo.
# Derivar ambos de esta misma tabla evita que veto y longshot se contradigan.
# (precio_maximo, minimo_usd): se aplica el PRIMER tramo cuyo precio_maximo >=
# precio del trade. El ultimo tramo (1.01) cubre cualquier precio > 0.35.
# Racional: un ticket pequeño a precio extremo es alta conviccion en payout
# ($800 a 0.07 = ~11.400 shares); a precio 0.50 el mismo importe es ruido.
RELEVANCE_TIERS: list[tuple[float, float]] = [
    (0.10, 500.0),    # precio <= 0.10 -> minimo $500 (caso Maduro: $800 a 0.07)
    (0.20, 1_500.0),  # precio <= 0.20 -> minimo $1.500
    (0.35, 1_500.0),  # precio <= 0.35 -> minimo $1.500
    (1.01, 3_000.0),  # precio > 0.35  -> minimo $3.000 (1.01 cubre todo)
]

# Precio maximo para el que el componente `longshot` puede puntuar. Los tramos
# de RELEVANCE_TIERS por encima de esto solo sirven para el veto, no para longshot.
LONGSHOT_MAX_PRICE: float = 0.35


def relevance_floor(price: float | None) -> float:
    """Suelo de relevancia economica ($) para un trade a este precio.

    Devuelve el min_usd del PRIMER tramo de RELEVANCE_TIERS cuyo precio_maximo
    cubre `price`. De aqui salen TANTO el veto (trade_usd < suelo -> descartar)
    COMO el componente longshot (trade_usd >= suelo del tramo, si price <= 0.35).
    Si el precio es None o invalido, se usa el tramo mas alto (el mas exigente).
    """
    if price is None:
        return RELEVANCE_TIERS[-1][1]
    for price_max, min_usd in RELEVANCE_TIERS:
        if price <= price_max:
            return min_usd
    return RELEVANCE_TIERS[-1][1]


SUSPICIOUS_WIN_RATE: float = 0.8
MIN_RESOLVED_FOR_WINRATE: int = 10
CONCENTRATION_MIN: float = 0.7
CONCENTRATION_MIN_VOLUME_USD: float = 20_000.0
TOXIC_IMBALANCE: float = 0.75
# Volumen minimo acumulado en el cubo para fiarse de su imbalance: con poco
# dinero (ej. un solo trade de $50) un imbalance de ±1 es ruido estadistico, no
# señal. En $ para ser consistente con que los cubos se definen por volumen en $.
MIN_BUCKET_VOLUME_FOR_TOXICITY_USD: float = 1_500.0
INSENSIBILITY_MIN_STREAK: int = 3

# Fase 2 (defecto 2): NORMALIZACION del score por componentes evaluables.
# El score bruto es una suma de techos dispares por rama (sin-perfil llega a ~85,
# fresca a ~120), asi que un bruto de 60 no significa lo mismo en cada caso y no
# es comparable. Se normaliza a 0-100 dividiendo el bruto por el TECHO de los
# componentes EVALUABLES del trade.
#
# EVALUABLE = sus precondiciones ESTRUCTURALES se cumplen, AUNQUE de 0 puntos.
# Tres estados: (a) no aplica -> fuera del techo (imposible estructuralmente);
# (b) aplica y no se activa -> DENTRO del techo (un "no" informativo, suma 0);
# (c) aplica y se activa -> dentro del techo, suma sus puntos. La distincion es
# estructural (que podia pasar), no de conducta (que paso).
#
# Las reglas reciben un `ctx` (dict) con el contexto ya extraido por el scorer:
#   has_profile: bool         -> la wallet tiene perfil (toda wallet tiene edad)
#   price: float | None       -> precio del outcome operado
#   cluster_id: str | None    -> funding_cluster_id del perfil (comparte funder)
#   total_volume_usd: float   -> volumen total historico de la wallet
#   same_side_streak: int     -> racha de mismo-lado en el mercado (>=1 si hubo)
# track_record NO aparece: peso 0, nunca entra en el techo (ni evaluable ni suma).
from typing import Callable  # noqa: E402

EVALUABILITY_RULES: dict[str, Callable[[dict], bool]] = {
    "wallet_fresca": lambda c: c["has_profile"],
    "sin_perfil": lambda c: not c["has_profile"],
    "tamano_anomalo": lambda c: True,
    "longshot": lambda c: c["price"] is not None and c["price"] <= LONGSHOT_MAX_PRICE,
    "cluster": lambda c: bool(c["cluster_id"]),
    "concentracion": lambda c: c["total_volume_usd"] > CONCENTRATION_MIN_VOLUME_USD,
    "flujo_toxico": lambda c: True,
    "insensibilidad_precio": lambda c: c["same_side_streak"] >= 1,
}


def component_ceiling(component: str) -> int:
    """Puntos MAXIMOS que un componente puede aportar al techo.

    Igual a su peso en SCORE_WEIGHTS, salvo wallet_fresca, que puede sumar el peso
    base MAS el extra de wallet muy fresca (el maximo alcanzable por esa rama).
    """
    if component == "wallet_fresca":
        return SCORE_WEIGHTS["wallet_fresca"] + SCORE_WEIGHTS["wallet_fresca_extra"]
    return SCORE_WEIGHTS.get(component, 0)


# Umbral de alerta, ahora como PORCENTAJE (0-100) sobre el score NORMALIZADO.
# Se mantiene en 50 por ahora; se afinara con el backtest cuando haya alertas v5.
ALERT_THRESHOLD: int = 50

# Perfilado bajo demanda en el detector (Fase 1). El detector construye el
# perfil de una wallet EN EL MOMENTO (Data API + Polygonscan) solo si el trade
# merece la pena: o es grande (>= PROFILING_MIN_TRADE_USD) o ya cumple un tramo
# de LONGSHOT_TIERS (para no perder entradas tipo "$800 a 0.07"). Los perfiles
# se cachean y se reconstruyen si superan PROFILE_TTL_HOURS.
PROFILING_MIN_TRADE_USD: float = 2_500.0
PROFILE_TTL_HOURS: float = 24.0

# Version del scoring. Las alertas se etiquetan con esto para que el backtest no
# mezcle scorings incompatibles: v1 (perfilado inactivo), v2 (track_record activo
# sobre un win_rate falso), v3 (track_record desactivado, Fase 1c), v4 (veto de
# relevancia escalonado por precio, Fase 2) y v5 (score NORMALIZADO por
# componentes evaluables: cambia COMO se decide la alerta). El backtest y el
# dashboard filtran a v5 por defecto.
SCORING_VERSION: str = "v5"

# Notificaciones por Telegram (ver DESPLIEGUE_VPS.md). El chat_id es el ID
# privado del usuario; se obtiene tras escribir /start al bot (paso en la doc).
TELEGRAM_BOT_TOKEN: str = "8780060569:AAHCElYk_wKHvtJ-xaj697C28wvyCbjQLmc"
TELEGRAM_CHAT_ID = 6058306383  # rellenar con tu chat_id (ver DESPLIEGUE_VPS.md)
