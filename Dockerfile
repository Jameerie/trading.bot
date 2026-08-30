# trading.bot - forex advisor
#
# The image is deliberately plain: the application has no runtime dependencies,
# so there is nothing to install and no build stage. slim + copy is the whole
# thing, which also keeps the attack surface close to "python and this code".

FROM python:3.12-slim

# Run as a non-root user. The container serves HTTP, so it should not be root
# even though nothing here writes outside its own directory.
RUN useradd --create-home --shell /bin/bash advisor

WORKDIR /app

COPY --chown=advisor:advisor src/ ./src/
COPY --chown=advisor:advisor config/ ./config/
COPY --chown=advisor:advisor data/samples/ ./data/samples/
COPY --chown=advisor:advisor pyproject.toml README.md ./

# Journals are written here; mount a volume to keep them across restarts.
RUN mkdir -p /app/reports && chown advisor:advisor /app/reports

USER advisor

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8787

# 0.0.0.0 inside the container is correct: the container boundary is the network
# boundary, and the published port decides who can actually reach it. Set
# TRADING_BOT_TOKEN when exposing it beyond localhost.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8787/api/health', timeout=4)"

ENTRYPOINT ["python", "-m", "trading_bot"]
CMD ["--config", "config/default.toml", "serve", "--host", "0.0.0.0", "--port", "8787"]
