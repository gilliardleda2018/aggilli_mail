"""Ferramentas contra um docker-mailserver real e descartável (ver conftest.dms)."""

from __future__ import annotations

import time
import uuid

import pytest
from conftest import OTHER, USER

from aggilli_mail_mcp.mailbox import Mailbox

pytestmark = pytest.mark.anyio


def wait_inbox(box: Mailbox, subject: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = box.search("inbox", subject=subject)
        if found["messages"]:
            return found["messages"][0]
        time.sleep(1)
    raise AssertionError(f"email {subject!r} não chegou")


async def test_account_info_and_folders(call):
    info = await call("email_account_info")
    assert info["address"] == USER
    roles = {f.get("role") for f in info["folders"]}
    assert {"inbox", "drafts", "sent", "trash", "junk"} <= roles
    assert info["sends_left"]["hour"] > 0


async def test_draft_then_confirmed_send_reaches_recipient(call, other_box):
    subject = f"Proposta {uuid.uuid4().hex[:6]}"
    draft = await call("email_draft_create", {"to": [OTHER], "subject": subject, "body": "Olá, segue a proposta."})
    assert draft["draft_uid"] and draft["to"] == [OTHER]
    drafts = await call("email_search", {"folder": "drafts", "subject": subject})
    assert drafts["messages"][0]["uid"] == draft["draft_uid"]

    # Sem confirmação, ou com destinatários diferentes do rascunho, nada sai.
    assert "não confirmado" in await call.error("email_send_draft", {"draft_uid": draft["draft_uid"],
                                                                     "confirmed": False, "recipients": [OTHER]})
    assert "não batem" in await call.error("email_send_draft", {"draft_uid": draft["draft_uid"], "confirmed": True,
                                                                "recipients": ["intruso@exemplo.test"]})

    sent = await call("email_send_draft", {"draft_uid": draft["draft_uid"], "confirmed": True,
                                           "recipients": [OTHER.upper()]})
    assert sent["sent"] is True and "warning" not in sent
    got = wait_inbox(other_box, subject)
    assert USER in got["from"] and "Aggilli Intelligence" in got["from"]
    # Cópia em Enviados e o rascunho sai da pasta Rascunhos.
    assert (await call("email_search", {"folder": "sent", "subject": subject}))["total"] == 1
    assert (await call("email_search", {"folder": "drafts", "subject": subject}))["total"] == 0


async def test_read_reply_mark_and_move_to_trash(call, other_box, live_settings):
    from email.message import EmailMessage
    subject = f"Orçamento da obra {uuid.uuid4().hex[:6]}"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = OTHER, USER, subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@exemplo.test>"
    msg.set_content("Texto simples")
    msg.add_alternative("<html><body><p>Prezado,</p><p>Valor: <b>R$ 18.400</b></p>"
                        "<script>alert(1)</script></body></html>", subtype="html")
    msg.add_attachment(b"%PDF-1.4 teste", maintype="application", subtype="pdf", filename="orcamento.pdf")
    other_box.send(msg, [USER])
    me = Mailbox(live_settings)
    found = wait_inbox(me, subject)
    assert found["unseen"] is True

    # Busca com acento (vai como literal UTF-8).
    hits = await call("email_search", {"subject": "Orçamento", "unseen": True})
    assert any(m["uid"] == found["uid"] for m in hits["messages"])

    read = await call("email_read", {"folder": "inbox", "uid": found["uid"]})
    assert read["body"] == "Texto simples" and read["attachments"][0]["filename"] == "orcamento.pdf"
    assert (await call("email_search", {"subject": subject}))["messages"][0]["unseen"] is True  # ler não marca

    reply = await call("email_draft_create", {"reply_to_folder": "inbox", "reply_to_uid": found["uid"],
                                              "body": "Recebido, obrigado."})
    assert reply["to"] == [OTHER] and reply["subject"] == f"Re: {subject}"
    assert "> Texto simples" in reply["body"]

    await call("email_mark", {"folder": "inbox", "uids": [found["uid"]], "seen": True, "flagged": True})
    marked = (await call("email_search", {"subject": subject}))["messages"][0]
    assert marked["unseen"] is False and marked["flagged"] is True

    moved = await call("email_move", {"folder": "inbox", "uids": [found["uid"]], "destination": "trash"})
    assert moved["to_folder"] == "Trash"
    assert (await call("email_search", {"subject": subject}))["total"] == 0
    assert (await call("email_search", {"folder": "trash", "subject": subject}))["total"] == 1


async def test_html_only_message_becomes_text(call, other_box, live_settings):
    from email.message import EmailMessage
    subject = f"Só HTML {uuid.uuid4().hex[:6]}"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = OTHER, USER, subject
    msg.set_content("<p>Linha 1</p><p>Linha <i>2</i></p><style>p{}</style>", subtype="html")
    other_box.send(msg, [USER])
    uid = wait_inbox(Mailbox(live_settings), subject)["uid"]
    read = await call("email_read", {"folder": "inbox", "uid": uid})
    assert read["body"] == "Linha 1\n\nLinha 2" and read["body_format"] == "html convertido em texto"


async def test_guards(call, live_settings):
    assert "não existe" in await call.error("email_search", {"folder": "Pasta Inventada"})
    assert "inválido" in await call.error("email_draft_create", {"to": ["não é email"], "body": "x"})
    many = [f"p{i}@exemplo.test" for i in range(live_settings.max_recipients + 1)]
    assert "No máximo" in await call.error("email_draft_create", {"to": many, "body": "x"})
    assert "não encontrada" in await call.error("email_read", {"folder": "inbox", "uid": 999999})
    assert "mesma" in await call.error("email_move", {"folder": "inbox", "uids": [1], "destination": "INBOX"})


async def test_send_limit(dms, tmp_path):
    from mcp.client import Client
    from conftest import ToolCaller, make_settings

    from aggilli_mail_mcp.server import build_server
    server, _, _ = build_server(make_settings(tmp_path, max_sends_per_hour=1, **dms))
    async with Client(server) as client:
        call = ToolCaller(client)
        uids = [(await call("email_draft_create", {"to": [OTHER], "subject": f"limite {i}", "body": "x"}))["draft_uid"]
                for i in range(2)]
        await call("email_send_draft", {"draft_uid": uids[0], "confirmed": True, "recipients": [OTHER]})
        err = await call.error("email_send_draft", {"draft_uid": uids[1], "confirmed": True, "recipients": [OTHER]})
        assert "Limite de envios" in err


async def test_wrong_mailbox_password_is_clear(dms, tmp_path):
    from aggilli_mail_mcp.mailbox import MailError
    box = Mailbox(make_settings_wrong(tmp_path, dms))
    with pytest.raises(MailError, match="recusou o login"):
        box.folders()


def make_settings_wrong(tmp_path, dms):
    from conftest import make_settings
    return make_settings(tmp_path, mail_password="errada", **dms)
