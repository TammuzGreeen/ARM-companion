FROM python:3.12.10-slim-bookworm
WORKDIR /app
LABEL org.opencontainers.image.title="recognition-helper" org.opencontainers.image.version="0.3.7"
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 flac && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
COPY LICENSE /usr/share/doc/recognition-helper/LICENSE
RUN pip install --no-cache-dir -r requirements.txt
COPY recognition_helper ./recognition_helper
COPY tests ./tests
COPY backend_overlay ./backend_overlay
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "recognition_helper.main:app", "--host", "0.0.0.0", "--port", "8080"]
