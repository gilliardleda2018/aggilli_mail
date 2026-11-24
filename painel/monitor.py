#!/usr/bin/env python3
"""Monitor do servidor: roda as verificações e manda email quando algo quebra, volta ao normal
ou continua quebrado. Rodado pelo cron a cada 10 minutos; `--teste` manda um email de teste."""
import json
import subprocess
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import checks
import dms

AQUI = Path(__file__).resolve().parent
ESTADO = AQUI / "monitor-estado.json"
HISTORICO = AQUI / "avisos.jsonl"
MARK = "# monitor-email (painel)"
GRAVIDADE = {"ok": 0, "aviso": 1, "erro": 2}


def _remetente() -> str:
    return f"postmaster@{checks.read_env().get('DOMAIN', '')}"


def send(to: str, assunto: str, corpo: str) -> None:
    msg = EmailMessage()
    msg["From"] = f"Aggilli Mail <{_remetente()}>"
    msg["To"] = to
    msg["Subject"] = assunto
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=_remetente().split("@")[1])
    msg.set_content(corpo)
    dms.sendmail(_remetente(), msg.as_bytes())


def history(n: int = 100) -> list[dict]:
    if not HISTORICO.exists():
        return []
    linhas = HISTORICO.read_text().splitlines()[-n:]
    return [json.loads(l) for l in reversed(linhas) if l.strip()]


def _log(ev: dict) -> None:
    with HISTORICO.open("a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def run() -> list[dict]:
    """Uma rodada: compara com a anterior, registra os eventos e avisa por email."""
    cfg = checks.settings()
    agora = datetime.now()
    estado = json.loads(ESTADO.read_text()) if ESTADO.exists() else {}
    try:
        resultado = checks.run_all()
    except Exception as e:  # o próprio monitor quebrou: isso também é um problema
        resultado = [checks.Check("monitor", "erro", "Monitor", f"falha ao verificar: {e}")]

    eventos, novo = [], {}
    for c in resultado:
        ant = estado.get(c.chave)
        reg = {"nivel": c.nivel, "desde": ant["desde"] if ant and ant["nivel"] == c.nivel else agora.isoformat(),
               "avisado": ant.get("avisado") if ant else None}
        tipo = None
        if c.nivel != "ok":
            piorou = not ant or GRAVIDADE[c.nivel] > GRAVIDADE[ant["nivel"]]
            velho = reg["avisado"] and agora - datetime.fromisoformat(reg["avisado"]) > \
                timedelta(hours=cfg["repetir_horas"])
            tipo = "problema" if piorou else "lembrete" if velho else None
        elif ant and ant["nivel"] != "ok":
            tipo = "resolvido"
        if tipo:
            eventos.append({"quando": agora.isoformat(timespec="seconds"), "tipo": tipo, "chave": c.chave,
                            "nivel": c.nivel, "titulo": c.titulo, "detalhe": c.detalhe})
            reg["avisado"] = agora.isoformat() if tipo != "resolvido" else None
        novo[c.chave] = reg
    # Verificação que sumiu (ex.: servidor parado esconde as outras) mantém o estado anterior
    ESTADO.write_text(json.dumps({**estado, **novo}, indent=1))

    if eventos:
        enviado, erro = False, ""
        if cfg["email"]:
            try:
                send(cfg["email"], _assunto(eventos), _corpo(eventos, resultado))
                enviado = True
            except Exception as e:
                erro = str(e)[:200]
        for ev in eventos:
            _log({**ev, "email": enviado, **({"erro": erro} if erro else {})})
    return eventos


def _assunto(eventos: list[dict]) -> str:
    problemas = [e for e in eventos if e["tipo"] != "resolvido"]
    if not problemas:
        return f"[Aggilli Mail] Resolvido: {', '.join(e['titulo'] for e in eventos)}"
    marca = "ERRO" if any(e["nivel"] == "erro" for e in problemas) else "Aviso"
    return f"[Aggilli Mail] {marca}: {', '.join(e['titulo'] for e in problemas)}"


def _corpo(eventos: list[dict], todos: list) -> str:
    rot = {"problema": "PROBLEMA", "lembrete": "CONTINUA", "resolvido": "RESOLVIDO"}
    linhas = ["Mudanças detectadas no servidor de email:", ""]
    linhas += [f"  {rot[e['tipo']]:<10} {e['titulo']}: {e['detalhe']}" for e in eventos]
    linhas += ["", "Situação completa agora:", ""]
    linhas += [f"  [{c.nivel.upper():^5}] {c.titulo}: {c.detalhe}" for c in todos]
    linhas += ["", f"Verificado em {datetime.now():%d/%m/%Y %H:%M}. Abra o painel para mais detalhes."]
    return "\n".join(linhas)


# ---------- agendamento ----------

def _crontab() -> list[str]:
    p = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return p.stdout.splitlines() if p.returncode == 0 else []


def scheduled() -> bool:
    return any(l.endswith(MARK) for l in _crontab())


def set_scheduled(on: bool) -> None:
    lines = [l for l in _crontab() if not l.endswith(MARK)]
    if on:
        lines.append(f"*/10 * * * * cd {AQUI} && .venv/bin/python monitor.py >> {AQUI.parent}/monitor.log "
                     f"2>&1 {MARK}")
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


if __name__ == "__main__":
    if "--teste" in sys.argv:
        to = checks.settings()["email"]
        if not to:
            sys.exit("Nenhum email de aviso configurado.")
        send(to, "[Aggilli Mail] Teste de aviso", "Este é um teste: os avisos do servidor chegam aqui.")
        print("Teste enviado para", to)
    else:
        evs = run()
        print(datetime.now().strftime("%F %T"), f"{len(evs)} evento(s)",
              "; ".join(f"{e['tipo']} {e['chave']}" for e in evs))
