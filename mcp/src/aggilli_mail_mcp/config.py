"""Configuração lida de variáveis de ambiente, validada na partida.

Um servidor mal configurado falha ao subir, com mensagem clara, e não no
primeiro uso. Referência das variáveis: mcp/README.md.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# Destinos de callback OAuth aceitos por padrão:
# - Aggilli Chat (LibreChat): retorno EXATO do servidor "aggillimail" do librechat.yaml;
# - claude.ai / claude.com: conector personalizado do Claude;
# - loopback: Claude Code e MCP Inspector.
AGGILLI_CHAT_CALLBACK = "https://chat.aggillintelligence.com.br/api/mcp/aggillimail/oauth/callback"
DEFAULT_REDIRECT_PREFIXES = (
    AGGILLI_CHAT_CALLBACK,
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:",
    "http://127.0.0.1:",
)

VALID_AUTH_MODES = ("oauth", "none")
MIN_ADMIN_PASSWORD_LENGTH = 16
EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")


class ConfigError(ValueError):
    """Configuração ausente ou inválida; a mensagem diz o que corrigir."""


@dataclass(frozen=True)
class Settings:
    # Caixa de email (uma só: a do administrador)
    mail_address: str
    mail_password: str
    imap_host: str = "mailserver"
    imap_port: int = 993
    smtp_host: str = "mailserver"
    smtp_port: int = 465
    mail_timeout: float = 30.0
    mail_verify_tls: bool = True
    from_name: str = "Aggilli Intelligence"

    # Servidor MCP
    public_url: str = "http://127.0.0.1:8000"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    timezone: str = "America/Sao_Paulo"

    # Autenticação
    auth_mode: str = "oauth"
    admin_password: str = ""
    data_dir: str = "/data"
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    allowed_redirect_prefixes: tuple[str, ...] = DEFAULT_REDIRECT_PREFIXES
    static_tokens: tuple[str, ...] = field(default_factory=tuple)

    # Limites das ferramentas
    max_body_chars: int = 20000
    max_search_results: int = 50
    max_recipients: int = 10
    max_sends_per_hour: int = 10
    max_sends_per_day: int = 50

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).netloc


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _require(name: str) -> str:
    value = _env(name)
    if value is None:
        raise ConfigError(f"A variável de ambiente {name} é obrigatória (ver mcp/.env.example).")
    return value


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    if value.lower() in ("1", "true", "yes", "sim", "on"):
        return True
    if value.lower() in ("0", "false", "no", "nao", "não", "off"):
        return False
    raise ConfigError(f"{name} deve ser true ou false, recebeu {value!r}.")


def _int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} deve ser um número inteiro, recebeu {value!r}.") from exc


def _csv(name: str) -> tuple[str, ...] | None:
    value = _env(name)
    if value is None:
        return None
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _clean_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{name} deve ser uma URL completa (https://...), recebeu {value!r}.")
    return value.rstrip("/")


def load_settings() -> Settings:
    auth_mode = (_env("MCP_AUTH_MODE", "oauth") or "oauth").lower()
    mail_host = _env("MAIL_HOST", "mailserver") or "mailserver"
    settings = Settings(
        mail_address=_require("MCP_MAIL_ADDRESS").lower(),
        mail_password=_require("MCP_MAIL_PASSWORD"),
        imap_host=_env("MCP_IMAP_HOST", mail_host) or mail_host,
        imap_port=_int("MCP_IMAP_PORT", 993),
        smtp_host=_env("MCP_SMTP_HOST", mail_host) or mail_host,
        smtp_port=_int("MCP_SMTP_PORT", 465),
        mail_timeout=float(_int("MCP_MAIL_TIMEOUT_SECONDS", 30)),
        mail_verify_tls=_bool("MCP_MAIL_VERIFY_TLS", True),
        from_name=_env("MCP_FROM_NAME", "Aggilli Intelligence") or "",
        public_url=_clean_url("MCP_PUBLIC_URL", _env("MCP_PUBLIC_URL", "http://127.0.0.1:8000") or ""),
        host=_env("MCP_HOST", "0.0.0.0") or "0.0.0.0",
        port=_int("MCP_PORT", 8000),
        log_level=(_env("MCP_LOG_LEVEL", "INFO") or "INFO").upper(),
        timezone=_env("MCP_TIMEZONE", "America/Sao_Paulo") or "America/Sao_Paulo",
        auth_mode=auth_mode,
        admin_password=_env("MCP_ADMIN_PASSWORD", "") or "",
        data_dir=_env("MCP_DATA_DIR", "/data") or "/data",
        access_token_ttl=_int("MCP_ACCESS_TOKEN_TTL_SECONDS", 3600),
        refresh_token_ttl=_int("MCP_REFRESH_TOKEN_TTL_SECONDS", 30 * 24 * 3600),
        allowed_redirect_prefixes=_csv("MCP_ALLOWED_REDIRECT_PREFIXES") or DEFAULT_REDIRECT_PREFIXES,
        static_tokens=_csv("MCP_STATIC_TOKENS") or (),
        max_body_chars=_int("MCP_MAX_BODY_CHARS", 20000),
        max_search_results=_int("MCP_MAX_SEARCH_RESULTS", 50),
        max_recipients=_int("MCP_MAX_RECIPIENTS", 10),
        max_sends_per_hour=_int("MCP_MAX_SENDS_PER_HOUR", 10),
        max_sends_per_day=_int("MCP_MAX_SENDS_PER_DAY", 50),
    )
    validate(settings)
    return settings


def validate(settings: Settings) -> None:
    """Regras que impedem subir um servidor inseguro por engano."""
    if settings.auth_mode not in VALID_AUTH_MODES:
        raise ConfigError(f"MCP_AUTH_MODE deve ser um de {VALID_AUTH_MODES}, recebeu {settings.auth_mode!r}.")
    if not EMAIL_RE.match(settings.mail_address):
        raise ConfigError(f"MCP_MAIL_ADDRESS inválido: {settings.mail_address!r}.")
    if settings.auth_mode == "oauth":
        if len(settings.admin_password) < MIN_ADMIN_PASSWORD_LENGTH:
            raise ConfigError(f"MCP_ADMIN_PASSWORD precisa ter ao menos {MIN_ADMIN_PASSWORD_LENGTH} caracteres: "
                              "é a senha digitada na tela de autorização.")
        if settings.admin_password == settings.mail_password:
            raise ConfigError("MCP_ADMIN_PASSWORD não pode ser igual à senha da caixa de email.")
        if not settings.public_url.startswith("https://") and not _is_loopback(settings.public_url):
            raise ConfigError("MCP_PUBLIC_URL precisa ser https:// fora de testes locais (o OAuth exige).")
    if settings.auth_mode == "none" and settings.host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("MCP_AUTH_MODE=none só é permitido com MCP_HOST=127.0.0.1.")
    for token in settings.static_tokens:
        if len(token) < 32:
            raise ConfigError("Cada token em MCP_STATIC_TOKENS precisa de ao menos 32 caracteres.")
    try:
        ZoneInfo(settings.timezone)
    except Exception as exc:
        raise ConfigError(f"MCP_TIMEZONE inválido: {settings.timezone!r}.") from exc
    if min(settings.max_recipients, settings.max_sends_per_hour, settings.max_sends_per_day,
           settings.max_search_results, settings.max_body_chars) <= 0:
        raise ConfigError("Limites (MCP_MAX_*) precisam ser maiores que zero.")
    if settings.access_token_ttl < 60 or settings.refresh_token_ttl < settings.access_token_ttl:
        raise ConfigError("TTLs inválidos: acesso >= 60s e refresh >= acesso.")


def _is_loopback(url: str) -> bool:
    return urlparse(url).hostname in ("127.0.0.1", "localhost", "::1")
