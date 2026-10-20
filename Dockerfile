# Flywire payment gateway — production image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[gateway]" && useradd --create-home --uid 10001 gateway && mkdir -p /data && chown gateway /data

COPY sites.toml ./sites.toml
USER gateway
ENV GATEWAY_SITES_FILE=/app/sites.toml GATEWAY_DB_PATH=/data/gateway.db
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "flywire_checkout.gateway", "serve", "--host", "0.0.0.0", "--port", "8080"]
