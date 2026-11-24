#!/usr/bin/env bash
# Atalhos para operar o servidor de email.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
source .env
: "${DOMAIN:?defina DOMAIN no .env}" "${MAIL_HOST:?defina MAIL_HOST no .env}"

usage() {
  cat <<EOF
Uso: ./mail.sh <comando>

  check            Verifica pré-requisitos (docker, portas, DNS)
  certs            Emite o certificado TLS (Let's Encrypt) para $MAIL_HOST (precisa da porta 80)
  certs-dns        Emite o certificado validando por registro TXT no DNS (sem porta 80)
  up               Sobe o servidor
  down             Para o servidor
  logs             Acompanha os logs
  add <email>      Cria uma conta (pede a senha)
  del <email>      Remove uma conta
  passwd <email>   Troca a senha de uma conta
  list             Lista as contas
  alias <de> <para>  Cria um alias
  dkim             Gera a chave DKIM (rode após criar a primeira conta)
  dns [IP_VPS]     Mostra os registros DNS que você precisa criar
  status           Status do container e dos serviços internos

  VPS (entrada/saída pela VPS, necessário por causa do CGNAT):
  vps-deploy <user@IP> [0]  Instala e configura a VPS por SSH (0 = só entrada, ex.: Oracle Free)
  vps-link <IP_TS_VPS>     Faz a saída de emails passar pela VPS e testa a ligação
  vps-unlink               Volta a enviar direto desta máquina
  vps-test [IP_PUBLICO]    Testa as portas da VPS vistas da internet

  Painel web:
  painel           Inicia o painel em http://$TAILSCALE_IP:8501 (só pelo Tailscale)
  painel-senha     Define/troca a senha do painel

  Backup:
  backup [MANTER] [DESTINO_EXTRA]  Backup completo em backups/ (mantém os MANTER mais recentes, padrão 7)
  restore <arquivo.tar.gz>         Restaura um backup (os dados atuais vão para data.antes-<data>)
EOF
}

# Usa o grupo docker mesmo em shells abertos antes de entrar no grupo
docker() {
  if command docker info >/dev/null 2>&1; then command docker "$@"
  else sg docker -c "$(printf '%q ' docker "$@")"; fi
}

dms() { if [ -t 0 ]; then docker exec -it mailserver setup "$@"; else docker exec -i mailserver setup "$@"; fi; }

wait_tailscale() {
  for _ in $(seq 1 60); do
    ip -4 addr show 2>/dev/null | grep -q "inet ${TAILSCALE_IP}/" && return 0
    sleep 5
  done
  echo "Tailscale sem o IP $TAILSCALE_IP após 5 min" >&2; return 1
}

set_env() {  # set_env CHAVE valor  (no mailserver.env)
  if grep -q "^#\?\s*$1=" mailserver.env; then
    sed -i "s|^#\?\s*$1=.*|$1=$2|" mailserver.env
  else echo "$1=$2" >> mailserver.env; fi
}

cmd_vps_deploy() {
  local target="${1:?informe usuario@IP_DA_VPS (Oracle: ubuntu@IP)}" relay="${2:-1}"
  local key=(); [[ -f vps/id_ed25519 ]] && key=(-i vps/id_ed25519)
  scp "${key[@]}" -o StrictHostKeyChecking=accept-new vps/setup-vps.sh "$target:/tmp/setup-vps.sh"
  ssh "${key[@]}" -t "$target" \
    "sudo HOME_TS_IP=$TAILSCALE_IP MAIL_HOST=$MAIL_HOST RELAY=$relay bash /tmp/setup-vps.sh"
  echo
  if [[ $relay == 1 ]]; then
    echo "Agora rode: ./mail.sh vps-link <IP Tailscale da VPS mostrado acima>"
  else
    echo "Agora troque no DNS: A mail -> IP público da VPS, e o SPF (veja ./mail.sh dns)"
  fi
}

cmd_vps_link() {
  local vts="${1:?informe o IP Tailscale da VPS (100.x.y.z)}"
  echo "== Testando relay da VPS ($vts:2525)"
  timeout 8 bash -c "exec 3<>/dev/tcp/$vts/2525; head -1 <&3" \
    || { echo "VPS não respondeu na 2525 via Tailscale; confira o setup-vps.sh"; return 1; }
  set_env DEFAULT_RELAY_HOST "[$vts]:2525"
  docker compose up -d mailserver
  echo
  echo "Saída agora passa pela VPS. Falta no DNS da Hostinger:"
  echo "  A   mail  ->  <IP público da VPS>   (substitui o IP antigo)"
  echo "E no painel da VPS: PTR do IP público -> $MAIL_HOST"
}

# Itens do backup (relativos à pasta do projeto). data/ inteiro guarda emails, contas,
# chaves DKIM, certificado, estado do anti-spam e o webmail.
BACKUP_ITEMS=(data .env mailserver.env compose.yaml mail.sh painel/config.json painel/.senha
              vps/id_ed25519 vps/id_ed25519.pub vps/vm-ip.txt vps/setup-vps.sh)

cmd_backup() {
  local keep="${1:-7}" extra="${2:-}" ts name items=()
  ts="$(date +%Y-%m-%d_%H%M%S)"; name="backup-email-$ts.tar.gz"
  mkdir -p backups
  for i in "${BACKUP_ITEMS[@]}"; do [[ -e $i ]] && items+=("$i"); done
  # Roda como root num container para ler arquivos de root/dovecot, preservando dono e permissões
  docker run --rm -v "$PWD:/src:ro" -v "$PWD/backups:/out" alpine sh -c \
    "cd /src && tar --numeric-owner -czpf /out/$name.part ${items[*]} 2>/tmp/err; \
     rc=\$?; grep -v 'socket ignored' /tmp/err >&2; \
     [ \$rc -le 1 ] && mv /out/$name.part /out/$name && chown $(id -u):$(id -g) /out/$name"
  [[ -f backups/$name ]] || { echo "ERRO: backup falhou" >&2; return 1; }
  tar -tzf "backups/$name" >/dev/null || { echo "ERRO: arquivo corrompido" >&2; return 1; }
  echo "OK backups/$name $(du -h "backups/$name" | cut -f1)"
  if [[ -n $extra ]]; then
    mkdir -p "$extra" && cp "backups/$name" "$extra/" && echo "copiado para $extra"
    ls -1t "$extra"/backup-email-*.tar.gz 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm -f
  fi
  ls -1t backups/backup-email-*.tar.gz | tail -n +"$((keep + 1))" | xargs -r rm -f
}

cmd_restore() {
  local file="${1:?informe o arquivo de backup}" ts
  [[ -f $file ]] || { echo "arquivo não encontrado: $file" >&2; return 1; }
  tar -tzf "$file" data/config >/dev/null 2>&1 || { echo "não parece um backup deste servidor" >&2; return 1; }
  ts="$(date +%Y-%m-%d_%H%M%S)"
  docker compose stop mailserver webmail
  file="$(realpath "$file")"
  docker run --rm -v "$PWD:/w" -v "$file:/backup.tar.gz:ro" alpine sh -c \
    "cd /w && mv data data.antes-$ts && tar --numeric-owner -xzpf /backup.tar.gz data"
  wait_tailscale && docker compose up -d mailserver webmail
  echo "Restaurado. Dados anteriores guardados em data.antes-$ts"
}

cmd_vps_test() {
  local ip="${1:-$(getent ahostsv4 "$MAIL_HOST" | awk 'NR==1{print $1}')}"
  echo "== Portas em $ip"
  for p in 25 465 587 993; do
    if timeout 6 bash -c "exec 3<>/dev/tcp/$ip/$p" 2>/dev/null; then echo "  $p aberta"
    else echo "  $p FECHADA"; fi
  done
  echo "== Banner SMTP"
  timeout 8 bash -c "exec 3<>/dev/tcp/$ip/25; head -1 <&3" || true
}

public_ip() {
  curl -fsS4 https://api.ipify.org 2>/dev/null || echo "<SEU_IP_PUBLICO>"
}

cmd_check() {
  echo "== Docker"
  if docker info >/dev/null 2>&1; then echo "ok"; else
    echo "sem acesso ao Docker. Rode: sudo usermod -aG docker \$USER  (e faça logout/login)"; fi

  echo "== Portas em uso localmente (precisam estar livres: 25 80 143 465 587 993)"
  ss -ltn | awk 'NR>1{print $4}' | grep -E ':(25|80|143|465|587|993)$' || echo "todas livres"

  echo "== Saída na porta 25 (muitos provedores residenciais bloqueiam)"
  if timeout 5 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
    echo "ok, porta 25 de saída liberada"; else
    echo "BLOQUEADA: você não conseguirá entregar emails diretamente; use um relay (veja README)"; fi

  echo "== DNS"
  echo "IP público: $(public_ip)"
  echo "A    $MAIL_HOST -> $(getent hosts "$MAIL_HOST" | awk '{print $1}' || true)"
  echo "MX   $DOMAIN -> $(dig +short MX "$DOMAIN" 2>/dev/null || echo '(instale dnsutils p/ ver)')"
}

cmd_dns() {
  local home_ip; home_ip="$(public_ip)"
  local ip="${1:-$home_ip}"   # com VPS: ./mail.sh dns <IP público da VPS>
  local dkim_file="data/config/rspamd/dkim/$DOMAIN-mail.public.dns.txt"
  cat <<EOF
Crie estes registros no painel DNS de $DOMAIN:

Tipo  Nome                Valor
----  ------------------  -----------------------------------------------
A     $MAIL_HOST        $ip
MX    $DOMAIN             10 $MAIL_HOST
TXT   $DOMAIN             v=spf1 mx ip4:$home_ip -all
TXT   _dmarc.$DOMAIN      v=DMARC1; p=quarantine; rua=mailto:postmaster@$DOMAIN; adkim=s; aspf=s
EOF
  if [[ -f "$dkim_file" ]]; then
    echo "TXT   mail._domainkey.$DOMAIN"
    echo "      $(tr -d '\n' < "$dkim_file" | sed -E 's/.*\(\s*//; s/\s*\).*//; s/"\s*"//g; s/"//g')"
  else
    echo "TXT   mail._domainkey.$DOMAIN  (rode ./mail.sh dkim primeiro)"
  fi
  cat <<EOF

E peça ao seu provedor de hospedagem/VPS o DNS reverso (PTR):
PTR   $ip -> $MAIL_HOST
EOF
}

case "${1:-}" in
  check)  cmd_check ;;
  certs)  mkdir -p data/certs && docker compose --profile certs run --rm --service-ports certbot ;;
  certs-dns)
          mkdir -p data/certs && docker compose --profile certs run --rm certbot \
            certonly --manual --preferred-challenges dns --agree-tos \
            --register-unsafely-without-email -d "$MAIL_HOST" ;;
  up)     mkdir -p data && wait_tailscale && docker compose up -d mailserver webmail ;;
  vps-deploy) cmd_vps_deploy "${2:-}" "${3:-1}" ;;
  vps-link)   cmd_vps_link "${2:-}" ;;
  vps-unlink) set_env DEFAULT_RELAY_HOST "" && docker compose up -d mailserver ;;
  vps-test)   cmd_vps_test "${2:-}" ;;
  painel)     wait_tailscale && cd painel && exec .venv/bin/streamlit run app.py \
                --server.address "$TAILSCALE_IP" --server.port 8501 \
                --server.headless true --browser.gatherUsageStats false ;;
  painel-senha) painel/.venv/bin/python painel/definir_senha.py ;;
  backup)     cmd_backup "${2:-7}" "${3:-}" ;;
  restore)    cmd_restore "${2:-}" ;;
  down)   docker compose down ;;
  logs)   docker compose logs -f mailserver ;;
  add)    dms email add "${2:?informe o email}" ;;
  del)    dms email del "${2:?informe o email}" ;;
  passwd) dms email update "${2:?informe o email}" ;;
  list)   dms email list ;;
  alias)  dms alias add "${2:?origem}" "${3:?destino}" ;;
  dkim)   dms config dkim domain "$DOMAIN" && cmd_dns ;;
  dns)    cmd_dns "${2:-}" ;;
  status) docker compose ps && docker exec mailserver supervisorctl status ;;
  *)      usage ;;
esac
