"""OAuth de ponta a ponta sobre HTTP, proteção do endpoint /mcp e configuração."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
import pytest
from conftest import make_settings

from aggilli_mail_mcp.config import ConfigError, validate
from aggilli_mail_mcp.server import build_app

pytestmark = pytest.mark.anyio

PUBLIC = "https://mcp.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "senha-forte-de-teste-123"
STATIC = "t" * 40
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture
async def http(tmp_path):
    settings = make_settings(tmp_path, auth_mode="oauth", admin_password=PASSWORD, public_url=PUBLIC,
                             host="0.0.0.0", static_tokens=(STATIC,))
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
            yield client


async def register(http: httpx.AsyncClient, redirect: str = CALLBACK) -> httpx.Response:
    return await http.post("/register", json={"client_name": "Claude", "redirect_uris": [redirect],
                                              "grant_types": ["authorization_code", "refresh_token"],
                                              "response_types": ["code"], "token_endpoint_auth_method": "none"})


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def start_authorization(http: httpx.AsyncClient) -> tuple[str, str, str]:
    client_id = (await register(http)).json()["client_id"]
    verifier, challenge = pkce()
    r = await http.get("/authorize", params={"response_type": "code", "client_id": client_id,
                                             "redirect_uri": CALLBACK, "code_challenge": challenge,
                                             "code_challenge_method": "S256", "state": "st4te"})
    assert r.status_code == 302
    login_url = urlparse(r.headers["location"])
    assert login_url.path == "/login"
    return client_id, verifier, parse_qs(login_url.query)["request"][0]


async def tools_list(http: httpx.AsyncClient, token: str | None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


async def test_metadata_and_unauthenticated_rejected(http):
    meta = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert meta["issuer"].rstrip("/") == PUBLIC and meta["registration_endpoint"].endswith("/register")
    r = await tools_list(http, None)
    assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")
    assert (await tools_list(http, "token-inventado")).status_code == 401


async def test_contract_of_aggilli_chat_monitor(http):
    """O monitor do Aggilli Chat (monitor/verificar.py) espera: 401 com resource_metadata, e metadados com
    authorization_servers. Se isso mudar, o monitor acusa o Aggilli Mail como fora do ar."""
    import re
    r = await tools_list(http, None)
    meta_url = re.search(r'resource_metadata="([^"]+)"', r.headers["www-authenticate"]).group(1)
    meta = (await http.get(meta_url)).json()
    assert meta["authorization_servers"] and meta["resource"].rstrip("/") == f"{PUBLIC}/mcp"


async def test_register_rejects_foreign_redirect(http):
    r = await register(http, "https://evil.example/callback")
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


async def test_aggilli_chat_callback_accepted_only_for_aggillimail_server(http):
    """O Aggilli Chat (LibreChat) só pode registrar o retorno do servidor 'aggillimail'."""
    ok = await register(http, "https://chat.aggillintelligence.com.br/api/mcp/aggillimail/oauth/callback")
    assert ok.status_code == 201, ok.text
    other = await register(http, "https://chat.aggillintelligence.com.br/api/mcp/pulse/oauth/callback")
    assert other.status_code == 400 and other.json()["error"] == "invalid_redirect_uri"


async def test_confidential_client_like_librechat_completes_flow(http):
    """LibreChat pode se registrar como cliente com segredo (client_secret_post); o fluxo precisa fechar igual."""
    callback = "https://chat.aggillintelligence.com.br/api/mcp/aggillimail/oauth/callback"
    reg = await http.post("/register", json={
        "client_name": "LibreChat", "redirect_uris": [callback],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post"})
    assert reg.status_code == 201, reg.text
    client = reg.json()
    assert client.get("client_secret")
    verifier, challenge = pkce()
    r = await http.get("/authorize", params={"response_type": "code", "client_id": client["client_id"],
                                             "redirect_uri": callback, "code_challenge": challenge,
                                             "code_challenge_method": "S256", "state": "s",
                                             "resource": f"{PUBLIC}/mcp"})
    request_id = parse_qs(urlparse(r.headers["location"]).query)["request"][0]
    ok = await http.post("/login", data={"request": request_id, "password": PASSWORD, "action": "allow"})
    assert ok.headers["location"].startswith(callback + "?")
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    token = await http.post("/token", data={"grant_type": "authorization_code", "code": code,
                                            "client_id": client["client_id"], "client_secret": client["client_secret"],
                                            "redirect_uri": callback, "code_verifier": verifier,
                                            "resource": f"{PUBLIC}/mcp"})
    assert token.status_code == 200, token.text
    assert (await tools_list(http, token.json()["access_token"])).status_code == 200


async def test_full_flow_with_refresh_rotation(http):
    client_id, verifier, request_id = await start_authorization(http)

    page = await http.get("/login", params={"request": request_id})
    assert page.status_code == 200 and "Claude" in page.text and page.headers["x-frame-options"] == "DENY"
    assert "teste@exemplo.test" in page.text and "enviar emails" in page.text

    wrong = await http.post("/login", data={"request": request_id, "password": "errada", "action": "allow"})
    assert wrong.status_code == 401

    ok = await http.post("/login", data={"request": request_id, "password": PASSWORD, "action": "allow"})
    assert ok.status_code == 302
    back = urlparse(ok.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    query = parse_qs(back.query)
    assert query["state"] == ["st4te"]
    code = query["code"][0]

    # O pedido de autorização é de uso único.
    again = await http.post("/login", data={"request": request_id, "password": PASSWORD, "action": "allow"})
    assert again.status_code == 400

    token = await http.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                            "redirect_uri": CALLBACK, "code_verifier": verifier})
    assert token.status_code == 200, token.text
    tokens = token.json()
    assert (await tools_list(http, tokens["access_token"])).status_code == 200

    # O código não pode ser reutilizado.
    reuse = await http.post("/token", data={"grant_type": "authorization_code", "code": code,
                                            "client_id": client_id, "redirect_uri": CALLBACK,
                                            "code_verifier": verifier})
    assert reuse.status_code == 400

    refreshed = await http.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                "refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    new = refreshed.json()
    assert (await tools_list(http, new["access_token"])).status_code == 200
    # Rotação: o refresh e o access antigos deixam de valer.
    assert (await tools_list(http, tokens["access_token"])).status_code == 401
    stale = await http.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                            "refresh_token": tokens["refresh_token"]})
    assert stale.status_code == 400


async def test_wrong_pkce_verifier_rejected(http):
    client_id, _, request_id = await start_authorization(http)
    ok = await http.post("/login", data={"request": request_id, "password": PASSWORD, "action": "allow"})
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    r = await http.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                        "redirect_uri": CALLBACK, "code_verifier": "x" * 50})
    assert r.status_code == 400


async def test_deny_returns_access_denied(http):
    _, _, request_id = await start_authorization(http)
    r = await http.post("/login", data={"request": request_id, "action": "deny"})
    assert r.status_code == 302 and "error=access_denied" in r.headers["location"]


async def test_lockout_after_failures(http):
    _, _, request_id = await start_authorization(http)
    for _ in range(5):
        await http.post("/login", data={"request": request_id, "password": "x", "action": "allow"})
    locked = await http.post("/login", data={"request": request_id, "password": PASSWORD, "action": "allow"})
    assert locked.status_code == 429


async def test_static_token_and_host_check(http):
    assert (await tools_list(http, STATIC)).status_code == 200
    evil = await http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {STATIC}", "Host": "evil.com"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert evil.status_code in (403, 421)


async def test_admin_revoke_all(http, tmp_path, monkeypatch, capsys):
    client_id, verifier, request_id = await start_authorization(http)
    ok = await http.post("/login", data={"request": request_id, "password": PASSWORD, "action": "allow"})
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    access = (await http.post("/token", data={"grant_type": "authorization_code", "code": code,
                                              "client_id": client_id, "redirect_uri": CALLBACK,
                                              "code_verifier": verifier})).json()["access_token"]
    assert (await tools_list(http, access)).status_code == 200

    from aggilli_mail_mcp import admin
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))
    admin.clients()
    assert "1 cliente(s), 1 sessão(ões)" in capsys.readouterr().out
    admin.revoke_all()
    assert (await tools_list(http, access)).status_code == 401


async def test_healthz_is_public(http):
    assert (await http.get("/healthz")).json()["status"] == "ok"


def test_config_guards(tmp_path):
    with pytest.raises(ConfigError, match="MCP_ADMIN_PASSWORD"):
        validate(make_settings(tmp_path, auth_mode="oauth", admin_password="curta", public_url=PUBLIC))
    with pytest.raises(ConfigError, match="https"):
        validate(make_settings(tmp_path, auth_mode="oauth", admin_password=PASSWORD,
                               public_url="http://mcp.example.com"))
    with pytest.raises(ConfigError, match="127.0.0.1"):
        validate(make_settings(tmp_path, auth_mode="none", host="0.0.0.0"))
    with pytest.raises(ConfigError, match="igual"):
        validate(make_settings(tmp_path, auth_mode="oauth", admin_password="senha-da-caixa-de-teste",
                               public_url=PUBLIC))
    with pytest.raises(ConfigError, match="MCP_MAIL_ADDRESS"):
        validate(make_settings(tmp_path, mail_address="sem-arroba"))
    with pytest.raises(ConfigError, match="MCP_TIMEZONE"):
        validate(make_settings(tmp_path, timezone="Marte/Base"))
    with pytest.raises(ConfigError, match="MCP_STATIC_TOKENS"):
        validate(make_settings(tmp_path, static_tokens=("curto",)))
    validate(make_settings(tmp_path, auth_mode="oauth", admin_password=PASSWORD, public_url=PUBLIC))
