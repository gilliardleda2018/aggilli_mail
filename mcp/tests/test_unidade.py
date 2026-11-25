"""Peças sem servidor: nomes de pasta, endereços, HTML e a lista de ferramentas."""

from __future__ import annotations

import pytest
from conftest import make_settings
from mcp.client import Client

from aggilli_mail_mcp.mailbox import MailError, decode_mutf7, encode_mutf7, html_to_text, parse_addresses, quote
from aggilli_mail_mcp.server import build_server


@pytest.mark.parametrize("name", ["INBOX", "Orçamentos", "Clientes & Parceiros", "日本", "Ação/Notas"])
def test_mutf7_ida_e_volta(name):
    assert decode_mutf7(encode_mutf7(name)) == name


def test_mutf7_exemplo_da_rfc():
    assert decode_mutf7("~peter/mail/&U,BTFw-/&ZeVnLIqe-") == "~peter/mail/台北/日本語"


def test_enderecos():
    assert parse_addresses(["Fulano <A@Exemplo.com>", "a@exemplo.com", "b@exemplo.com"], 5) == \
        ["a@exemplo.com", "b@exemplo.com"]
    with pytest.raises(MailError, match="inválido"):
        parse_addresses(["sem arroba"], 5)
    with pytest.raises(MailError, match="No máximo 1"):
        parse_addresses(["a@x.com", "b@x.com"], 1)


def test_html_vira_texto_sem_script():
    assert html_to_text("<p>Oi</p><script>x()</script><p>Tchau &amp; até</p>") == "Oi\n\nTchau & até"


def test_quote_escapa_aspas():
    assert quote('a"b\\c') == '"a\\"b\\\\c"'


@pytest.mark.anyio
async def test_ferramentas_e_anotacoes(tmp_path):
    server, _, _ = build_server(make_settings(tmp_path))
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"email_account_info", "email_folders", "email_search", "email_read", "email_mark",
                          "email_move", "email_draft_create", "email_send_draft"}
    assert tools["email_send_draft"].annotations.open_world_hint is True
    assert tools["email_send_draft"].annotations.destructive_hint is True
    for name in ("email_account_info", "email_folders", "email_search", "email_read"):
        assert tools[name].annotations.read_only_hint is True
    assert {"draft_uid", "confirmed", "recipients"} <= set(tools["email_send_draft"].input_schema["required"])
