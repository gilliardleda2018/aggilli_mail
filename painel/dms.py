"""Acesso ao docker-mailserver via `docker exec`, sem shell intermediário."""
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from functools import lru_cache

CONTAINER = "mailserver"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class DmsError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _docker_prefix() -> tuple[str, ...]:
    """Usa `docker` direto se o processo já tem o grupo; senão passa por `sg docker`."""
    ok = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    return () if ok else ("sg", "docker", "-c")


def _run(cmd: list[str], timeout: int = 60, check: bool = True) -> str:
    prefix = _docker_prefix()
    full = [*prefix, shlex.join(cmd)] if prefix else cmd
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    out = ANSI.sub("", proc.stdout + proc.stderr).strip()
    if check and proc.returncode != 0:
        raise DmsError(out or f"falhou: {' '.join(cmd[:4])}")
    return out


def exec_(*args: str, timeout: int = 60, check: bool = True) -> str:
    return _run(["docker", "exec", CONTAINER, *args], timeout=timeout, check=check)


def setup(*args: str) -> str:
    return exec_("setup", *args)


# ---------- status ----------

def container_status() -> dict:
    try:
        raw = _run(["docker", "inspect", CONTAINER])
    except DmsError:
        return {"existe": False}
    info = json.loads(raw)[0]["State"]
    return {
        "existe": True,
        "rodando": info.get("Running", False),
        "saude": info.get("Health", {}).get("Status", "?"),
        "desde": info.get("StartedAt", "")[:19].replace("T", " "),
    }


def services() -> list[tuple[str, str]]:
    out = exec_("supervisorctl", "status", check=False)  # sai != 0 se algum serviço opcional está parado
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "STOPPED":
            rows.append((parts[0], parts[1]))
    return rows


# ---------- caixas ----------

@dataclass
class Mailbox:
    email: str
    usado: str
    cota: str
    pct: str
    aliases: list[str]


def list_mailboxes() -> list[Mailbox]:
    out = setup("email", "list")
    boxes: list[Mailbox] = []
    for line in out.splitlines():
        m = re.match(r"\*\s+(\S+)\s+\(\s*(.*?)\s*/\s*(.*?)\s*\)\s*\[(.*?)\]", line)
        if m:
            boxes.append(Mailbox(m[1], m[2], m[3], m[4], []))
            continue
        a = re.search(r"\[\s*aliases\s*->\s*(.*?)\s*\]", line)
        if a and boxes:
            boxes[-1].aliases = [x.strip() for x in a[1].split(",") if x.strip()]
    return boxes


def add_mailbox(email: str, password: str) -> str:
    return setup("email", "add", email, password)


def update_password(email: str, password: str) -> str:
    return setup("email", "update", email, password)


def delete_mailbox(email: str, apagar_emails: bool) -> str:
    try:
        return setup("email", "del", "-y" if apagar_emails else "-n", email)
    except DmsError as e:
        # Caixa que nunca recebeu email não tem pasta: não há o que apagar, só remove a conta
        if apagar_emails and "does not exist" in str(e):
            return setup("email", "del", "-n", email)
        raise


def set_quota(email: str, quota: str) -> str:
    return setup("quota", "set", email, quota)


def remove_quota(email: str) -> str:
    return setup("quota", "del", email)


@dataclass
class Usage:
    usado_kib: int
    limite_kib: int | None  # None = sem limite
    mensagens: int


def quota_report() -> dict[str, Usage]:
    """Uso real de cada conta (Dovecot), em KiB."""
    out = exec_("doveadm", "-f", "tab", "quota", "get", "-A")
    rep: dict[str, Usage] = {}
    for line in out.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        user, _, kind, value, limit = cols[:5]
        u = rep.setdefault(user, Usage(0, None, 0))
        if kind == "STORAGE":
            u.usado_kib = int(value or 0)
            u.limite_kib = None if limit in ("-", "", "0") else int(limit)
        elif kind == "MESSAGE":
            u.mensagens = int(value or 0)
    return rep


QUOTA_RE = re.compile(r"^(0|\d+(\.\d+)?[KMGT]?)$", re.I)


def human_kib(kib: int | None) -> str:
    if kib is None:
        return "sem limite"
    size = float(kib)
    for unit in ("KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "KB" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ---------- aliases ----------

def list_aliases() -> list[tuple[str, str]]:
    out = setup("alias", "list")
    rows = []
    for line in out.splitlines():
        m = re.match(r"\*\s+(\S+)\s+(.+)", line)
        if m:
            for dest in m[2].split(","):
                rows.append((m[1], dest.strip()))
    return rows


def add_alias(alias: str, dest: str) -> str:
    return setup("alias", "add", alias, dest)


def del_alias(alias: str, dest: str) -> str:
    return setup("alias", "del", alias, dest)


# ---------- fila ----------

def queue() -> list[dict]:
    out = exec_("postqueue", "-j")
    items = []
    for line in out.splitlines():
        try:
            q = json.loads(line)
        except json.JSONDecodeError:
            continue
        for r in q.get("recipients", []):
            items.append({
                "id": q.get("queue_id"),
                "fila": q.get("queue_name"),
                "de": q.get("sender"),
                "para": r.get("address"),
                "motivo": r.get("delay_reason", ""),
                "tamanho": q.get("message_size"),
            })
    return items


def flush_queue() -> str:
    return exec_("postqueue", "-f")


def delete_queued(queue_id: str) -> str:
    return exec_("postsuper", "-d", queue_id)


# ---------- logs / segurança ----------

def mail_log(lines: int = 200) -> str:
    return exec_("tail", "-n", str(lines), "/var/log/mail/mail.log")


def banned_ips() -> list[str]:
    out = setup("fail2ban")
    return re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", out)


def unban(ip: str) -> str:
    return setup("fail2ban", "unban", ip)


def dkim_record(domain: str) -> str | None:
    try:
        raw = exec_("cat", f"/tmp/docker-mailserver/rspamd/dkim/{domain}-mail.public.dns.txt")
    except DmsError:
        return None
    parts = re.findall(r'"([^"]*)"', raw)
    return "".join(parts).strip() if parts else raw.strip()


# ---------- DNS ----------

def dig(name: str, rtype: str) -> list[str]:
    proc = subprocess.run(["dig", "+short", rtype, name, "@8.8.8.8"],
                          capture_output=True, text=True, timeout=10)
    return [l.strip().strip('"') for l in proc.stdout.splitlines() if l.strip()]


# ---------- acesso das caixas ----------

ACCOUNTS = "/tmp/docker-mailserver/postfix-accounts.cf"
SUSPENSO = "!"  # prefixo no hash: a senha deixa de conferir, mas a original volta ao reativar


def suspended() -> set[str]:
    out = exec_("cat", ACCOUNTS, check=False)
    return {l.split("|", 1)[0] for l in out.splitlines() if "|" in l and "}" + SUSPENSO in l}


def _edit_hash(email: str, old: str, new: str) -> None:
    if not EMAIL_RE.match(email):
        raise DmsError("Endereço inválido.")
    # Endereço escapado para o sed; o arquivo é observado pelo servidor, que recarrega sozinho
    esc = re.sub(r"([.\[\]*^$\\/])", r"\\\1", email)
    exec_("sed", "-i", f"s/^{esc}|\\({{[^}}]*}}\\){old}/{esc}|\\1{new}/", ACCOUNTS)


def suspend(email: str) -> None:
    """Bloqueia login (IMAP, SMTP e webmail). A caixa continua recebendo emails."""
    _edit_hash(email, "", SUSPENSO)


def reactivate(email: str) -> None:
    _edit_hash(email, re.escape(SUSPENSO), "")


LOGIN = re.compile(r"^(\S+) .*?(?:imap-login: Logged in: user=<([^>]+)>.*?rip=([^,]+)"
                   r"|client=[^\[]*\[([^\]]+)\], sasl_method=\w+, sasl_username=(\S+))")


def last_logins() -> dict[str, tuple[str, str]]:
    """Último acesso de cada caixa (IMAP, webmail ou SMTP autenticado): {email: (iso, ip)}."""
    out = exec_("sh", "-c", "zcat -f /var/log/mail/mail.log* 2>/dev/null | grep -E "
                "'imap-login: Logged in|sasl_username='", check=False, timeout=120)
    last: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        m = LOGIN.match(line)
        if not m:
            continue
        user, ip = (m[2], m[3]) if m[2] else (m[5], m[4])
        if user not in last or m[1] > last[user][0]:
            last[user] = (m[1], ip)
    return last


def cert_expiry(host: str) -> str:
    out = exec_("openssl", "x509", "-enddate", "-noout", "-in",
                f"/etc/letsencrypt/live/{host}/fullchain.pem", check=False)
    return out.partition("=")[2].strip()


def relay_host() -> str:
    out = exec_("cat", "/tmp/docker-mailserver/postfix-relaymap.cf", check=False)
    m = re.search(r"\[([^\]]+)\]", out)
    return m[1] if m else ""


# ---------- webmail (nome do remetente no Roundcube) ----------

def set_webmail_name(email: str, name: str) -> bool:
    """Atualiza o nome da identidade padrão no Roundcube. False se a caixa nunca entrou no webmail."""
    php = ('$d=new PDO("sqlite:/var/roundcube/db/sqlite.db");'
           '$s=$d->prepare("update identities set name=? where email=? and del=0");'
           '$s->execute([$argv[2],$argv[1]]); echo $s->rowCount();')
    out = _run(["docker", "exec", "webmail", "php", "-r", php, "--", email, name], check=False)
    return out.strip().isdigit() and int(out.strip()) > 0
