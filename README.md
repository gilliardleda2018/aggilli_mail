# Servidor de email (docker-mailserver)

Postfix (SMTP) + Dovecot (IMAP) + Rspamd (anti-spam, DKIM, SPF, DMARC) + Fail2ban, num único container.

## Passo a passo

1. **Acesso ao Docker** (uma vez só, depois faça logout/login):
   `sudo usermod -aG docker $USER`
2. **Domínio**: edite `.env` e troque `example.com` pelo seu domínio.
3. **Pré-requisitos**: `./mail.sh check`
4. **DNS inicial**: crie o registro `A mail.<dominio> -> seu IP público` e espere propagar.
5. **Certificado TLS**: `./mail.sh certs` (usa a porta 80 temporariamente).
6. **Subir**: `./mail.sh up`
7. **Primeira conta** (em até 2 min após subir, senão o container fica esperando):
   `./mail.sh add voce@<dominio>`
8. **DKIM**: `./mail.sh dkim` e depois `./mail.sh down && ./mail.sh up`
9. **DNS completo**: `./mail.sh dns` mostra MX, SPF, DKIM, DMARC e PTR para criar.
10. **Testar**: envie para um Gmail e para https://www.mail-tester.com (meta: 9/10 ou mais).

## Configurar no cliente (Thunderbird, celular etc.)

| | Servidor | Porta | Segurança |
|---|---|---|---|
| Entrada (IMAP) | mail.<dominio> | 993 | SSL/TLS |
| Saída (SMTP) | mail.<dominio> | 465 | SSL/TLS (ou 587 STARTTLS) |

Usuário: o email completo. Senha: a definida no `add`.

## Renovar certificado

O Let's Encrypt vale 90 dias. Agende (ex.: cron mensal):
`docker compose --profile certs run --rm --service-ports certbot renew && docker compose restart mailserver`

## Avisos importantes

- **Porta 25 bloqueada**: conexões residenciais quase sempre bloqueiam saída na 25 e não deixam configurar PTR.
  Nesses casos o email sai mas cai em spam ou é recusado. Soluções: rodar numa VPS que libere a porta 25
  (Hetzner, OVH, etc.) ou usar um relay de saída (Amazon SES, Brevo, Mailgun) com `DEFAULT_RELAY_HOST` no `mailserver.env`.
- **Backup**: tudo fica em `./data/` (emails, contas, chaves DKIM, certificados). Faça backup dessa pasta.
- Portas que precisam estar abertas no firewall/roteador: 25, 80 (só p/ certificado), 143, 465, 587, 993.

## Recebimento via VPS (a conexão de casa está atrás de CGNAT)

A operadora não entrega conexões de entrada para esta máquina, então uma VPS faz a ponte:

- **Entrada:** internet → VPS (HAProxy nas portas 25/465/587/993) → Tailscale → casa (portas 12525/12465/12587/12993, com PROXY protocol para preservar o IP real do remetente).
- **Saída:** casa → Tailscale → VPS (Postfix relay na porta 2525) → internet, usando o IP e o PTR da VPS.

Passos:
1. Contratar a VPS (Debian 12+), com porta 25 de saída liberada.
2. Na VPS, como root: `bash setup-vps.sh` (arquivo em `vps/`) e autorizar no Tailscale.
3. No `mailserver.env`: `DEFAULT_RELAY_HOST=[<IP Tailscale da VPS>]:2525` e reiniciar.
4. DNS: `A mail` → IP público da VPS. No painel da VPS: PTR do IP → `mail.aggillintelligence.com.br`.

## Painel web

- Endereço: http://<TAILSCALE_IP>:8501 (só pelo Tailscale)
- Inicia sozinho no boot (crontab). Manual: `./mail.sh painel`
- Trocar a senha: apagar `painel/.senha` e abrir o painel (mostra a tela de primeiro acesso)
- Código: `painel/app.py` (telas) e `painel/dms.py` (comandos ao servidor)

## Webmail

- Endereço: http://<TAILSCALE_IP>:8502 (só pelo Tailscale), também na página "Webmail" do painel
- Login: email completo e senha da caixa
- Roundcube em `compose.yaml` (serviço `webmail`), dados em `data/roundcube/`

## Backup

- Página "Backup" do painel: fazer agora, baixar, apagar, restaurar e agendar
- Agendado no crontab (padrão: todo dia às 03:00, mantém os 7 mais recentes), arquivos em `backups/`
- Linha de comando: `./mail.sh backup [MANTER] [DESTINO_EXTRA]` e `./mail.sh restore backups/<arquivo>.tar.gz`
- A restauração guarda os dados anteriores em `data.antes-<data>`

## MCP do Aggilli Mail (Aggilli Chat)

A caixa do administrador no Aggilli Chat: ler, buscar, organizar, rascunhar e enviar com confirmação.
Pasta `mcp/`, serviços `mcp-email` e `mcp-caddy` (perfil `mcp`), HTTPS em `mcp-mail.aggillintelligence.com.br`
pela VM da Oracle (portas 80/443). Passo a passo, ferramentas e proteções em [mcp/README.md](mcp/README.md).
Comandos: `./mail.sh mcp-config`, `mcp-up`, `mcp-senha`, `mcp-logs`, `mcp-revogar`, `mcp-down`.
