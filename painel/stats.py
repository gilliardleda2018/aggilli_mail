"""Estatísticas a partir dos logs do servidor (Postfix e Rspamd), incluindo os rotacionados."""
import re
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

import dms

TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?([+-]\d{2}:\d{2})")
QMGR_FROM = re.compile(r"postfix/qmgr\[\d+\]: ([0-9A-F]+): from=<([^>]*)>, size=(\d+)")
DELIVERY = re.compile(r"postfix/(lmtp|smtp)\[\d+\]: ([0-9A-F]+): to=<([^>]*)>,.*?status=(\w+)(?: \((.*)\))?")
REJECT = re.compile(r"NOQUEUE: reject: RCPT from ([^\[]*)\[([^\]]+)\]: \d{3} [\d.]+ (?:<[^>]*>: )?([^;]+);"
                    r" from=<([^>]*)> to=<([^>]*)>")
POSTSCREEN = re.compile(r"postscreen\[\d+\]: (PREGREET|DNSBL|HANGUP|BARE NEWLINE|COMMAND PIPELINING)")
RSPAMD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) .*rspamd_task_write_log: .*?qid: <([^>]*)>, "
                    r"ip: ([^,]+),.*?from: <([^>]*)>, \(default: [TF] \(([^)]+)\): \[(-?[\d.]+)/")

SPAM_ACTIONS = {"add header", "rewrite subject", "reject", "soft reject"}


def _local_time(m: re.Match) -> datetime:
    """Converte o horário do log (com fuso) para o horário local da máquina, sem fuso."""
    return datetime.fromisoformat(m[1] + m[2]).astimezone().replace(tzinfo=None)


def _read(pattern: str) -> list[str]:
    # zcat -f lê tanto os arquivos normais quanto os rotacionados .gz
    out = dms.exec_("sh", "-c", f"zcat -f {pattern} 2>/dev/null", check=False, timeout=120)
    return out.splitlines()


@dataclass
class Stats:
    entregas: pd.DataFrame   # data, tipo, de, para, status, detalhe
    recusas: pd.DataFrame    # data, ip, motivo, de, para
    spam: pd.DataFrame       # data, acao, nota, ip, de
    bloqueios_entrada: int   # conexões barradas pelo postscreen antes do SMTP


def collect(domain: str) -> Stats:
    senders: dict[str, str] = {}
    entregas, recusas = [], []
    bloqueios = 0
    for line in _read("/var/log/mail/mail.log*"):
        ts = TS.match(line)
        if not ts:
            continue
        if m := QMGR_FROM.search(line):
            senders[m[1]] = m[2]
        elif m := DELIVERY.search(line):
            via, qid, to, status, detail = m.groups()
            if via == "lmtp":
                tipo = "recebido"
            else:
                tipo = "enviado" if not to.endswith("@" + domain) else "interno"
            entregas.append((_local_time(ts), tipo, qid, to, status, detail or ""))
        elif m := REJECT.search(line):
            recusas.append((_local_time(ts), m[2], m[3].strip(), m[4], m[5]))
        elif POSTSCREEN.search(line):
            bloqueios += 1

    ent = pd.DataFrame(entregas, columns=["data", "tipo", "qid", "para", "status", "detalhe"])
    ent["de"] = ent["qid"].map(senders).fillna("")
    # Uma mensagem adiada que depois saiu conta só como enviada: fica o último status de cada destinatário
    ent = ent.sort_values("data").drop_duplicates(["qid", "para"], keep="last")
    rec = pd.DataFrame(recusas, columns=["data", "ip", "motivo", "de", "para"])

    spam = []
    for line in _read("/var/log/mail/rspamd.log*"):
        if m := RSPAMD.search(line):
            spam.append((datetime.fromisoformat(m[1]), m[5], float(m[6]), m[3], m[4]))
    sp = pd.DataFrame(spam, columns=["data", "acao", "nota", "ip", "de"])
    return Stats(ent, rec, sp, bloqueios)


def since(df: pd.DataFrame, start: datetime | None) -> pd.DataFrame:
    return df if start is None or df.empty else df[df["data"] >= start]


def daily(stats: Stats, start: datetime | None) -> pd.DataFrame:
    """Tabela por dia: recebidos, enviados, devolvidos, recusados, spam."""
    e = since(stats.entregas, start)
    parts = {
        "Recebidos": e[(e.tipo == "recebido") & (e.status == "sent")],
        "Enviados": e[(e.tipo != "recebido") & (e.status == "sent")],
        "Devolvidos": e[e.status == "bounced"],
        "Recusados": since(stats.recusas, start),
        "Spam": since(stats.spam, start).query("acao in @SPAM_ACTIONS"),
    }
    series = {k: v.groupby(v["data"].dt.date).size() for k, v in parts.items() if not v.empty}
    if not series:
        return pd.DataFrame(columns=list(parts))
    df = pd.DataFrame(series).fillna(0).astype(int)
    df.index = pd.to_datetime(df.index)
    full = pd.date_range(df.index.min(), df.index.max(), freq="D")
    df = df.reindex(full, fill_value=0).reindex(columns=list(parts), fill_value=0)
    df.index = df.index.strftime("%d/%m")  # rótulo por dia: barras lado a lado, sem eixo de horas
    return df


def short_reason(detail: str) -> str:
    """Resume a resposta do servidor remoto numa frase curta."""
    d = re.sub(r"\s+", " ", detail)
    if "not authorized to" in d and "SMTP relay" in d:
        return "Gmail recusa IP residencial (precisa de relay)"
    if m := re.search(r"said: (\d{3}[- ][\d.]+ .{0,90})", d):
        return m[1]
    return d[:110]
