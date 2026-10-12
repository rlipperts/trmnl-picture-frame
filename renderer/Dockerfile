# Official Playwright image: Chromium + all its system libs are already present
# and version-matched to the playwright pip package pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

# DejaVu is a sturdy, wide-coverage face that stays legible after the 1-bit
# threshold. Liberation gives us a Helvetica-metric alternative to fall back on.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    FRAME_CONFIG=/config/config.yaml

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
