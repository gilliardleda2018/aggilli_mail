#!/usr/bin/env bash
# Prepara a VPS como "porta de entrada" do servidor de email que roda em casa.
#
#  Internet --25/465/587/993--> VPS (HAProxy) --Tailscale + PROXY protocol--> casa (docker-mailserver)
#  casa --Tailscale:2525--> VPS (Postfix relay) --25--> internet   (saída com IP e PTR da VPS)
#
# Uso (como root, Debian 12+/Ubuntu 22.04+, inclusive Oracle Cloud Free):
#   bash setup-vps.sh
#   RELAY=0 bash setup-vps.sh   # só entrada (provedor bloqueia porta 25 de saída, ex.: Oracle)
set -euo pipefail

HOME_TS_IP="${HOME_TS_IP:?informe HOME_TS_IP (IP Tailscale da máquina de casa)}"
MAIL_HOST="${MAIL_HOST:?informe MAIL_HOST}"
RELAY_PORT=2525
RELAY="${RELAY:-1}"                              # 1 = também relay de saída

[[ $EUID -eq 0 ]] || { echo "rode como root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "== Pacotes"
apt-get update -q
apt-get install -y -q curl haproxy nftables
if [[ $RELAY == 1 ]]; then
  # postfix sem perguntas interativas
  debconf-set-selections <<<"postfix postfix/main_mailer_type select Internet Site"
  debconf-set-selections <<<"postfix postfix/mailname string $MAIL_HOST"
  apt-get install -y -q postfix
fi

echo "== Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale ip -4 >/dev/null 2>&1; then
  echo ">>> Abra o link abaixo e autorize esta VPS na sua conta Tailscale <<<"
  tailscale up --hostname=mail-vps ${TS_AUTHKEY:+--authkey="$TS_AUTHKEY"}
fi
VPS_TS_IP="$(tailscale ip -4)"
echo "IP Tailscale da VPS: $VPS_TS_IP"
ping -c1 -W3 "$HOME_TS_IP" >/dev/null && echo "casa alcançável via Tailscale" \
  || echo "AVISO: casa ($HOME_TS_IP) não respondeu ao ping"

echo "== HAProxy (entrada)"
cat > /etc/haproxy/haproxy.cfg <<EOF
global
    log /dev/log local0
    maxconn 2000

defaults
    mode tcp
    log global
    option tcplog
    timeout connect 10s
    timeout client  5m
    timeout server  5m

frontend smtp
    bind :25
    default_backend smtp
backend smtp
    server casa $HOME_TS_IP:12525 send-proxy-v2

frontend submissions
    bind :465
    default_backend submissions
backend submissions
    server casa $HOME_TS_IP:12465 send-proxy-v2

frontend submission
    bind :587
    default_backend submission
backend submission
    server casa $HOME_TS_IP:12587 send-proxy-v2

frontend imaps
    bind :993
    default_backend imaps
backend imaps
    server casa $HOME_TS_IP:12993 send-proxy-v2
EOF
haproxy -c -f /etc/haproxy/haproxy.cfg

if [[ $RELAY == 1 ]]; then
echo "== Postfix (saída: relay só para a máquina de casa)"
postconf -e \
  "myhostname = $MAIL_HOST" \
  "myorigin = $MAIL_HOST" \
  "mydestination =" \
  "relay_domains =" \
  "local_transport = error:sem entrega local" \
  "inet_interfaces = all" \
  "inet_protocols = ipv4" \
  "mynetworks = 127.0.0.0/8 $HOME_TS_IP/32" \
  "smtpd_client_restrictions = permit_mynetworks, reject" \
  "smtpd_relay_restrictions = permit_mynetworks, reject" \
  "smtp_tls_security_level = may" \
  "smtp_tls_loglevel = 1" \
  "message_size_limit = 26214400"
# Postfix não escuta na 25 (é do HAProxy); escuta na 2525 para o relay
postconf -M# smtp/inet
postconf -M "$RELAY_PORT/inet=$RELAY_PORT inet n - y - - smtpd"
fi

echo "== Firewall"
# Imagens Ubuntu da Oracle trazem regras iptables que bloqueiam tudo menos SSH
if systemctl list-unit-files netfilter-persistent.service >/dev/null 2>&1; then
  systemctl disable --now netfilter-persistent || true
  rm -f /etc/iptables/rules.v4 /etc/iptables/rules.v6
fi
cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    meta l4proto { icmp, ipv6-icmp } accept
    tcp dport 22 accept
    tcp dport { 25, 465, 587, 993 } accept
    iifname "tailscale0" tcp dport $RELAY_PORT accept
    udp dport 41641 accept
  }
  chain forward { type filter hook forward priority 0; policy accept; }
  chain output  { type filter hook output priority 0; policy accept; }
}
EOF
systemctl enable --now nftables
nft -f /etc/nftables.conf
systemctl restart tailscaled   # recria as regras do Tailscale apagadas pelo flush

systemctl enable haproxy
systemctl restart haproxy
if [[ $RELAY == 1 ]]; then systemctl enable postfix; systemctl restart postfix; fi

echo
echo "== Pronto"
echo "IP público da VPS:     $(curl -fsS4 https://api.ipify.org || echo '?')"
echo "IP Tailscale da VPS:   $VPS_TS_IP"
echo
[[ $RELAY == 1 ]] || { echo "Modo só entrada (RELAY=0): os emails continuam saindo direto de casa."; exit 0; }
echo "Saída na porta 25 (o provedor precisa liberar):"
timeout 5 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25 && echo "  liberada"' \
  || echo "  BLOQUEADA: peça ao provedor da VPS para liberar a porta 25 de saída"
