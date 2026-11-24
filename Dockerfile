FROM python:3.12-slim

LABEL org.opencontainers.image.title="nutanix-prism-central-exporter" \
      org.opencontainers.image.description="Prometheus exporter for Nutanix Prism Central (API v4)" \
      org.opencontainers.image.source="https://github.com/motorbass0/nutanix-prism-central-exporter"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin exporter

WORKDIR /app
COPY prism_exporter.py .

USER 10001
EXPOSE 9421

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PRISM_EXPORTER_PORT', '9421') + '/health', timeout=4)" || exit 1

ENTRYPOINT ["python", "/app/prism_exporter.py"]
