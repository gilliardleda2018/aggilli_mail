"""Ponto de entrada: `python -m aggilli_mail_mcp`."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .config import ConfigError, load_settings
from .server import build_app


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aggilli_mail_mcp").info("Iniciando o MCP do Aggilli Mail em %s (auth=%s, caixa=%s)",
                                               settings.public_url, settings.auth_mode, settings.mail_address)
    # proxy_headers: confia no X-Forwarded-* do Caddy (mesma rede Docker).
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level.lower(),
                proxy_headers=True, forwarded_allow_ips="*", server_header=False)


if __name__ == "__main__":
    main()
