"""Fixtures: configurações de teste e um docker-mailserver descartável (IMAP/SMTP de verdade).

O servidor de teste usa o mesmo image do servidor real, um domínio fictício
(exemplo.test) e certificado autoassinado. Nada sai para a internet: os emails
de teste vão de uma caixa local para outra. Sem Docker, os testes "ao vivo"
são pulados (os de OAuth e de unidade continuam).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp.client import Client  # noqa: E402

from aggilli_mail_mcp.config import Settings  # noqa: E402
from aggilli_mail_mcp.mailbox import Mailbox  # noqa: E402
from aggilli_mail_mcp.server import build_server  # noqa: E402

IMAGE = "ghcr.io/docker-mailserver/docker-mailserver:latest"
DOMAIN = "exemplo.test"
USER, OTHER = f"teste@{DOMAIN}", f"outro@{DOMAIN}"
PASSWORD = "senha-da-caixa-de-teste"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = dict(mail_address=USER, mail_password=PASSWORD, auth_mode="none", host="127.0.0.1",
                                data_dir=str(tmp_path), mail_verify_tls=False, mail_timeout=15.0)
    base.update(overrides)
    return Settings(**base)


def _docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="session")
def dms(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("Docker indisponível: testes com servidor de email real pulados.")
    certs = tmp_path_factory.mktemp("certs")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", f"/CN=mail.{DOMAIN}", "-keyout", str(certs / "key.pem"), "-out", str(certs / "cert.pem")],
                   check=True, capture_output=True)
    for f in certs.iterdir():
        f.chmod(0o644)
    name = f"aggilli-mcp-teste-{uuid.uuid4().hex[:8]}"
    _docker("run", "-d", "--rm", "--name", name, "--hostname", f"mail.{DOMAIN}",
            "-e", "SSL_TYPE=manual", "-e", "SSL_CERT_PATH=/certs/cert.pem", "-e", "SSL_KEY_PATH=/certs/key.pem",
            "-e", "ENABLE_RSPAMD=0", "-e", "ENABLE_CLAMAV=0", "-e", "ENABLE_FAIL2BAN=0", "-e", "ENABLE_AMAVIS=0",
            "-e", "ENABLE_SPAMASSASSIN=0", "-e", "ENABLE_POSTGREY=0", "-e", "SPOOF_PROTECTION=1",
            "-e", "PERMIT_DOCKER=none", "-e", "LOG_LEVEL=warn",
            "-v", f"{certs}:/certs:ro", "-p", "127.0.0.1::993", "-p", "127.0.0.1::465", IMAGE)
    try:
        for account in (USER, OTHER):
            for _ in range(60):
                if subprocess.run(["docker", "exec", name, "setup", "email", "add", account, PASSWORD],
                                  capture_output=True).returncode == 0:
                    break
                time.sleep(2)
        imap = int(_docker("port", name, "993/tcp").rsplit(":", 1)[1])
        smtp = int(_docker("port", name, "465/tcp").rsplit(":", 1)[1])
        info = {"imap_port": imap, "smtp_port": smtp, "imap_host": "127.0.0.1", "smtp_host": "127.0.0.1"}
        deadline = time.time() + 180
        last: Exception | None = None
        while time.time() < deadline:
            try:
                for who in (USER, OTHER):
                    Mailbox(Settings(mail_address=who, mail_password=PASSWORD, mail_verify_tls=False,
                                     **info)).folders()  # type: ignore[arg-type]
                break
            except Exception as exc:  # servidor ainda subindo
                last = exc
                time.sleep(3)
        else:
            raise RuntimeError(f"docker-mailserver de teste não ficou pronto: {last}")
        yield info
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


class ToolCaller:
    def __init__(self, client: Client):
        self.client = client

    async def __call__(self, name: str, args: dict[str, Any] | None = None) -> Any:
        result = await self.client.call_tool(name, args or {})
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raise AssertionError(f"ferramenta falhou: {text}")
        try:
            return json.loads(text)
        except ValueError:
            return result

    async def error(self, name: str, args: dict[str, Any] | None = None) -> str:
        result = await self.client.call_tool(name, args or {})
        assert result.is_error, "era esperado erro"
        return "\n".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture
def live_settings(dms: dict[str, Any], tmp_path: Path) -> Settings:
    return make_settings(tmp_path, **dms)


@pytest.fixture
async def call(live_settings: Settings):
    server, _, _ = build_server(live_settings)
    async with Client(server) as client:
        yield ToolCaller(client)


@pytest.fixture
def other_box(dms: dict[str, Any]) -> Mailbox:
    return Mailbox(Settings(mail_address=OTHER, mail_password=PASSWORD, mail_verify_tls=False, **dms))  # type: ignore[arg-type]
