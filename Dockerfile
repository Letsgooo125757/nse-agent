# NSE desk web app. Render/Fly/any Docker host: set the env vars listed in DEPLOY.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY nse_agent ./nse_agent
COPY data ./data
COPY templates ./templates
RUN pip install . && useradd --create-home --uid 10001 app && chown -R app /app
USER app

ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz',timeout=4)"
# --proxy-headers: Render terminates HTTPS in front of the app and passes the client IP on.
CMD ["sh", "-c", "uvicorn nse_agent.web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
