"""Comandos administrativos do banco OAuth.

Uso (no container):
    docker compose exec mcp-email python -m aggilli_mail_mcp.admin clients
    docker compose exec mcp-email python -m aggilli_mail_mcp.admin revoke-all

`revoke-all` apaga todos os tokens, códigos e clientes registrados: todo
conector precisará autorizar de novo. É o botão de emergência descrito em
mcp/README.md. Lê apenas MCP_DATA_DIR, para funcionar mesmo com o resto
da configuração quebrado.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime


def _db() -> sqlite3.Connection:
    path = os.path.join(os.environ.get("MCP_DATA_DIR", "/data"), "oauth.sqlite3")
    if not os.path.exists(path):
        print(f"Banco não encontrado em {path}; nada a fazer.")
        sys.exit(0)
    return sqlite3.connect(path)


def clients() -> None:
    db = _db()
    rows = db.execute("SELECT id, data, created_at FROM clients ORDER BY created_at").fetchall()
    tokens = db.execute("SELECT COUNT(*) FROM refresh").fetchone()[0]
    for cid, data, created in rows:
        info = json.loads(data)
        when = datetime.fromtimestamp(created).isoformat(timespec="seconds")
        print(f"{when}  {cid}  {info.get('client_name') or '-'}  {', '.join(info.get('redirect_uris') or [])}")
    db.close()
    print(f"{len(rows)} cliente(s), {tokens} sessão(ões) ativa(s).")


def revoke_all() -> None:
    db = _db()
    with db:
        for table in ("access", "refresh", "codes", "pending", "clients"):
            db.execute(f"DELETE FROM {table}")
    db.close()
    print("Todos os tokens e clientes foram revogados. Reconecte o Aggilli Mail no chat.")


COMMANDS = {"clients": clients, "revoke-all": revoke_all}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"Uso: python -m aggilli_mail_mcp.admin [{'|'.join(COMMANDS)}]")
        sys.exit(2)
    COMMANDS[sys.argv[1]]()
