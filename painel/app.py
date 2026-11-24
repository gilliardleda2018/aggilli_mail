"""Painel de administração do servidor de email (docker-mailserver)."""
import base64
import json
import secrets
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import auth
import backup
import stats
import dms

BASE = Path(__file__).resolve().parent.parent


def read_env() -> dict:
    env = {}
    for line in (BASE / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = read_env()
DOMAIN = ENV.get("DOMAIN", "")
MAIL_HOST = ENV.get("MAIL_HOST", DOMAIN)
VM_IP_FILE = BASE / "vps" / "vm-ip.txt"
VM_IP = VM_IP_FILE.read_text().strip() if VM_IP_FILE.exists() else ""
WEBMAIL_URL = f"http://{ENV.get('TAILSCALE_IP', 'localhost')}:8502"

MARCA = Path(__file__).resolve().parent / "marca"
st.set_page_config(page_title="Aggilli Mail", page_icon=str(MARCA / "favicon-32x32.png"),
                   layout="wide")
st.logo(str(MARCA / "logo-512.png"), size="large", icon_image=str(MARCA / "icon-192x192.png"))

# Identidade Aggilli: faixa ciano→verde no topo e títulos em azul petróleo
st.markdown("""
<style>
  [data-testid="stHeader"] { border-top: 4px solid; border-image: linear-gradient(90deg,#00A8D8,#50A000) 1; }
  h1, h2, h3 { color: #004860 !important; }
  .aggilli-nome { font-size: 1.6rem; font-weight: 700; color: #004860; line-height: 1.1; }
  .aggilli-nome span { color: #3D7F00; }
  [data-testid="stSidebar"] { background: #F2F9FB; }
  [data-testid="stMetric"] { background: #fff; border: 1px solid #D3E8EF; border-radius: 12px;
      padding: 12px 16px; box-shadow: 0 1px 3px rgba(0,72,96,.06); }
  [data-testid="stMetricValue"] { color: #004860; }
  .ag-banner { padding: 12px 18px; border-radius: 12px; font-weight: 600; margin-bottom: 1rem; }
  .ag-banner.ok { background: #EAF6E0; color: #2F6600; border-left: 6px solid #50A000; }
  .ag-banner.aviso { background: #FFF4E5; color: #8A4B00; border-left: 6px solid #E08A1E; }
  .ag-checks { border: 1px solid #D3E8EF; border-radius: 12px; background: #fff; }
  .ag-check { display: flex; gap: 12px; align-items: center; padding: 10px 14px;
      border-bottom: 1px solid #EEF5F8; }
  .ag-check:last-child { border-bottom: none; }
  .ag-check small { color: #4A6470; }
  .ag-dot { flex: 0 0 26px; height: 26px; border-radius: 50%; display: flex; align-items: center;
      justify-content: center; font-weight: 700; color: #fff; font-size: .85rem; }
  .ag-dot.ok { background: #50A000; } .ag-dot.aviso { background: #E08A1E; } .ag-dot.erro { background: #C0392B; }
  .ag-card { border: 1px solid #D3E8EF; border-radius: 12px; background: #fff; padding: 14px 16px;
      line-height: 1.5; }
  .ag-ficha { margin-bottom: .5rem; }
  .ag-ficha-nome { font-size: 1.35rem; font-weight: 700; color: #004860; }
  .ag-ficha-email { color: #4A6470; }
  .ag-tag { font-size: .75rem; font-weight: 600; padding: 2px 10px; border-radius: 999px;
      vertical-align: middle; margin-left: 6px; }
  .ag-tag.ativa { background: #EAF6E0; color: #2F6600; }
  .ag-tag.susp { background: #FDECEA; color: #A93226; }
</style>
""", unsafe_allow_html=True)


def brand_title(sub: str = "") -> None:
    st.markdown(f'<div class="aggilli-nome">Aggilli <span>Mail</span></div>', unsafe_allow_html=True)
    if sub:
        st.caption(sub)


# ---------------- login ----------------

def login() -> bool:
    if st.session_state.get("ok"):
        return True
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        logo = base64.b64encode((MARCA / "logo-512.png").read_bytes()).decode()
        st.markdown(f"""
<div style="text-align:center; margin: 2rem 0 1.5rem;">
  <img src="data:image/png;base64,{logo}" alt="Aggilli" style="width:220px; max-width:70%;">
  <div class="aggilli-nome" style="font-size:2.2rem; margin-top:.5rem;">Aggilli <span>Mail</span></div>
  <div style="color:#4A6470; margin-top:.25rem;">Painel do servidor de email</div>
</div>""", unsafe_allow_html=True)
        if not auth.configured():
            st.info("Primeiro acesso: crie a senha do painel.")
            with st.form("primeira_senha"):
                p1 = st.text_input("Nova senha (mínimo 10 caracteres)", type="password")
                p2 = st.text_input("Repita a senha", type="password")
                if st.form_submit_button("Salvar senha", type="primary", width="stretch"):
                    if err := validate_password(p1, p2):
                        st.error(err)
                    elif auth.configured():  # alguém salvou enquanto esta tela estava aberta
                        st.error("A senha já foi definida. Recarregue a página.")
                    else:
                        auth.save(p1)
                        st.session_state.ok = True
                        st.rerun()
            return False
        with st.form("login"):
            pwd = st.text_input("Senha", type="password")
            if st.form_submit_button("Entrar", type="primary", width="stretch"):
                if auth.check(pwd):
                    st.session_state.ok = True
                    st.rerun()
                time.sleep(2)
                st.error("Senha incorreta.")
    return False


def run(action, ok_msg: str):
    """Executa uma ação no servidor e mostra o resultado."""
    try:
        action()
        st.toast(ok_msg, icon="✅")
        time.sleep(0.6)
        st.rerun()
    except dms.DmsError as e:
        st.error(str(e))


def validate_password(p1: str, p2: str) -> str | None:
    if len(p1) < 10:
        return "A senha precisa ter pelo menos 10 caracteres."
    if p1 != p2:
        return "As senhas não conferem."
    return None


# ---------------- páginas ----------------

def quando(iso: str | None) -> str:
    """Data do log em texto curto: 'hoje 13:18', 'ontem 09:02', 'há 5 dias'."""
    if not iso:
        return "sem registro"
    dt = datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)
    dias = (datetime.now().date() - dt.date()).days
    if dias == 0:
        return f"hoje {dt:%H:%M}"
    if dias == 1:
        return f"ontem {dt:%H:%M}"
    return f"há {dias} dias" if dias < 30 else f"{dt:%d/%m/%Y}"


def check_list(items: list[tuple[str, str, str]]) -> None:
    """Lista de verificações: (nível ok|aviso|erro, título, detalhe)."""
    icon = {"ok": ("✓", "ok"), "aviso": ("!", "aviso"), "erro": ("✕", "erro")}
    html = "".join(
        f'<div class="ag-check"><span class="ag-dot {icon[n][1]}">{icon[n][0]}</span>'
        f'<div><b>{t}</b><br><small>{d}</small></div></div>' for n, t, d in items)
    st.markdown(f'<div class="ag-checks">{html}</div>', unsafe_allow_html=True)


def page_overview():
    st.header("Visão geral")
    s = dms.container_status()
    if not s.get("existe"):
        st.error("Container `mailserver` não encontrado. Rode `./mail.sh up`.")
        return
    boxes = dms.list_mailboxes()
    fila = dms.queue()
    usage = dms.quota_report()
    st_ = load_stats()
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    e = stats.since(st_.entregas, hoje)
    recebidos = e[(e.tipo == "recebido") & (e.status == "sent")]
    enviados = e[(e.tipo != "recebido") & (e.status == "sent")]
    falhas = e[e.status == "bounced"]

    checks = []
    rodando = s["rodando"] and s["saude"] == "healthy"
    checks.append(("ok" if rodando else "erro", "Servidor",
                   f"{'funcionando' if rodando else s['saude']} desde {s['desde']} UTC"))
    svcs = dms.services()
    parados = [n for n, est in svcs if est != "RUNNING"]
    checks.append(("ok" if not parados else "erro", "Serviços",
                   f"{len(svcs) - len(parados)} de {len(svcs)} rodando"
                   + (f" · parados: {', '.join(parados)}" if parados else "")))
    relay = dms.relay_host()
    # O que vale é o último envio para fora: devoluções antigas já resolvidas não acendem alerta
    ext = st_.entregas[st_.entregas.tipo == "enviado"]
    ultimo_ok = ext.empty or ext.sort_values("data").status.iloc[-1] == "sent"
    checks.append(("ok" if relay and ultimo_ok else "aviso", "Envio de emails",
                   ("pela Oracle Email Delivery" if "oracle" in relay else relay or "direto, sem relay")
                   + ("" if ultimo_ok else " · o último envio foi recusado, veja Estatísticas")
                   + (f" · {len(falhas)} devolvido(s) hoje" if not falhas.empty else "")))
    venc = dms.cert_expiry(MAIL_HOST)
    if venc:
        dias = (datetime.strptime(venc, "%b %d %H:%M:%S %Y %Z") - datetime.now()).days
        checks.append(("ok" if dias > 30 else "aviso" if dias > 7 else "erro", "Certificado TLS",
                       f"vence em {dias} dias" + (" · renove com `./mail.sh certs-dns`" if dias <= 30 else "")))
    else:
        checks.append(("erro", "Certificado TLS", "não encontrado"))
    bks = backup.list_backups()
    if bks:
        horas = (datetime.now() - bks[0].quando).total_seconds() / 3600
        checks.append(("ok" if horas <= 30 else "aviso", "Backup",
                       f"último em {bks[0].quando:%d/%m %H:%M}" + (" · atrasado" if horas > 30 else "")))
    else:
        checks.append(("aviso", "Backup", "nenhum backup feito"))
    adiadas = [i for i in fila if i["fila"] == "deferred"]
    checks.append(("ok" if not adiadas else "aviso", "Fila de envio",
                   "vazia" if not fila else f"{len(fila)} mensagem(ns), {len(adiadas)} com atraso"))
    cheias = [u for u, x in usage.items() if x.limite_kib and x.usado_kib >= 0.9 * x.limite_kib]
    checks.append(("ok" if not cheias else "aviso", "Cotas",
                   "nenhuma caixa perto do limite" if not cheias else f"quase cheias: {', '.join(cheias)}"))

    problemas = sum(n != "ok" for n, _, _ in checks)
    if problemas == 0:
        st.markdown('<div class="ag-banner ok">Tudo funcionando</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="ag-banner aviso">{problemas} ponto(s) precisam de atenção</div>',
                    unsafe_allow_html=True)

    c = st.columns(4)
    c[0].metric("Caixas", len(boxes))
    c[1].metric("Recebidos hoje", len(recebidos))
    c[2].metric("Enviados hoje", len(enviados))
    c[3].metric("Na fila", len(fila))

    esq, dir_ = st.columns([3, 2], gap="large")
    with esq:
        st.subheader("Situação")
        check_list(checks)
    with dir_:
        st.subheader("Atalhos")
        if st.button("Nova caixa de email", icon=":material/add:", type="primary", width="stretch"):
            st.session_state.abrir_nova = True
            st.switch_page(PG["caixas"])
        st.link_button("Abrir webmail", WEBMAIL_URL, icon=":material/mail:", width="stretch")
        if st.button("Ver estatísticas", icon=":material/insights:", width="stretch"):
            st.switch_page(PG["estatisticas"])
        if st.button("Backup", icon=":material/backup:", width="stretch"):
            st.switch_page(PG["backup"])

        st.subheader("Programas de email")
        st.markdown(f"""
<div class="ag-card">
<b>Entrada (IMAP)</b><br><code>{MAIL_HOST}</code> · porta 993 · SSL/TLS<br><br>
<b>Saída (SMTP)</b><br><code>{MAIL_HOST}</code> · porta 465 · SSL/TLS<br><br>
<small>Usuário: o email completo. Senha: a da caixa.</small>
</div>""", unsafe_allow_html=True)

    with st.expander("Serviços do servidor"):
        st.dataframe(pd.DataFrame(svcs, columns=["Serviço", "Estado"]), hide_index=True, width="stretch")


CONFIG_FILE = Path(__file__).with_name("config.json")
QUOTA_PRESETS = ["1G", "2G", "5G", "10G", "20G", "50G", "sem limite"]


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {"cota_padrao": "5G"}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def gen_password() -> str:
    return secrets.token_urlsafe(12)


def quota_picker(label: str, key: str, default: str) -> str | None:
    """Seletor de cota. Devolve '5G', '0' (sem limite) ou None se inválido."""
    opts = QUOTA_PRESETS + ["personalizado"]
    choice = st.selectbox(label, opts, index=opts.index(default) if default in opts else 2, key=key)
    if choice == "personalizado":
        val = st.text_input("Valor (ex.: 750M, 3G)", key=f"{key}_custom").strip().upper()
        return val if dms.QUOTA_RE.match(val) else None
    return "0" if choice == "sem limite" else choice


def create_mailbox(email: str, password: str, quota: str, nome: str = "") -> None:
    dms.add_mailbox(email, password)
    if quota != "0":
        dms.set_quota(email, quota)
    if nome:
        cfg = load_config()
        cfg.setdefault("caixas", {})[email] = {"nome": nome}
        save_config(cfg)


def show_generated():
    """Mostra uma única vez as senhas geradas na última ação."""
    gen = st.session_state.get("geradas")
    if not gen:
        return
    with st.container(border=True):
        st.success("Copie as senhas agora: elas não serão mostradas de novo.")
        df = pd.DataFrame(gen, columns=["Email", "Senha", "Cota"])
        st.dataframe(df, hide_index=True, width="stretch")
        c1, c2 = st.columns([1, 4])
        c1.download_button("Baixar CSV", df.to_csv(index=False), "caixas-criadas.csv", "text/csv")
        if c2.button("Já copiei, esconder"):
            del st.session_state["geradas"]
            st.rerun()


@st.dialog("Nova caixa de email")
def dialog_new(emails: list[str], cfg: dict):
    user = st.text_input("Endereço", placeholder="financeiro", key="new_user")
    st.caption(f"Vai ficar **{(user or 'nome').strip().lower()}@{DOMAIN}**")
    nome = st.text_input("Nome de exibição (opcional)", placeholder="Financeiro Aggilli", key="new_nome")
    modo = st.radio("Senha", ["Gerar senha forte", "Definir senha"], horizontal=True, key="new_mode")
    p1 = p2 = ""
    if modo == "Definir senha":
        p1 = st.text_input("Senha", type="password", key="new_p1")
        p2 = st.text_input("Repita a senha", type="password", key="new_p2")
    quota = quota_picker("Cota", "new_quota", cfg["cota_padrao"])
    if st.button("Criar caixa", type="primary", key="new_btn", width="stretch"):
        email = f"{user.strip().lower()}@{DOMAIN}"
        pwd = gen_password() if modo == "Gerar senha forte" else p1
        if not dms.EMAIL_RE.match(email):
            st.error("Endereço inválido. Use letras, números, ponto, hífen ou sublinhado.")
        elif email in emails:
            st.error("Essa caixa já existe.")
        elif quota is None:
            st.error("Cota inválida.")
        elif modo == "Definir senha" and (err := validate_password(p1, p2)):
            st.error(err)
        else:
            try:
                create_mailbox(email, pwd, quota, nome.strip())
            except dms.DmsError as e:
                st.error(str(e))
            else:
                if modo == "Gerar senha forte":
                    st.session_state.geradas = [(email, pwd, quota)]
                st.session_state.selecionar = email
                st.toast(f"{email} criada", icon="✅")
                st.rerun()


@st.dialog("Criar caixas em lote", width="large")
def dialog_bulk(emails: list[str], cfg: dict):
    st.write("Uma caixa por linha: `endereço`, `endereço,cota` ou `endereço,cota,Nome de exibição`. "
             f"Sem cota, usa a padrão ({cfg['cota_padrao']}). As senhas são geradas.")
    texto = st.text_area("Caixas", height=180,
                         placeholder="vendas\nsuporte,2G\njoao.silva,10G,João Silva")
    if st.button("Criar todas", type="primary", key="bulk_btn", width="stretch"):
        criadas, erros = [], []
        for raw in [l.strip() for l in texto.splitlines() if l.strip()]:
            partes = [x.strip() for x in raw.split(",", 2)] + ["", ""]
            nome, q, exib = partes[:3]
            email = nome.lower() if "@" in nome else f"{nome.lower()}@{DOMAIN}"
            q = (q or cfg["cota_padrao"]).upper()
            q = "0" if q in ("0", "SEM LIMITE") else q
            if not dms.EMAIL_RE.match(email) or not email.endswith("@" + DOMAIN):
                erros.append(f"{raw}: endereço inválido")
            elif email in emails:
                erros.append(f"{raw}: já existe")
            elif not dms.QUOTA_RE.match(q):
                erros.append(f"{raw}: cota inválida")
            else:
                pwd = gen_password()
                try:
                    create_mailbox(email, pwd, q, exib)
                    criadas.append((email, pwd, q))
                except dms.DmsError as e:
                    erros.append(f"{raw}: {e}")
        st.session_state.lote_erros = erros
        if criadas:
            st.session_state.geradas = criadas
        st.rerun()


def mailbox_card(email: str, info: dict, cfg: dict) -> None:
    """Ficha de uma caixa, com as ações de edição."""
    meta = cfg.get("caixas", {}).get(email, {})
    k = email  # chaves dos campos mudam com a caixa, para não herdar valores da anterior
    with st.container(border=True):
        tag = ('<span class="ag-tag susp">Suspensa</span>' if info["suspensa"]
               else '<span class="ag-tag ativa">Ativa</span>')
        titulo = meta.get("nome") or email.split("@")[0]
        st.markdown(f'<div class="ag-ficha"><div><span class="ag-ficha-nome">{titulo}</span> {tag}</div>'
                    f'<div class="ag-ficha-email">{email}</div></div>', unsafe_allow_html=True)
        c = st.columns(4)
        c[0].metric("Ocupado", info["usado"], help=f"Limite: {info['limite']}")
        c[1].metric("Mensagens", info["mensagens"])
        c[2].metric("Último acesso", info["acesso"],
                    help=f"Do IP {info['ip']}" if info["ip"] else "Nenhum acesso nas últimas 4 semanas de log")
        c[3].metric("Aliases", len(info["aliases"]))
        if info["aliases"]:
            st.caption("Também recebe por: " + ", ".join(info["aliases"]))

        t_dados, t_cota, t_senha, t_acesso, t_rm = st.tabs(
            [":material/badge: Dados", ":material/inventory_2: Cota", ":material/key: Senha",
             ":material/block: Acesso", ":material/delete: Remover"])

        with t_dados:
            with st.form(f"dados_{k}", border=False):
                nome = st.text_input("Nome de exibição", value=meta.get("nome", ""),
                                     help="Aparece como remetente nos emails enviados pelo webmail.")
                obs = st.text_input("Responsável / observação", value=meta.get("obs", ""),
                                    placeholder="ex.: Maria, setor financeiro")
                if st.form_submit_button("Salvar", type="primary"):
                    cfg.setdefault("caixas", {})[email] = {**meta, "nome": nome.strip(), "obs": obs.strip()}
                    save_config(cfg)
                    sync = dms.set_webmail_name(email, nome.strip())
                    st.toast("Dados salvos" + ("" if sync else ". O nome vai para o webmail no primeiro acesso "
                                               "da caixa (salve de novo depois)."), icon="✅")
                    time.sleep(0.6)
                    st.rerun()

        with t_cota:
            q = quota_picker("Cota", f"q_{k}", info["limite_cfg"])
            if st.button("Aplicar cota", type="primary", disabled=q is None, key=f"qbtn_{k}"):
                run(lambda: dms.remove_quota(email) if q == "0" else dms.set_quota(email, q),
                    f"Cota de {email}: {'sem limite' if q == '0' else q}")
            st.caption("Ao atingir a cota, o servidor recusa novos emails para a caixa até liberar espaço.")

        with t_senha:
            modo = st.radio("Senha", ["Gerar senha forte", "Definir senha"], horizontal=True, key=f"pm_{k}")
            p1 = p2 = ""
            if modo == "Definir senha":
                p1 = st.text_input("Nova senha", type="password", key=f"p1_{k}")
                p2 = st.text_input("Repita a senha", type="password", key=f"p2_{k}")
            if st.button("Trocar senha", type="primary", key=f"pbtn_{k}"):
                pwd = gen_password() if modo == "Gerar senha forte" else p1
                if modo == "Definir senha" and (err := validate_password(p1, p2)):
                    st.error(err)
                else:
                    try:
                        dms.update_password(email, pwd)
                    except dms.DmsError as e:
                        st.error(str(e))
                    else:
                        if modo == "Gerar senha forte":
                            st.session_state.geradas = [(email, pwd, "-")]
                        st.toast(f"Senha de {email} trocada", icon="✅")
                        st.rerun()
            if info["suspensa"]:
                st.caption("Trocar a senha de uma caixa suspensa também a reativa.")

        with t_acesso:
            if info["suspensa"]:
                st.warning("Esta caixa está suspensa: ninguém consegue entrar nela (webmail, celular ou "
                           "programa de email). Os emails continuam chegando normalmente.")
                if st.button("Reativar caixa", type="primary", icon=":material/lock_open:", key=f"re_{k}"):
                    run(lambda: dms.reactivate(email), f"{email} reativada")
            else:
                st.write("Suspender bloqueia o acesso à caixa sem apagar nada: os emails continuam "
                         "chegando e a senha atual volta a funcionar ao reativar. Útil quando alguém sai "
                         "da empresa ou a senha pode ter vazado.")
                ok = st.checkbox("Entendi, suspender o acesso", key=f"sc_{k}")
                if st.button("Suspender caixa", icon=":material/block:", disabled=not ok, key=f"su_{k}"):
                    run(lambda: dms.suspend(email), f"{email} suspensa")

        with t_rm:
            apagar = st.checkbox("Apagar também todos os emails guardados (irreversível)", key=f"ra_{k}")
            confirma = st.text_input("Digite o endereço para confirmar", key=f"rc_{k}")
            if st.button("Remover caixa", type="primary", disabled=confirma != email, key=f"rb_{k}"):
                def remover():
                    dms.delete_mailbox(email, apagar)
                    cfg.get("caixas", {}).pop(email, None)
                    save_config(cfg)
                run(remover, f"{email} removida")


def page_mailboxes():
    st.header("Caixas de email")
    cfg = load_config()
    boxes = dms.list_mailboxes()
    emails = [b.email for b in boxes]
    if st.session_state.pop("abrir_nova", False):
        dialog_new(emails, cfg)
    show_generated()
    for e in st.session_state.pop("lote_erros", []):
        st.error(e)

    usage = dms.quota_report()
    susp = dms.suspended()
    logins = dms.last_logins()
    meta = cfg.get("caixas", {})
    infos, rows = {}, []
    for b in boxes:
        u = usage.get(b.email, dms.Usage(0, None, 0))
        pct = round(100 * u.usado_kib / u.limite_kib, 1) if u.limite_kib else 0.0
        iso, ip = logins.get(b.email, (None, ""))
        if u.limite_kib is None:
            limite_cfg = "sem limite"
        elif u.limite_kib % 1_048_576 == 0:
            limite_cfg = f"{u.limite_kib // 1_048_576}G"
        else:
            limite_cfg = cfg["cota_padrao"]
        infos[b.email] = {"suspensa": b.email in susp, "usado": dms.human_kib(u.usado_kib),
                          "limite": dms.human_kib(u.limite_kib), "mensagens": u.mensagens,
                          "acesso": quando(iso), "ip": ip, "aliases": b.aliases, "limite_cfg": limite_cfg,
                          "pct": pct}
        rows.append({"Situação": "⏸ Suspensa" if b.email in susp else "● Ativa",
                     "Email": b.email, "Nome": meta.get(b.email, {}).get("nome", ""),
                     "Uso": pct, "Ocupado": f"{dms.human_kib(u.usado_kib)} de {dms.human_kib(u.limite_kib)}",
                     "Mensagens": u.mensagens, "Último acesso": quando(iso),
                     "Responsável": meta.get(b.email, {}).get("obs", "")})

    c = st.columns(4)
    c[0].metric("Caixas", len(boxes))
    c[1].metric("Suspensas", len(susp & set(emails)))
    c[2].metric("Espaço usado", dms.human_kib(sum(usage.get(e, dms.Usage(0, None, 0)).usado_kib
                                                  for e in emails)))
    c[3].metric("Acima de 80% da cota", sum(r["Uso"] >= 80 for r in rows))

    c1, c2, c3, c4 = st.columns([3, 1.4, 1, 1], vertical_alignment="bottom")
    busca = c1.text_input("Buscar", placeholder="Buscar por email, nome ou responsável",
                          label_visibility="collapsed", icon=":material/search:")
    filtro = c2.selectbox("Filtro", ["Todas", "Ativas", "Suspensas", "Quase cheias"],
                          label_visibility="collapsed")
    if c3.button("Nova caixa", type="primary", icon=":material/add:", width="stretch"):
        dialog_new(emails, cfg)
    if c4.button("Em lote", icon=":material/playlist_add:", width="stretch"):
        dialog_bulk(emails, cfg)

    df = pd.DataFrame(rows, columns=["Situação", "Email", "Nome", "Uso", "Ocupado", "Mensagens",
                                     "Último acesso", "Responsável"])
    if busca:
        b = busca.strip().lower()
        df = df[df[["Email", "Nome", "Responsável"]].apply(
            lambda r: any(b in str(v).lower() for v in r), axis=1)]
    if filtro == "Ativas":
        df = df[~df.Email.isin(susp)]
    elif filtro == "Suspensas":
        df = df[df.Email.isin(susp)]
    elif filtro == "Quase cheias":
        df = df[df.Uso >= 80]
    df = df.reset_index(drop=True)

    if not boxes:
        st.info("Nenhuma caixa ainda. Clique em **Nova caixa** para começar.")
        return
    if df.empty:
        st.info("Nenhuma caixa encontrada com esse filtro.")
        return

    ev = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key="tabela_caixas", column_config={
                          "Uso": st.column_config.ProgressColumn("Uso da cota", format="%.0f%%",
                                                                 min_value=0, max_value=100)})
    sel = ev.selection.rows
    email = df.Email[sel[0]] if sel else None
    pre = st.session_state.pop("selecionar", None)
    if email is None and len(df) == 1:
        email = df.Email[0]
    elif email is None and pre in infos:
        email = pre
    if email:
        mailbox_card(email, infos[email], cfg)
    else:
        st.caption("Clique numa linha para abrir a ficha da caixa.")

    with st.expander("Cota em várias caixas e cota padrão"):
        alvo = st.multiselect("Caixas", emails, key="q_boxes")
        q = quota_picker("Nova cota", "q_value", cfg["cota_padrao"])
        if st.button("Aplicar cota", type="primary", disabled=not alvo or q is None, key="q_btn"):
            def apply():
                for e in alvo:
                    dms.remove_quota(e) if q == "0" else dms.set_quota(e, q)
            run(apply, f"Cota aplicada em {len(alvo)} caixa(s)")
        st.divider()
        padrao = st.selectbox("Cota padrão para novas caixas", QUOTA_PRESETS,
                              index=QUOTA_PRESETS.index(cfg["cota_padrao"])
                              if cfg["cota_padrao"] in QUOTA_PRESETS else 2, key="q_default")
        if st.button("Salvar padrão", key="q_default_btn"):
            save_config({**cfg, "cota_padrao": padrao})
            st.toast(f"Cota padrão: {padrao}", icon="✅")
            st.rerun()


def page_aliases():
    st.header("Aliases (redirecionamentos)")
    rows = dms.list_aliases()
    if rows:
        st.dataframe(pd.DataFrame(rows, columns=["Alias", "Entrega em"]),
                     hide_index=True, width="stretch")
    with st.form("alias", clear_on_submit=True):
        c1, c2 = st.columns(2)
        alias = c1.text_input("Alias", placeholder="vendas")
        dest = c2.text_input("Entregar em", placeholder=f"contato@{DOMAIN} ou externo@gmail.com")
        if st.form_submit_button("Adicionar alias", type="primary"):
            full = alias.strip().lower()
            full = full if "@" in full else f"{full}@{DOMAIN}"
            if not (dms.EMAIL_RE.match(full) and dms.EMAIL_RE.match(dest.strip())):
                st.error("Endereço inválido.")
            else:
                run(lambda: dms.add_alias(full, dest.strip()), f"{full} → {dest}")
    if rows:
        st.subheader("Remover")
        opt = st.selectbox("Alias", [f"{a} → {d}" for a, d in rows])
        if st.button("Remover alias"):
            a, d = opt.split(" → ")
            run(lambda: dms.del_alias(a, d), f"{a} removido")


def page_queue():
    st.header("Fila de envio")
    items = dms.queue()
    if not items:
        st.success("Fila vazia: tudo entregue.")
    else:
        st.dataframe(pd.DataFrame(items), hide_index=True, width="stretch")
        c1, c2 = st.columns(2)
        if c1.button("Tentar entregar agora"):
            run(dms.flush_queue, "Reenvio disparado")
        qid = c2.selectbox("Apagar mensagem", sorted({i["id"] for i in items}))
        if c2.button("Apagar da fila") and qid:
            run(lambda: dms.delete_queued(qid), f"{qid} apagada")


def page_security():
    st.header("Segurança")
    st.subheader("IPs bloqueados (Fail2ban)")
    ips = dms.banned_ips()
    if not ips:
        st.success("Nenhum IP bloqueado.")
    else:
        st.write(ips)
        ip = st.selectbox("Desbloquear", ips)
        if st.button("Desbloquear IP"):
            run(lambda: dms.unban(ip), f"{ip} desbloqueado")


def page_dns():
    st.header("DNS")
    st.caption("Consulta em tempo real no DNS do Google (8.8.8.8).")
    dkim = dms.dkim_record(DOMAIN) or ""
    checks = [
        ("A", MAIL_HOST, VM_IP or "(IP da VM Oracle)"),
        ("MX", DOMAIN, f"10 {MAIL_HOST}."),
        ("TXT", DOMAIN, f"v=spf1 mx ip4:{ENV.get('HOME_PUBLIC_IP', '')} include:rp.oracleemaildelivery.com -all"),
        ("TXT", f"_dmarc.{DOMAIN}", "v=DMARC1; p=quarantine"),
        ("TXT", f"mail._domainkey.{DOMAIN}", dkim[:60] + "…"),
    ]
    rows = []
    for rtype, name, expected in checks:
        got = dms.dig(name, rtype)
        exp = expected.rstrip("…")
        ok = any(g.replace('" "', "").startswith(exp[:40]) for g in got)
        rows.append({"OK": "✅" if ok else "❌", "Tipo": rtype, "Nome": name,
                     "Atual": " | ".join(got)[:120] or "(vazio)", "Esperado": expected})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with st.expander("Valor completo do DKIM"):
        st.code(dkim or "(chave DKIM não encontrada)")


def page_webmail():
    st.header("Webmail")
    c1, c2 = st.columns([1, 3])
    c1.link_button("Abrir em nova aba", WEBMAIL_URL, type="primary")
    c2.caption(f"{WEBMAIL_URL} · entre com o email completo e a senha da caixa.")
    st.iframe(WEBMAIL_URL, height=800)


def page_backup():
    st.header("Backup")
    st.caption("Inclui emails, contas, chaves DKIM, certificado, webmail e configurações.")
    backups = backup.list_backups()
    sched = backup.schedule()

    c1, c2, c3 = st.columns(3)
    if backups:
        idade = datetime.now() - backups[0].quando
        horas = idade.total_seconds() / 3600
        c1.metric("Último backup", backups[0].quando.strftime("%d/%m %H:%M"),
                  f"há {horas:.0f} h" if horas >= 1 else "agora há pouco",
                  delta_color="inverse" if horas > 30 else "off")
    else:
        c1.metric("Último backup", "nenhum")
    c2.metric("Backups guardados", len(backups))
    c3.metric("Automático", f"todo dia às {sched['hora']:02d}:00" if sched else "desligado")
    if sched and backups and (datetime.now() - backups[0].quando).total_seconds() > 30 * 3600:
        st.warning("O backup automático não roda há mais de 30 horas. Veja o log abaixo.")

    if st.button("Fazer backup agora", type="primary"):
        with st.spinner("Gerando backup…"):
            ok, out = backup.run_backup(sched["manter"] if sched else 7, sched["extra"] if sched else "")
        (st.success if ok else st.error)(out)
        if ok:
            st.rerun()

    st.subheader("Backup automático")
    with st.form("agenda"):
        ativo = st.toggle("Fazer backup todo dia", value=sched is not None)
        c1, c2 = st.columns(2)
        hora = c1.selectbox("Horário", list(range(24)), format_func=lambda h: f"{h:02d}:00",
                            index=sched["hora"] if sched else 3)
        manter = c2.number_input("Quantos manter", 1, 90, sched["manter"] if sched else 7)
        extra = st.text_input("Copiar também para (opcional)", value=sched["extra"] if sched else "",
                              placeholder="/media/hd-externo/backups ou pasta sincronizada com a nuvem")
        st.caption("Os backups ficam em `backups/`, no mesmo disco do servidor. Uma cópia em outro "
                   "disco ou na nuvem protege também contra falha do disco.")
        if st.form_submit_button("Salvar agendamento"):
            try:
                backup.set_schedule(hora if ativo else None, int(manter), extra.strip())
                st.toast("Agendamento salvo", icon="✅")
                st.rerun()
            except (ValueError, subprocess.CalledProcessError) as e:
                st.error(str(e))

    st.subheader("Backups guardados")
    if not backups:
        st.info("Nenhum backup ainda.")
    for b in backups:
        c1, c2, c3 = st.columns([3, 1, 1])
        c1.write(f"**{b.quando:%d/%m/%Y %H:%M}** · {b.tamanho_mb:.1f} MB")
        c2.download_button("Baixar", b.path.read_bytes(), b.path.name, "application/gzip",
                           key=f"dl_{b.path.name}")
        if c3.button("Apagar", key=f"rm_{b.path.name}"):
            b.path.unlink()
            st.rerun()

    if backups:
        with st.expander("Restaurar um backup"):
            st.warning("Restaurar substitui todos os emails e contas pelo conteúdo do backup. "
                       "Os dados atuais não são apagados: ficam guardados em `data.antes-<data>`. "
                       "O servidor fica parado por cerca de 1 minuto.")
            escolha = st.selectbox("Backup", backups, format_func=lambda b: f"{b.quando:%d/%m/%Y %H:%M}")
            conf = st.text_input("Digite RESTAURAR para confirmar")
            if st.button("Restaurar", disabled=conf != "RESTAURAR"):
                with st.spinner("Restaurando…"):
                    ok, out = backup.restore(escolha.path)
                (st.success if ok else st.error)(out)

    log = backup.last_log()
    if log:
        with st.expander("Log do backup automático"):
            st.code(log, language="log")


@st.cache_data(ttl=120, show_spinner="Lendo os logs…")
def load_stats():
    return stats.collect(DOMAIN)


STAT_COLORS = ["#007A9E", "#50A000", "#D9822B", "#C0392B", "#8E44AD"]


def page_stats():
    st.header("Estatísticas")
    s = load_stats()
    periodo = st.segmented_control("Período", ["Hoje", "7 dias", "30 dias", "Tudo"], default="7 dias")
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = {"Hoje": hoje, "7 dias": hoje - timedelta(days=6),
             "30 dias": hoje - timedelta(days=29)}.get(periodo)

    e = stats.since(s.entregas, start)
    rec = stats.since(s.recusas, start)
    sp = stats.since(s.spam, start)
    spam = sp[sp.acao.isin(stats.SPAM_ACTIONS)]
    recebidos = e[(e.tipo == "recebido") & (e.status == "sent")]
    enviados = e[(e.tipo != "recebido") & (e.status == "sent")]
    falhas = e[e.status.isin(["bounced", "deferred"])]

    c = st.columns(5)
    c[0].metric("Recebidos", len(recebidos))
    c[1].metric("Enviados", len(enviados))
    c[2].metric("Falhas de envio", len(falhas))
    c[3].metric("Recusados na entrada", len(rec))
    c[4].metric("Spam detectado", len(spam))

    if not e.empty or not rec.empty:
        st.subheader("Por dia")
        st.bar_chart(stats.daily(s, start), color=STAT_COLORS, stack=False, height=280)
    else:
        st.info("Nenhum movimento no período.")

    ext = s.entregas[s.entregas.tipo == "enviado"].sort_values("data")
    if (not ext.empty and ext.status.iloc[-1] != "sent"
            and any("relay" in stats.short_reason(d) for d in falhas.detalhe)):
        st.error("O Gmail está recusando emails enviados por este servidor: ele não aceita envio direto "
                 "de IP residencial. Veja os detalhes na aba **Falhas de envio**.")

    t_fail, t_rej, t_top, t_spam = st.tabs(
        ["⚠️ Falhas de envio", "🚫 Recusados na entrada", "🏆 Mais ativos", "🛡️ Spam"])
    with t_fail:
        if falhas.empty:
            st.success("Nenhuma falha de envio no período.")
        else:
            st.dataframe(pd.DataFrame({
                "Quando": falhas.data.dt.strftime("%d/%m %H:%M"), "De": falhas.de, "Para": falhas.para,
                "Situação": falhas.status.map({"bounced": "devolvido", "deferred": "tentando de novo"}),
                "Motivo": falhas.detalhe.map(stats.short_reason)}).iloc[::-1],
                hide_index=True, width="stretch")
    with t_rej:
        st.caption(f"Além disso, {s.bloqueios_entrada} conexões foram barradas antes de enviar qualquer email "
                   "(robôs e servidores mal configurados).")
        if rec.empty:
            st.success("Nenhum email recusado no período.")
        else:
            motivos = rec.motivo.str.replace(r"<[^>]*>: ", "", regex=True).value_counts()
            st.dataframe(motivos.rename_axis("Motivo").reset_index(name="Vezes"), hide_index=True,
                         width="stretch")
            st.dataframe(pd.DataFrame({"Quando": rec.data.dt.strftime("%d/%m %H:%M"), "IP": rec.ip,
                                       "De": rec.de, "Para": rec.para, "Motivo": rec.motivo}).iloc[::-1],
                         hide_index=True, width="stretch")
    with t_top:
        c1, c2, c3 = st.columns(3)
        def top(col, title, series):
            col.markdown(f"**{title}**")
            vc = series[series != ""].value_counts().head(10)
            if vc.empty:
                col.caption("Nada no período.")
            else:
                col.dataframe(vc.rename_axis("Endereço").reset_index(name="Emails"), hide_index=True,
                              width="stretch")
        top(c1, "Caixas que mais recebem", recebidos.para)
        top(c2, "Quem mais envia para cá", recebidos.de)
        top(c3, "Destinos externos mais usados", enviados[enviados.tipo == "enviado"].para)
    with t_spam:
        if sp.empty:
            st.info("Nenhuma mensagem analisada no período.")
        else:
            c1, c2 = st.columns(2)
            c1.metric("Mensagens analisadas", len(sp))
            c2.metric("Nota média", f"{sp.nota.mean():.1f}", help="Quanto menor, mais confiável. Acima de 6 "
                      "vai para a pasta de spam; acima de 11 é recusado.")
            acoes = {"no action": "entregue normal", "add header": "marcado como spam",
                     "rewrite subject": "marcado como spam", "greylist": "atrasado (greylist)",
                     "soft reject": "adiado", "reject": "recusado"}
            st.dataframe(sp.acao.map(acoes).fillna(sp.acao).value_counts()
                         .rename_axis("Resultado").reset_index(name="Mensagens"),
                         hide_index=True, width="stretch")

    inicio = min([d.data.min() for d in (s.entregas, s.recusas) if not d.empty], default=None)
    if inicio is not None:
        st.caption(f"Logs disponíveis desde {inicio:%d/%m/%Y %H:%M}. O servidor guarda 4 semanas de log. "
                   "Os dados se atualizam a cada 2 minutos.")


def page_logs():
    st.header("Logs")
    c1, c2 = st.columns([1, 3])
    n = c1.selectbox("Linhas", [100, 300, 1000], index=1)
    filtro = c2.text_input("Filtrar", placeholder="ex.: contato@, reject, status=sent")
    lines = dms.mail_log(n).splitlines()
    if filtro:
        lines = [l for l in lines if filtro.lower() in l.lower()]
    st.code("\n".join(lines[-n:]) or "(nada)", language="log")


PG = {
    "visao": st.Page(page_overview, title="Visão geral", icon=":material/dashboard:", url_path="visao-geral",
                     default=True),
    "estatisticas": st.Page(page_stats, title="Estatísticas", icon=":material/insights:",
                            url_path="estatisticas"),
    "webmail": st.Page(page_webmail, title="Webmail", icon=":material/mail:", url_path="webmail"),
    "caixas": st.Page(page_mailboxes, title="Caixas", icon=":material/inbox:", url_path="caixas"),
    "aliases": st.Page(page_aliases, title="Aliases", icon=":material/alternate_email:", url_path="aliases"),
    "fila": st.Page(page_queue, title="Fila de envio", icon=":material/schedule_send:", url_path="fila"),
    "seguranca": st.Page(page_security, title="Segurança", icon=":material/shield:", url_path="seguranca"),
    "dns": st.Page(page_dns, title="DNS", icon=":material/dns:", url_path="dns"),
    "backup": st.Page(page_backup, title="Backup", icon=":material/backup:", url_path="backup"),
    "logs": st.Page(page_logs, title="Logs", icon=":material/article:", url_path="logs"),
}
MENU = {
    "Painel": [PG["visao"], PG["estatisticas"]],
    "Email": [PG["webmail"], PG["caixas"], PG["aliases"], PG["fila"]],
    "Servidor": [PG["seguranca"], PG["dns"], PG["backup"], PG["logs"]],
}

if login():
    nav = st.navigation(MENU)
    with st.sidebar:
        st.divider()
        st.caption(f"Servidor **{DOMAIN}**")
        if st.button("Sair", icon=":material/logout:"):
            st.session_state.clear()
            st.rerun()
    try:
        nav.run()
    except dms.DmsError as e:
        st.error(f"Erro ao falar com o servidor: {e}")
