FROM python:3.12-slim
# Noto fonts that calendar_render.py loads from /usr/share/fonts/truetype/noto/
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-core fonts-noto-mono \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# requirements.lock = pip freeze of the venv the desktop cron ran with
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY fonts/ fonts/
COPY calendar_render.py .
USER 65532:65532
ENTRYPOINT ["python", "/app/calendar_render.py"]
