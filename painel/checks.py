"""Verificações de saúde do servidor, usadas pela visão geral do painel e pelo monitor (cron)."""
import json
import shutil
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import backup
import dms
import oracle
import stats

BASE = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(__file__).with_name("config.json")

PADRAO = {
    "ativo": False,          # monitor automático (cron) ligado
    "email": "",             # quem recebe os avisos
    "cota_pct": 90,          # caixa acima disso gera aviso
    "cert_dias": 30,         # certificado vencendo em menos dias
    "backup_horas": 30,      # último backup mais velho que isso
    "fila_min": 30,          # mensagem presa na fila há mais que isso
    "limite_diario": 200,    # envios/dia permitidos pela Oracle
    "limite_pct": 80,        # avisa ao passar deste percentual do limite
    "disco_pct": 90,         # disco acima disso
    "repetir_horas": 24,     # problema que continua: lembra de novo depois disso
}


@dataclass
class Check:
    chave: str
    nivel: str   # ok | aviso | erro
    titulo: str
    detalhe: str


def read_env() -> dict:
    env = {}
    for line in (BASE / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def settings() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    return {**PADRAO, **cfg.get("avisos", {})}


def _imap_by_vm(vm_ip: str) -> tuple[bool, str]:
    """Abre IMAPS pelo IP público da VM: confirma VM → Tailscale → servidor de casa."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((vm_ip, 993), timeout=8) as raw, \
                ctx.wrap_socket(raw) as s:
            s.settimeout(8)
            banner = s.recv(200).decode(errors="replace")
        return banner.startswith("* OK"), banner.strip()[:60]
    except OSError as e:
        return False, str(e)


def run_all(entregas=None) -> list[Check]:
    """Roda todas as verificações. `entregas` reaproveita o que o painel já leu dos logs."""
    env = read_env()
    host = env.get("MAIL_HOST", env.get("DOMAIN", ""))
    cfg = settings()
    out: list[Check] = []

    s = dms.container_status()
    if not s.get("existe") or not s.get("rodando"):
        return [Check("servidor", "erro", "Servidor", "o container mailserver está parado")]
    saude = s["saude"] == "healthy"
    out.append(Check("servidor", "ok" if saude else "erro", "Servidor",
                     f"{'funcionando' if saude else s['saude']} desde {s['desde']} UTC"))

    svcs = dms.services()
    parados = [n for n, est in svcs if est != "RUNNING"]
    out.append(Check("servicos", "ok" if not parados else "erro", "Serviços",
                     f"{len(svcs) - len(parados)} de {len(svcs)} rodando"
                     + (f" · parados: {', '.join(parados)}" if parados else "")))

    vm_file = BASE / "vps" / "vm-ip.txt"
    if vm_file.exists():
        ok, info = _imap_by_vm(vm_file.read_text().strip())
        out.append(Check("entrada", "ok" if ok else "erro", "Recebimento pela VM Oracle",
                         "VM e túnel respondendo" if ok
                         else f"a VM não alcança o servidor ({info}): emails de fora não chegam"))

    if entregas is None:
        entregas = stats.collect(env.get("DOMAIN", "")).entregas
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    ext = entregas[entregas.tipo == "enviado"].sort_values("data")
    relay = dms.relay_host()
    ultimo_ok = ext.empty or ext.status.iloc[-1] == "sent"
    out.append(Check("envio", "ok" if relay and ultimo_ok else "aviso", "Envio de emails",
                     ("pela Oracle Email Delivery" if "oracle" in relay else relay or "direto, sem relay")
                     + ("" if ultimo_ok else " · o último envio para fora foi recusado")))

    enviados_hoje = int(((ext.data >= hoje) & (ext.status == "sent")).sum())
    lim = cfg["limite_diario"]
    pct = 100 * enviados_hoje / lim if lim else 0
    out.append(Check("limite", "ok" if pct < cfg["limite_pct"] else "aviso" if pct < 100 else "erro",
                     "Limite diário da Oracle",
                     f"{enviados_hoje} de {lim} envios hoje ({pct:.0f}%)"))

    venc = dms.cert_expiry(host)
    if venc:
        dias = (datetime.strptime(venc, "%b %d %H:%M:%S %Y %Z") - datetime.now()).days
        out.append(Check("certificado", "ok" if dias > cfg["cert_dias"] else "aviso" if dias > 7 else "erro",
                         "Certificado TLS", f"vence em {dias} dias"
                         + (" · renove com ./mail.sh certs-dns" if dias <= cfg["cert_dias"] else "")))
    else:
        out.append(Check("certificado", "erro", "Certificado TLS", "não encontrado"))

    bks = backup.list_backups()
    log = backup.last_log(5)
    falhou = bool(log) and not log.splitlines()[-1].startswith("OK")
    if bks:
        horas = (datetime.now() - bks[0].quando).total_seconds() / 3600
        atrasado = horas > cfg["backup_horas"]
        out.append(Check("backup", "aviso" if atrasado or falhou else "ok", "Backup",
                         f"último em {bks[0].quando:%d/%m %H:%M}"
                         + (" · atrasado" if atrasado else "")
                         + (" · a última execução falhou, veja o log" if falhou else "")))
    else:
        out.append(Check("backup", "aviso", "Backup", "nenhum backup feito"))

    fila = dms.queue()
    agora = datetime.now().timestamp()
    presas = [i for i in fila if i["fila"] == "deferred"
              and agora - (i.get("chegada") or agora) > cfg["fila_min"] * 60]
    out.append(Check("fila", "ok" if not presas else "aviso", "Fila de envio",
                     "vazia" if not fila else
                     f"{len(fila)} mensagem(ns)" + (f", {len(presas)} presa(s) há mais de "
                                                   f"{cfg['fila_min']} min" if presas else "")))

    usage = dms.quota_report()
    cheias = [f"{u} ({100 * x.usado_kib / x.limite_kib:.0f}%)" for u, x in usage.items()
              if x.limite_kib and x.usado_kib >= cfg["cota_pct"] / 100 * x.limite_kib]
    out.append(Check("cotas", "ok" if not cheias else "aviso", "Cotas",
                     "nenhuma caixa perto do limite" if not cheias else f"quase cheias: {', '.join(cheias)}"))

    disco = shutil.disk_usage(BASE / "data")
    dpct = 100 * disco.used / disco.total
    out.append(Check("disco", "ok" if dpct < cfg["disco_pct"] else "erro", "Disco",
                     f"{dpct:.0f}% usado · {disco.free / 1e9:.0f} GB livres"))

    if oracle.available():
        try:
            senders = oracle.senders()
            boxes = [b.email for b in dms.list_mailboxes()]
            sem = [b for b in boxes if b not in senders]
            out.append(Check("remetentes", "ok" if not sem else "aviso", "Remetentes na Oracle",
                             "todas as caixas podem enviar" if not sem
                             else f"não podem enviar: {', '.join(sem)}"))
        except oracle.OracleError as e:
            out.append(Check("remetentes", "aviso", "Remetentes na Oracle", str(e)))
    return out
