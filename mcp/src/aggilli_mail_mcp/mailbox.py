"""Acesso à caixa de email por IMAP (leitura e organização) e SMTP (envio).

Síncrono de propósito (imaplib/smtplib da biblioteca padrão, sem dependências);
as ferramentas chamam estes métodos em uma thread. Cada operação abre e fecha a
própria conexão: simples, sem estado e resistente a quedas do servidor.

Regras de segurança aplicadas aqui, não só na descrição das ferramentas:
- nada é apagado definitivamente, exceto o rascunho que acabou de ser enviado
  (a cópia fica em Enviados);
- mensagens só são movidas, nunca expurgadas;
- o envio sai sempre da própria caixa configurada (o servidor também bloqueia
  remetente falso com SPOOF_PROTECTION).
"""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import re
import smtplib
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage, Message
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError

from .config import EMAIL_RE, Settings

ROLES = ("inbox", "drafts", "sent", "trash", "junk", "archive")
_ROLE_FLAGS = {"\\drafts": "drafts", "\\sent": "sent", "\\trash": "trash", "\\junk": "junk", "\\archive": "archive"}
_FALLBACK = {"inbox": "INBOX", "drafts": "Drafts", "sent": "Sent", "trash": "Trash", "junk": "Junk",
             "archive": "Archive"}
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')
_UID_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_APPENDUID_RE = re.compile(rb"APPENDUID \d+ (\d+)")
_HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID"


class MailError(ToolError):
    """Erro com mensagem pronta para o modelo (sem segredos); o SDK a entrega como erro da ferramenta."""


# ----------------------------------------------------------------- utilitários

def decode_mutf7(raw: str) -> str:
    """Nome de pasta IMAP (UTF-7 modificado, RFC 3501) para texto."""
    out, i = [], 0
    while i < len(raw):
        if raw[i] == "&":
            j = raw.index("-", i)
            chunk = raw[i + 1:j]
            if not chunk:
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def encode_mutf7(text: str) -> str:
    out, buf = [], ""

    def flush() -> None:
        nonlocal buf
        if buf:
            b64 = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b64}-")
            buf = ""

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return "".join(out)


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_addresses(value: str | list[str] | None, limit: int) -> list[str]:
    """Lista de destinatários validada (só endereços, sem nomes), sem repetição."""
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    found: list[str] = []
    for _, addr in getaddresses([str(v) for v in items]):
        addr = addr.strip().lower()
        if not addr:
            continue
        if not EMAIL_RE.match(addr):
            raise MailError(f"Endereço inválido: {addr!r}.")
        if addr not in found:
            found.append(addr)
    if len(found) > limit:
        raise MailError(f"No máximo {limit} destinatários por email (pedido: {len(found)}).")
    return found


class _HTMLText(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [+{len(text) - limit} caracteres]"


def _header(msg: Message, name: str) -> str:
    value = msg.get(name)
    return str(value).strip() if value is not None else ""


def _iso(value: str, tz: ZoneInfo) -> str:
    try:
        return parsedate_to_datetime(value).astimezone(tz).isoformat(timespec="minutes")
    except (TypeError, ValueError, IndexError):
        return value


def _imap_date(value: str, field: str) -> str:
    try:
        d = date.fromisoformat(value[:10])
    except ValueError as exc:
        raise MailError(f"{field} inválido: {value!r}. Use AAAA-MM-DD.") from exc
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{d.day}-{months[d.month - 1]}-{d.year}"


@dataclass
class Folder:
    name: str
    role: str | None
    selectable: bool


# --------------------------------------------------------------------- caixa

class Mailbox:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self._roles: dict[str, str] | None = None

    # ------------------------------------------------------------ conexões

    def _ssl(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.settings.mail_verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @contextmanager
    def _imap(self) -> Iterator[imaplib.IMAP4_SSL]:
        s = self.settings
        try:
            conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port, ssl_context=self._ssl(), timeout=s.mail_timeout)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailError(f"Servidor de email indisponível (IMAP {s.imap_host}:{s.imap_port}): {exc}") from exc
        try:
            try:
                conn.login(s.mail_address, s.mail_password)
            except imaplib.IMAP4.error as exc:
                raise MailError("A caixa recusou o login (senha da caixa no servidor MCP está errada?).") from exc
            yield conn
        except imaplib.IMAP4.error as exc:
            raise MailError(f"Erro do servidor de email: {exc}") from exc
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    @staticmethod
    def _ok(result: tuple[str, list[Any]], what: str) -> list[Any]:
        status, data = result
        if status != "OK":
            detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
            raise MailError(f"{what} falhou: {detail or status}")
        return data

    # --------------------------------------------------------------- pastas

    def _list(self, conn: imaplib.IMAP4_SSL) -> list[Folder]:
        folders = []
        for line in self._ok(conn.list(), "Listar pastas"):
            if not isinstance(line, bytes):
                continue
            m = _LIST_RE.match(line)
            if not m:
                continue
            flags = m.group("flags").decode().lower().split()
            raw = m.group("name").decode()
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            name = decode_mutf7(raw)
            role = "inbox" if name.upper() == "INBOX" else next(
                (_ROLE_FLAGS[f] for f in flags if f in _ROLE_FLAGS), None)
            folders.append(Folder(name=name, role=role, selectable="\\noselect" not in flags))
        return folders

    def _role_map(self, conn: imaplib.IMAP4_SSL) -> dict[str, str]:
        if self._roles is None:
            found = {f.role: f.name for f in self._list(conn) if f.role}
            self._roles = {r: found.get(r, _FALLBACK[r]) for r in ROLES}
        return self._roles

    def resolve(self, conn: imaplib.IMAP4_SSL, folder: str) -> str:
        """Aceita o nome da pasta ou o papel (inbox, drafts, sent, trash, junk, archive)."""
        folder = (folder or "inbox").strip()
        roles = self._role_map(conn)
        if folder.lower() in roles:
            return roles[folder.lower()]
        names = {f.name for f in self._list(conn) if f.selectable}
        if folder not in names:
            raise MailError(f"Pasta {folder!r} não existe. Use email_folders para ver as pastas.")
        return folder

    def _select(self, conn: imaplib.IMAP4_SSL, folder: str, readonly: bool = True) -> str:
        name = self.resolve(conn, folder)
        self._ok(conn.select(quote(encode_mutf7(name)), readonly=readonly), f"Abrir a pasta {name!r}")
        return name

    def folders(self) -> list[dict[str, Any]]:
        out = []
        with self._imap() as conn:
            for f in self._list(conn):
                item: dict[str, Any] = {"name": f.name, "role": f.role}
                if f.selectable:
                    data = self._ok(conn.status(quote(encode_mutf7(f.name)), "(MESSAGES UNSEEN)"), "Contar")
                    m = re.search(rb"MESSAGES (\d+) UNSEEN (\d+)", data[0] or b"")
                    if m:
                        item.update(messages=int(m.group(1)), unseen=int(m.group(2)))
                out.append({k: v for k, v in item.items() if v is not None})
        return out

    # --------------------------------------------------------------- busca

    def search(self, folder: str = "inbox", text: str = "", sender: str = "", recipient: str = "",
               subject: str = "", since: str = "", before: str = "", unseen: bool = False,
               flagged: bool = False, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, self.settings.max_search_results))
        criteria: list[str] = []
        literal: bytes | None = None
        pending_key = ""
        for key, value in (("FROM", sender), ("TO", recipient), ("SUBJECT", subject), ("TEXT", text)):
            if not value:
                continue
            if value.isascii():
                criteria += [key, quote(value)]
            elif literal is None:
                literal = value.encode()
                pending_key = key
            else:
                raise MailError("Só um filtro com acento ou caractere especial por busca; simplifique os outros.")
        if since:
            criteria += ["SINCE", _imap_date(since, "since")]
        if before:
            criteria += ["BEFORE", _imap_date(before, "before")]
        if unseen:
            criteria.append("UNSEEN")
        if flagged:
            criteria.append("FLAGGED")
        with self._imap() as conn:
            name = self._select(conn, folder)
            if literal is not None:
                conn.literal = literal  # type: ignore[attr-defined]
                data = self._ok(conn.uid("SEARCH", "CHARSET", "UTF-8", *(criteria or ["ALL"]), pending_key),
                                "Buscar")
            else:
                data = self._ok(conn.uid("SEARCH", *(criteria or ["ALL"])), "Buscar")
            uids = [int(u) for u in (data[0] or b"").split()]
            total = len(uids)
            chosen = sorted(uids, reverse=True)[:limit]
            messages = self._summaries(conn, chosen)
        return {"folder": name, "total": total, "shown": len(messages), "messages": messages}

    def _summaries(self, conn: imaplib.IMAP4_SSL, uids: list[int]) -> list[dict[str, Any]]:
        if not uids:
            return []
        data = self._ok(conn.uid("FETCH", ",".join(map(str, uids)),
                                 f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})])"), "Ler cabeçalhos")
        found: dict[int, dict[str, Any]] = {}
        for part in data:
            if not isinstance(part, tuple):
                continue
            meta, raw = part
            m = _UID_RE.search(meta)
            if not m:
                continue
            uid = int(m.group(1))
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            flags = (_FLAGS_RE.search(meta).group(1).decode().split()  # type: ignore[union-attr]
                     if _FLAGS_RE.search(meta) else [])
            size = _SIZE_RE.search(meta)
            found[uid] = {
                "uid": uid,
                "date": _iso(_header(msg, "Date"), self.tz),
                "from": _header(msg, "From"),
                "to": _header(msg, "To"),
                "subject": _header(msg, "Subject") or "(sem assunto)",
                "unseen": "\\Seen" not in flags,
                "flagged": "\\Flagged" in flags,
                "size_kb": round(int(size.group(1)) / 1024, 1) if size else None,
            }
        return [found[u] for u in uids if u in found]

    # --------------------------------------------------------------- leitura

    def _fetch_raw(self, conn: imaplib.IMAP4_SSL, uid: int) -> bytes:
        data = self._ok(conn.uid("FETCH", str(uid), "(BODY.PEEK[])"), "Ler a mensagem")
        for part in data:
            if isinstance(part, tuple):
                return part[1]
        raise MailError(f"Mensagem uid={uid} não encontrada nesta pasta (use email_search para achar o uid).")

    def read(self, folder: str, uid: int, mark_seen: bool = False) -> dict[str, Any]:
        with self._imap() as conn:
            name = self._select(conn, folder, readonly=not mark_seen)
            raw = self._fetch_raw(conn, uid)
            if mark_seen:
                self._ok(conn.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Seen)"), "Marcar como lida")
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        body, kind = self.body_text(msg)
        attachments = []
        for att in msg.iter_attachments():  # type: ignore[attr-defined]
            payload = att.get_payload(decode=True) or b""
            attachments.append({"filename": att.get_filename() or "(sem nome)",
                                "type": att.get_content_type(), "size_kb": round(len(payload) / 1024, 1)})
        out = {
            "folder": name, "uid": uid,
            "date": _iso(_header(msg, "Date"), self.tz),
            "from": _header(msg, "From"), "reply_to": _header(msg, "Reply-To"),
            "to": _header(msg, "To"), "cc": _header(msg, "Cc"),
            "subject": _header(msg, "Subject") or "(sem assunto)",
            "message_id": _header(msg, "Message-ID"),
            "body_format": kind,
            "body": clip(body, self.settings.max_body_chars),
            "attachments": attachments,
        }
        return {k: v for k, v in out.items() if v not in ("", [], None)}

    @staticmethod
    def body_text(msg: EmailMessage | Message) -> tuple[str, str]:
        part = msg.get_body(preferencelist=("plain", "html")) if isinstance(msg, EmailMessage) else None
        if part is None:
            return "", "vazio"
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            return html_to_text(content), "html convertido em texto"
        return content.strip(), "texto"

    # ---------------------------------------------------------- organização

    def set_flags(self, folder: str, uids: list[int], seen: bool | None, flagged: bool | None) -> dict[str, Any]:
        if not uids:
            raise MailError("Informe ao menos um uid.")
        ids = ",".join(map(str, uids))
        with self._imap() as conn:
            name = self._select(conn, folder, readonly=False)
            for flag, value in (("\\Seen", seen), ("\\Flagged", flagged)):
                if value is not None:
                    self._ok(conn.uid("STORE", ids, "+FLAGS.SILENT" if value else "-FLAGS.SILENT", f"({flag})"),
                             "Marcar")
        return {"folder": name, "uids": uids, "seen": seen, "flagged": flagged}

    def move(self, folder: str, uids: list[int], destination: str) -> dict[str, Any]:
        if not uids:
            raise MailError("Informe ao menos um uid.")
        ids = ",".join(map(str, uids))
        with self._imap() as conn:
            dest = self.resolve(conn, destination)
            name = self._select(conn, folder, readonly=False)
            if dest == name:
                raise MailError("A pasta de destino é a mesma de origem.")
            caps = {c.upper() for c in conn.capabilities}
            target = quote(encode_mutf7(dest))
            if "MOVE" in caps:
                self._ok(conn.uid("MOVE", ids, target), "Mover")
            else:  # pragma: no cover - Dovecot tem MOVE
                self._ok(conn.uid("COPY", ids, target), "Copiar")
                self._ok(conn.uid("STORE", ids, "+FLAGS.SILENT", "(\\Deleted)"), "Marcar")
                self._ok(conn.uid("EXPUNGE", ids), "Concluir a movimentação")
        return {"from_folder": name, "to_folder": dest, "uids": uids}

    # ---------------------------------------------------------- rascunhos

    def original(self, folder: str, uid: int) -> EmailMessage:
        with self._imap() as conn:
            self._select(conn, folder)
            raw = self._fetch_raw(conn, uid)
        return email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[return-value]

    def append(self, role: str, msg: EmailMessage, flags: str) -> int | None:
        with self._imap() as conn:
            name = self.resolve(conn, role)
            data = self._ok(conn.append(quote(encode_mutf7(name)), flags, imaplib.Time2Internaldate(time.time()),
                                        msg.as_bytes(policy=email.policy.SMTP)), "Gravar")
        m = _APPENDUID_RE.search(b" ".join(d for d in data if isinstance(d, bytes)))
        return int(m.group(1)) if m else None

    def draft(self, uid: int) -> EmailMessage:
        return self.original("drafts", uid)

    def remove_draft(self, uid: int) -> None:
        """Único apagamento definitivo: o rascunho já enviado (a cópia fica em Enviados)."""
        with self._imap() as conn:
            self._select(conn, "drafts", readonly=False)
            self._ok(conn.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)"), "Marcar rascunho")
            self._ok(conn.uid("EXPUNGE", str(uid)), "Retirar rascunho enviado")

    # --------------------------------------------------------------- envio

    def send(self, msg: EmailMessage, recipients: list[str]) -> None:
        s = self.settings
        try:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=self._ssl(), timeout=s.mail_timeout) as smtp:
                smtp.login(s.mail_address, s.mail_password)
                refused = smtp.send_message(msg, from_addr=s.mail_address, to_addrs=recipients)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError("O servidor recusou o login de envio (senha da caixa errada?).") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(f"Todos os destinatários foram recusados: {', '.join(exc.recipients)}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"Falha ao enviar pelo SMTP: {exc}") from exc
        if refused:
            raise MailError(f"Enviado, mas recusado para: {', '.join(refused)}")

    def now_header(self) -> str:
        return format_datetime(datetime.now(self.tz))
