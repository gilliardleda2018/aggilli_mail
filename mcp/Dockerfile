# Servidor MCP do Aggilli Mail. Roda sem privilégios e só fala com o mailserver pela rede interna.
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim
LABEL org.opencontainers.image.title="aggilli-mail-mcp"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MCP_DATA_DIR=/data MCP_HOST=0.0.0.0 MCP_PORT=8000
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --home /data mcp && mkdir -p /data && chown mcp /data && chmod 700 /data
USER mcp
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "aggilli_mail_mcp"]
