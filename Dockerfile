FROM python:3.13-slim

# DejaVu is the font image_generator.py falls back to on Linux
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simple_printer/ simple_printer/
COPY tests/ tests/
COPY pytest.ini .

# Non-root user; /data holds the printed-task state file (mount a volume here)
RUN useradd --create-home --uid 1000 printer \
    && mkdir /data \
    && chown printer:printer /data
USER printer

ENV STATE_PATH=/data/printed.json
VOLUME /data

CMD ["python", "-m", "simple_printer"]
