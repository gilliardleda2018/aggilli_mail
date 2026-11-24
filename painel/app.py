"""Painel de administração do servidor de email (docker-mailserver)."""
import base64
import json
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import auth
import backup
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

def page_overview():
    st.header("Visão geral")
    s = dms.container_status()
    if not s.get("existe"):
        st.error("Container `mailserver` não encontrado. Rode `./mail.sh up`.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Servidor", "rodando" if s["rodando"] else "parado")
    c2.metric("Saúde", s["saude"])
    boxes = dms.list_mailboxes()
    c3.metric("Caixas", len(boxes))
    c4.metric("Na fila", len(dms.queue()))
    st.caption(f"Ligado desde {s['desde']} UTC · {MAIL_HOST}")

    st.subheader("Serviços")
    st.dataframe(pd.DataFrame(dms.services(), columns=["Serviço", "Estado"]),
                 hide_index=True, width="stretch")

    st.subheader("Acesso nos clientes de email")
    st.table(pd.DataFrame([
        ["Entrada (IMAP)", MAIL_HOST, 993, "SSL/TLS"],
        ["Saída (SMTP)", MAIL_HOST, 465, "SSL/TLS"],
    ], columns=["", "Servidor", "Porta", "Segurança"]))
    st.caption("Usuário: o email completo.")


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


def create_mailbox(email: str, password: str, quota: str) -> None:
    dms.add_mailbox(email, password)
    if quota != "0":
        dms.set_quota(email, quota)


def show_generated():
    """Mostra uma única vez as senhas geradas na última ação."""
    gen = st.session_state.get("geradas")
    if not gen:
        return
    st.success("Copie as senhas agora: elas não serão mostradas de novo.")
    df = pd.DataFrame(gen, columns=["Email", "Senha", "Cota"])
    st.dataframe(df, hide_index=True, width="stretch")
    c1, c2 = st.columns([1, 4])
    c1.download_button("Baixar CSV", df.to_csv(index=False), "caixas-criadas.csv", "text/csv")
    if c2.button("Já copiei, esconder"):
        del st.session_state["geradas"]
        st.rerun()


def page_mailboxes():
    st.header("Caixas de email")
    cfg = load_config()
    show_generated()

    boxes = dms.list_mailboxes()
    usage = dms.quota_report()
    rows = []
    for b in boxes:
        u = usage.get(b.email, dms.Usage(0, None, 0))
        pct = round(100 * u.usado_kib / u.limite_kib, 1) if u.limite_kib else 0.0
        rows.append({"Email": b.email, "Usado": dms.human_kib(u.usado_kib),
                     "Limite": dms.human_kib(u.limite_kib), "Uso": pct,
                     "Mensagens": u.mensagens, "Aliases": ", ".join(b.aliases)})

    c1, c2, c3 = st.columns(3)
    c1.metric("Caixas", len(boxes))
    c2.metric("Espaço usado", dms.human_kib(sum(usage.get(b.email, dms.Usage(0, None, 0)).usado_kib
                                                for b in boxes)))
    c3.metric("Acima de 80% da cota", sum(r["Uso"] >= 80 for r in rows))
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            "Uso": st.column_config.ProgressColumn("Uso da cota", format="%.0f%%",
                                                   min_value=0, max_value=100)})
        for r in rows:
            if r["Uso"] >= 90:
                st.warning(f"{r['Email']} está com {r['Uso']:.0f}% da cota: novos emails podem ser recusados.")
    else:
        st.info("Nenhuma caixa ainda.")

    emails = [b.email for b in boxes]
    t_new, t_bulk, t_quota, t_pwd, t_del = st.tabs(
        ["➕ Nova caixa", "📥 Criar em lote", "📦 Cotas", "🔑 Trocar senha", "🗑️ Remover"])

    with t_new:
        user = st.text_input("Nome", placeholder="financeiro", key="new_user")
        st.caption(f"Endereço: **{(user or 'nome').strip().lower()}@{DOMAIN}**")
        modo = st.radio("Senha", ["Gerar senha forte", "Definir senha"], horizontal=True, key="new_mode")
        p1 = p2 = ""
        if modo == "Definir senha":
            p1 = st.text_input("Senha", type="password", key="new_p1")
            p2 = st.text_input("Repita a senha", type="password", key="new_p2")
        quota = quota_picker("Cota", "new_quota", cfg["cota_padrao"])
        if st.button("Criar caixa", type="primary", key="new_btn"):
            email = f"{user.strip().lower()}@{DOMAIN}"
            pwd = gen_password() if modo == "Gerar senha forte" else p1
            if not dms.EMAIL_RE.match(email):
                st.error("Nome inválido. Use letras, números, ponto, hífen ou sublinhado.")
            elif email in emails:
                st.error("Essa caixa já existe.")
            elif quota is None:
                st.error("Cota inválida.")
            elif modo == "Definir senha" and (err := validate_password(p1, p2)):
                st.error(err)
            else:
                try:
                    create_mailbox(email, pwd, quota)
                except dms.DmsError as e:
                    st.error(str(e))
                else:
                    if modo == "Gerar senha forte":
                        st.session_state.geradas = [(email, pwd, quota)]
                    st.toast(f"{email} criada", icon="✅")
                    st.rerun()

    with t_bulk:
        st.write("Uma caixa por linha: `nome` ou `nome,cota` (ex.: `vendas,10G`). "
                 f"Sem cota, usa a padrão ({cfg['cota_padrao']}). As senhas são geradas.")
        texto = st.text_area("Caixas", height=150, placeholder="vendas\nsuporte,2G\njoao.silva,10G")
        if st.button("Criar todas", type="primary", key="bulk_btn"):
            criadas, erros = [], []
            for raw in [l.strip() for l in texto.splitlines() if l.strip()]:
                nome, _, q = (x.strip() for x in raw.partition(","))
                email = nome.lower() if "@" in nome else f"{nome.lower()}@{DOMAIN}"
                q = (q or cfg["cota_padrao"]).upper()
                q = "0" if q in ("0", "SEM LIMITE") else q
                if not dms.EMAIL_RE.match(email) or not email.endswith("@" + DOMAIN):
                    erros.append(f"{raw}: nome inválido")
                elif email in emails:
                    erros.append(f"{raw}: já existe")
                elif not dms.QUOTA_RE.match(q):
                    erros.append(f"{raw}: cota inválida")
                else:
                    pwd = gen_password()
                    try:
                        create_mailbox(email, pwd, q)
                        criadas.append((email, pwd, q))
                    except dms.DmsError as e:
                        erros.append(f"{raw}: {e}")
            st.session_state.lote_erros = erros
            if criadas:
                st.session_state.geradas = criadas
                st.rerun()
        for e in st.session_state.pop("lote_erros", []):
            st.error(e)

    with t_quota:
        alvo = st.multiselect("Caixas", emails, key="q_boxes")
        q = quota_picker("Nova cota", "q_value", cfg["cota_padrao"])
        if st.button("Aplicar cota", type="primary", disabled=not alvo or q is None, key="q_btn"):
            def apply():
                for e in alvo:
                    dms.remove_quota(e) if q == "0" else dms.set_quota(e, q)
            run(apply, f"Cota aplicada em {len(alvo)} caixa(s)")
        st.divider()
        st.subheader("Cota padrão para novas caixas")
        padrao = st.selectbox("Cota padrão", QUOTA_PRESETS,
                              index=QUOTA_PRESETS.index(cfg["cota_padrao"])
                              if cfg["cota_padrao"] in QUOTA_PRESETS else 2, key="q_default")
        if st.button("Salvar padrão", key="q_default_btn"):
            save_config({**cfg, "cota_padrao": padrao})
            st.toast(f"Cota padrão: {padrao}", icon="✅")
            st.rerun()
        st.caption("Ao atingir a cota, o servidor recusa novos emails para a caixa até liberar espaço.")

    with t_pwd:
        email = st.selectbox("Caixa", emails, key="pwd_box")
        modo = st.radio("Senha", ["Gerar senha forte", "Definir senha"], horizontal=True, key="pwd_mode")
        p1 = p2 = ""
        if modo == "Definir senha":
            p1 = st.text_input("Nova senha", type="password", key="pwd_p1")
            p2 = st.text_input("Repita a senha", type="password", key="pwd_p2")
        if st.button("Trocar senha", type="primary", key="pwd_btn") and email:
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

    with t_del:
        email = st.selectbox("Caixa", emails, key="del_box")
        apagar = st.checkbox("Apagar também todos os emails guardados (irreversível)")
        confirma = st.text_input("Digite o endereço para confirmar", key="del_confirm")
        if st.button("Remover caixa", type="primary", disabled=confirma != email or not email):
            run(lambda: dms.delete_mailbox(email, apagar), f"{email} removida")


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
        ("TXT", DOMAIN, f"v=spf1 mx ip4:{ENV.get('HOME_PUBLIC_IP', '')} -all"),
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


def page_logs():
    st.header("Logs")
    c1, c2 = st.columns([1, 3])
    n = c1.selectbox("Linhas", [100, 300, 1000], index=1)
    filtro = c2.text_input("Filtrar", placeholder="ex.: contato@, reject, status=sent")
    lines = dms.mail_log(n).splitlines()
    if filtro:
        lines = [l for l in lines if filtro.lower() in l.lower()]
    st.code("\n".join(lines[-n:]) or "(nada)", language="log")


PAGES = {
    "Visão geral": page_overview,
    "Webmail": page_webmail,
    "Caixas": page_mailboxes,
    "Aliases": page_aliases,
    "Fila": page_queue,
    "Segurança": page_security,
    "DNS": page_dns,
    "Backup": page_backup,
    "Logs": page_logs,
}

if login():
    with st.sidebar:
        brand_title(DOMAIN)
        page = st.radio("Menu", list(PAGES), label_visibility="collapsed")
        st.divider()
        if st.button("Sair"):
            st.session_state.clear()
            st.rerun()
    try:
        PAGES[page]()
    except dms.DmsError as e:
        st.error(f"Erro ao falar com o servidor: {e}")
