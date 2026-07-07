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

## Notificaciones en el móvil (ntfy.sh)

El detector envía una notificación push cada vez que una alerta supera el umbral
de score (`ALERT_THRESHOLD`). Usa [ntfy.sh](https://ntfy.sh), un servicio
gratuito: el detector hace un POST a `https://ntfy.sh/{canal}` y quien esté
suscrito a ese canal recibe la notificación.

Para recibir notificaciones en el móvil:

1. Instala la app **ntfy.sh** (App Store / Play Store).
2. En la app, suscríbete al canal: **`polymarket-alerts-corta-2026`**
   (definido en `src/config.py` como `NTFY_CHANNEL`).
3. Cuando el detector genere alertas, recibirás notificaciones push con el
   título del mercado, el outcome, el score, el usuario, el tamaño en $ y el lado.

Notas:
- El canal es público: cualquiera que lo conozca puede suscribirse. Si quieres
  privacidad, cámbialo por un nombre difícil de adivinar en `NTFY_CHANNEL`.
- La notificación es un extra: la alerta siempre se guarda en la tabla `alerts`
  de la base de datos aunque el envío a ntfy falle (se registra un WARNING).
