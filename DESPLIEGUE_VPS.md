# Despliegue en VPS

Guía breve para correr el sistema (collector, detector y dashboard) en un VPS y
recibir notificaciones en el móvil.

## Procesos

```bash
# Entorno
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Collector (snapshots de mercados de política a SQLite)
python -m src.collector.runner --loop 60

# Detector en tiempo real (WebSocket -> alertas)
python -m src.realtime.stream

# Dashboard
streamlit run src/dashboard/app.py
```

En un VPS conviene ejecutar el collector y el detector como servicios (systemd)
para que sobrevivan a reinicios; ambos escriben a la misma base de datos SQLite
(`data/polymarket_politics.db`, en modo WAL para lectura/escritura concurrente).

## Notificaciones por Telegram

El detector envía un mensaje privado de Telegram cada vez que una alerta supera
el umbral de score (`ALERT_THRESHOLD`), con push notification en el móvil.

Configuración (una sola vez):

1. Instala **Telegram** (App Store / Play Store) si no lo tienes.
2. En Telegram, busca **@BotFather**, crea un bot nuevo (`/newbot`) y copia el
   **TOKEN** que te da.
3. Reemplaza `TELEGRAM_BOT_TOKEN` en `src/config.py` con tu TOKEN real.
4. Busca tu nuevo bot por su username en Telegram y abre el chat.
5. Escribe **`/start`** en el chat privado con tu bot.
6. Obtén tu `chat_id` (un número largo). Desde el VPS:

   ```bash
   python -c "from src.realtime.stream import get_telegram_chat_id; print(get_telegram_chat_id())"
   ```

   Imprime tu `chat_id` leyendo el último mensaje que le enviaste al bot.
   (Requiere haber escrito `/start` antes.)
7. En `src/config.py`, rellena `TELEGRAM_CHAT_ID = "<el número>"`.
8. En el VPS: `git pull && systemctl restart pm-detector`.
9. Cuando el detector genere alertas, recibirás mensajes de Telegram en tiempo
   real con el mercado, el outcome, el tamaño en $, el score, el usuario y el lado.

Notas:
- Si `TELEGRAM_CHAT_ID` está sin configurar, el detector funciona igual pero no
  envía notificaciones (registra un WARNING).
- La notificación es un extra: la alerta siempre se guarda en la tabla `alerts`
  aunque el envío a Telegram falle (se registra un WARNING).
- El TOKEN del bot es un secreto: no lo compartas ni lo publiques.

## Paso 4: Ejecutar el backtest

Una semana despues de desplegar, cuando hayas acumulado **>=50 alertas** (cuantas
mas y mas mercados resueltos, mejor):

```bash
cd ~/Polymarket
source .venv/bin/activate
python -m src.backtest_runner              # todas las alertas
python -m src.backtest_runner --min-score 50  # solo las de score alto
```

Simula entrar en cada alerta en el mismo lado que el trader vigilado y cerrar
segun varias estrategias de salida (por resolucion del mercado, a 24h y a 1
semana). Genera un informe en `reports/backtest_FECHA.md` con:

- Tabla comparativa de estrategias (retorno medio, % rentables, Sharpe, drawdown).
- Curva de capital (ASCII) de la mejor estrategia.
- Predictividad por componente del score: retorno medio de las alertas en las que
  cada componente (wallet_fresca, longshot, tamaño...) puntuo. Sirve para saber
  que pesos suben y cuales bajar.
- Retorno por tramo de longshot (¿los precios <0.10 predicen mejor?).
- Conclusiones honestas: si los pesos no funcionan, lo dice; si una estrategia de
  salida gana claramente, lo muestra.

El backtest **solo lee de la BD** (`alerts`); no depende del detector en vivo. Se
puede ejecutar ahora con pocas alertas para validar la maquinaria (avisara de que
la muestra es pequeña y no concluyente), y de nuevo en una semana con datos
reales. Los precios de salida se obtienen de Gamma (final si el mercado esta
resuelto) y CLOB (serie historica para los horizontes); las alertas cuyo mercado
sigue abierto o sin historial quedan como "na" y no cuentan.

## Columna P&L en la pestaña Alertas

La columna **P&L** de la tabla de alertas muestra la ganancia o pérdida de cada
alerta al precio actual del mercado (shares = tamaño$ / precio de entrada, y la
diferencia contra el precio actual del outcome). Se refresca cada 60 segundos
(precios cacheados). En verde si es ganancia, rojo si es pérdida, gris si es
cero o no disponible. Si el mercado ya está resuelto, muestra **"Resuelto"** con
el payout final (las shares valen 1$ si el outcome ganó, 0$ si perdió); si el
mercado no se encuentra o la API falla, muestra **"N/A"**.
