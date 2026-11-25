"""Servidor de autorização OAuth 2.1 embutido, para um único dono.

Por que existe: o conector personalizado do claude.ai só aceita servidores
sem autenticação ou com OAuth com registro dinâmico de clientes (DCR). Um
servidor sem autenticação exporia a caixa de email a quem descobrisse a URL.
O SDK do MCP já implementa os endpoints OAuth (metadados, /register,
/authorize, /token, /revoke); aqui fica só o "provedor": onde guardar
clientes e tokens, e a tela em que o dono confirma o acesso com a senha
MCP_ADMIN_PASSWORD. Copiado do mcp_nextcloud (ADR-002 de lá), mesmo modelo.

Garantias:
- tokens e códigos são guardados apenas como hash SHA-256 (um vazamento do
  banco não entrega tokens válidos);
- códigos de autorização valem 5 minutos e são de uso único;
- refresh tokens são rotacionados a cada uso;
- só destinos de callback da lista MCP_ALLOWED_REDIRECT_PREFIXES são aceitos
  no registro de clientes;
- tentativas erradas de senha travam a tela com espera exponencial.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings

log = logging.getLogger(__name__)

SCOPE = "email"
CODE_TTL = 300
PENDING_TTL = 600
MAX_FREE_FAILURES = 5
MAX_LOCK_SECONDS = 3600


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _Store:
    """SQLite simples; o volume Docker /data o mantém entre reinícios."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        # umask 077 durante a criação: banco, -wal e -shm nascem legíveis só pelo dono.
        old_umask = os.umask(0o077)
        try:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._init_schema()
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS codes   (hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS access  (hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS refresh (hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL,
                                                access_hash TEXT);
            """
        )

    def put(self, table: str, key: str, data: dict, expires_at: float, **extra: str) -> None:
        cols = ["id" if table in ("clients", "pending") else "hash", "data",
                "created_at" if table == "clients" else "expires_at", *extra]
        values = [key, json.dumps(data), expires_at, *extra.values()]
        marks = ",".join("?" * len(values))
        with self._lock, self._db:
            self._db.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({marks})", values)

    def get(self, table: str, key: str) -> dict | None:
        key_col = "id" if table in ("clients", "pending") else "hash"
        with self._lock:
            row = self._db.execute(f"SELECT data FROM {table} WHERE {key_col}=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_row(self, table: str, key: str) -> tuple | None:
        with self._lock:
            return self._db.execute(f"SELECT * FROM {table} WHERE hash=?", (key,)).fetchone()

    def delete(self, table: str, key: str) -> None:
        key_col = "id" if table in ("clients", "pending") else "hash"
        with self._lock, self._db:
            self._db.execute(f"DELETE FROM {table} WHERE {key_col}=?", (key,))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock, self._db:
            for table in ("pending", "codes", "access", "refresh"):
                self._db.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))


class _LoginThrottle:
    """Trava global de tentativas (servidor de um único dono)."""

    def __init__(self) -> None:
        self.failures = 0
        self.locked_until = 0.0

    def seconds_locked(self) -> int:
        return max(0, int(self.locked_until - time.time()))

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= MAX_FREE_FAILURES:
            wait = min(MAX_LOCK_SECONDS, 30 * 2 ** (self.failures - MAX_FREE_FAILURES))
            self.locked_until = time.time() + wait
            log.warning("Tela de autorização travada por %ss após %s falhas.", wait, self.failures)

    def success(self) -> None:
        self.failures = 0
        self.locked_until = 0.0


class SingleOwnerOAuthProvider:
    """Implementa OAuthAuthorizationServerProvider do SDK do MCP."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = _Store(os.path.join(settings.data_dir, "oauth.sqlite3"))
        self.throttle = _LoginThrottle()
        self._password_hash = hashlib.sha256(settings.admin_password.encode()).digest()

    # -------------------------------------------------------------- clientes

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get("clients", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        allowed = self.settings.allowed_redirect_prefixes
        bad = [u for u in uris if not any(u.startswith(p) for p in allowed)]
        if not uris or bad:
            log.warning("Registro de cliente recusado; redirect_uris=%s", uris)
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect_uri fora da lista permitida (MCP_ALLOWED_REDIRECT_PREFIXES).",
            )
        self.store.put("clients", client_info.client_id or "", client_info.model_dump(mode="json"), time.time())
        log.info("Cliente OAuth registrado: %s (%s)", client_info.client_name, client_info.client_id)

    # ----------------------------------------------------------- autorização

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self.store.purge_expired()
        request_id = secrets.token_urlsafe(24)
        self.store.put("pending", request_id, {
            "client_id": client.client_id,
            "params": params.model_dump(mode="json"),
        }, time.time() + PENDING_TTL)
        return f"{self.settings.public_url}/login?request={request_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        data = self.store.get("codes", _hash(authorization_code))
        if not data or data["client_id"] != client.client_id or data["expires_at"] < time.time():
            return None
        return AuthorizationCode.model_validate({**data, "code": authorization_code})

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        self.store.delete("codes", _hash(authorization_code.code))  # uso único
        return self._issue(client.client_id or "", authorization_code.scopes, authorization_code.resource)

    # ---------------------------------------------------------------- tokens

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        access_exp, refresh_exp = now + self.settings.access_token_ttl, now + self.settings.refresh_token_ttl
        base = {"client_id": client_id, "scopes": scopes, "resource": resource, "subject": self.settings.mail_address}
        self.store.put("access", _hash(access), {**base, "expires_at": access_exp}, access_exp)
        self.store.put("refresh", _hash(refresh), {**base, "expires_at": refresh_exp}, refresh_exp,
                       access_hash=_hash(access))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.settings.access_token_ttl,
                          refresh_token=refresh, scope=" ".join(scopes) or None)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = self.store.get("refresh", _hash(refresh_token))
        if not data or data["client_id"] != client.client_id or data["expires_at"] < time.time():
            return None
        return RefreshToken.model_validate({**data, "token": refresh_token})

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        self._revoke_refresh(_hash(refresh_token.token))  # rotação: o antigo deixa de valer
        return self._issue(client.client_id or "", scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        for static in self.settings.static_tokens:
            if hmac.compare_digest(token.encode(), static.encode()):
                return AccessToken(token=token, client_id="static-token", scopes=[SCOPE],
                                   subject=self.settings.mail_address)
        data = self.store.get("access", _hash(token))
        if not data or data["expires_at"] < time.time():
            return None
        return AccessToken.model_validate({**data, "token": token})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            self._revoke_refresh(_hash(token.token))
        else:
            self.store.delete("access", _hash(token.token))

    def _revoke_refresh(self, refresh_hash: str) -> None:
        row = self.store.get_row("refresh", refresh_hash)
        if row and row[3]:
            self.store.delete("access", row[3])
        self.store.delete("refresh", refresh_hash)

    async def exchange_identity_assertion(self, client: Any, params: Any) -> OAuthToken:  # pragma: no cover
        raise NotImplementedError("Concessão por asserção de identidade não é usada neste servidor.")

    # ------------------------------------------------------- tela de login

    def _password_ok(self, password: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self._password_hash)

    async def login_page(self, request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        pending = self.store.get("pending", request_id)
        if not pending:
            return _page("Pedido expirado", "<p>Este pedido de autorização expirou ou já foi usado. "
                         "Volte ao chat e conecte de novo.</p>", status=400)
        client = await self.get_client(pending["client_id"])
        return _page("Autorizar acesso ao Aggilli Mail", _form(request_id, client, pending, error=None, address=self.settings.mail_address))

    async def login_submit(self, request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("request", ""))
        pending = self.store.get("pending", request_id)
        if not pending:
            return _page("Pedido expirado", "<p>Este pedido de autorização expirou ou já foi usado.</p>", status=400)
        params = AuthorizationParams.model_validate(pending["params"])
        client = await self.get_client(pending["client_id"])

        if form.get("action") == "deny":
            self.store.delete("pending", request_id)
            return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), error="access_denied",
                                                           state=params.state), status_code=302)

        locked = self.throttle.seconds_locked()
        if locked:
            return _page("Aguarde", _form(request_id, client, pending,
                                          error=f"Muitas tentativas. Tente de novo em {locked} segundos.", address=self.settings.mail_address), status=429)
        if not self._password_ok(str(form.get("password", ""))):
            self.throttle.fail()
            log.warning("Senha de autorização incorreta.")
            return _page("Autorizar acesso ao Aggilli Mail", _form(request_id, client, pending, error="Senha incorreta.", address=self.settings.mail_address),
                         status=401)

        self.throttle.success()
        self.store.delete("pending", request_id)
        code = secrets.token_urlsafe(32)
        self.store.put("codes", _hash(code), {
            "client_id": pending["client_id"],
            "scopes": params.scopes or [SCOPE],
            "expires_at": time.time() + CODE_TTL,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
            "subject": self.settings.mail_address,
        }, time.time() + CODE_TTL)
        log.info("Acesso autorizado para o cliente %s.", pending["client_id"])
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                status_code=302)


# ------------------------------------------------------------------- HTML

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1d2330;margin:0;padding:24px 16px}}
main{{max-width:420px;margin:8vh auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 12px #0001}}
h1{{font-size:1.25rem;margin:0 0 12px}} p{{line-height:1.5}} .muted{{color:#5b6475;font-size:.9rem}}
input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #c9ced8;border-radius:8px;font-size:1rem}}
.row{{display:flex;flex-direction:row-reverse;gap:8px;margin-top:16px}} button{{flex:1;padding:10px;border-radius:8px;border:0;font-size:1rem}}
.ok{{background:#0b5cad;color:#fff}} .no{{background:#e7e9ee}} .err{{color:#b3261e}}
@media (prefers-color-scheme:dark){{body{{background:#14171c;color:#e6e8ec}}main{{background:#1d2128}}
.muted{{color:#a0a7b4}}input{{background:#14171c;color:#e6e8ec;border-color:#3a404b}}.no{{background:#2c313a;color:#e6e8ec}}}}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
    return HTMLResponse(doc, status_code=status, headers=_SECURITY_HEADERS)


def _form(request_id: str, client: OAuthClientInformationFull | None, pending: dict, error: str | None,
          address: str = "") -> str:
    name = html.escape((client.client_name if client else None) or "Aplicativo desconhecido")
    redirect = html.escape(str(pending["params"].get("redirect_uri", "")).split("?")[0])
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    # "Autorizar" vem primeiro no HTML para que Enter no campo de senha autorize;
    # o CSS (row-reverse) o mostra à direita, posição usual da ação principal.
    return f"""<p><strong>{name}</strong> quer acessar a caixa <strong>{html.escape(address)}</strong>
do Aggilli Mail: ler, buscar, organizar, criar rascunhos e <strong>enviar emails</strong> em nome dela.</p>
<p class="muted">Retorno para: {redirect}</p>{err}
<form method="post" action="login">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<label for="pw">Senha de autorização do servidor MCP</label>
<input id="pw" type="password" name="password" autocomplete="current-password" autofocus>
<div class="row"><button class="ok" name="action" value="allow">Autorizar</button>
<button class="no" name="action" value="deny">Recusar</button></div></form>"""
