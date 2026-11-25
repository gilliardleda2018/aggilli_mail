# MCP do Aggilli Mail

Servidor MCP que leva **uma caixa do Aggilli Mail** (a do administrador, por padrão
`contato@mail.aggillintelligence.com.br`) para o [Aggilli Chat](https://chat.aggillintelligence.com.br).
Mesmo modelo do `mcp_nextcloud`: OAuth 2.1 com cadastro automático do chat e uma senha de autorização que
só o administrador tem.

## Decisões (25/11/2026)

| Pergunta | Decisão |
|---|---|
| Quem conecta | **Só o administrador**: uma caixa fixa no servidor; a tela de autorização pede `MCP_ADMIN_PASSWORD` |
| O que faz | Lê, busca, marca, move (excluir = mover para a Lixeira), cria rascunhos e **envia com confirmação** |
| Onde roda | **Em casa, junto do mailserver** (este compose, perfil `mcp`); a VM da Oracle repassa 443/80 |

## Ferramentas

| Ferramenta | O que faz |
|---|---|
| `email_account_info` | caixa conectada, pastas com totais e não lidas, envios que ainda cabem no limite |
| `email_folders` | pastas (nome, papel, total, não lidas) |
| `email_search` | busca numa pasta (texto, remetente, destinatário, assunto, datas, não lidas, com estrela); não marca como lida |
| `email_read` | lê uma mensagem (HTML vira texto) e lista os anexos, sem o conteúdo deles |
| `email_mark` | lida/não lida, com/sem estrela |
| `email_move` | move entre pastas; "excluir" é mover para `trash` |
| `email_draft_create` | cria rascunho (novo ou resposta, com citação), **não envia** |
| `email_send_draft` | **envia** um rascunho; exige `confirmed=true` e os destinatários exatos do rascunho |

## Proteções

- **Envio em dois passos.** O LibreChat não pede confirmação por ferramenta, então o servidor exige um
  rascunho, `confirmed=true` e a lista de destinatários igual à do rascunho (se o rascunho mudou, recusa). As
  instruções do servidor (`serverInstructions: true` no chat) mandam mostrar o rascunho e esperar um "sim".
- **Limites.** No máximo `MCP_MAX_RECIPIENTS` (10) destinatários por email e `MCP_MAX_SENDS_PER_HOUR` (10) /
  `MCP_MAX_SENDS_PER_DAY` (50) envios, contados em disco. A Oracle Email Delivery ainda limita a 200/dia.
- **Nada é apagado.** Mensagens só são movidas; a única remoção definitiva é a do rascunho que acabou de ser
  enviado (a cópia fica em Enviados).
- **Conteúdo de email é dado.** As instruções dizem ao modelo para nunca seguir pedidos escritos dentro de
  uma mensagem.
- **Só sai como a própria caixa.** O mailserver tem `SPOOF_PROTECTION=1`; o MCP usa o login da caixa.
- **Segredos** só no `mcp/.env` (fora do Git, permissão 600). O Caddy não recebe esse arquivo.

## Como ligar

```
Internet ──443/80──▶ VM Oracle (HAProxy, TCP + PROXY) ──Tailscale──▶ casa:12443/12080 ──▶ mcp-caddy ──▶ mcp-email ──IMAPS/SMTPS──▶ mailserver
```

1. **DNS (Hostinger):** `A  mcp-mail  →  IP público da VM da Oracle` (o mesmo do registro `mail`).
2. **Oracle:** na *Security List* da VM, acrescente as portas TCP `80,443` (origem `0.0.0.0/0`).
3. **VM:** `./mail.sh vps-deploy ubuntu@IP_DA_VM 0` (reaplica o HAProxy com 80/443 e o firewall).
4. **Casa:** `./mail.sh mcp-config` (pede a senha da caixa sem mostrar e gera a senha de autorização) e
   `./mail.sh mcp-up`.
5. **Conferir:** `curl https://mcp-mail.aggillintelligence.com.br/healthz` → `{"status":"ok",...}` e
   `curl -s -o /dev/null -w "%{http_code}" -X POST https://mcp-mail.aggillintelligence.com.br/mcp` → `401`.
6. **Chat:** item `aggillimail` no `librechat.yaml` do `aggilli-chat` (retorno aceito:
   `https://chat.aggillintelligence.com.br/api/mcp/aggillimail/oauth/callback`). Ao conectar, digite a senha de
   `./mail.sh mcp-senha`.

Emergência: `./mail.sh mcp-revogar` derruba todas as conexões; `./mail.sh mcp-down` desliga o servidor.

## Desenvolvimento

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest     # os testes "ao vivo" sobem um docker-mailserver descartável (exemplo.test); sem Docker, são pulados
```
