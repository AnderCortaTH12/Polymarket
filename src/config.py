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
    "track_record": 20,          # win-rate sospechosamente alto
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
# Longshot por tramos: un ticket pequeño a precio extremo es una conviccion
# grande en payout ($800 a 0.07 = ~11.400 shares), asi que el minimo en $ escala
# con lo extremo del precio. (precio_maximo, minimo_usd): se aplica el PRIMER
# tramo cuyo precio_maximo >= precio del trade. Precio > 0.35 => no aplica.
LONGSHOT_TIERS: list[tuple[float, float]] = [
    (0.10, 500.0),    # precio extremo: basta $500
    (0.20, 1_200.0),  # precio muy bajo: $1.200
    (0.35, 2_500.0),  # resto: el umbral original
]
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

# Score minimo para generar una alerta.
ALERT_THRESHOLD: int = 50

# Canal de ntfy.sh para notificaciones push al movil (ver DESPLIEGUE_VPS.md).
NTFY_CHANNEL: str = "polymarket-alerts-corta-2026"
