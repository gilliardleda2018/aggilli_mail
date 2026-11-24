# Próximos passos: receber emails via Oracle Cloud (grátis)

A conexão de casa está atrás de CGNAT e o roteador bloqueia IPv6 de entrada, então uma VM gratuita
da Oracle recebe as conexões e repassa para cá pelo Tailscale. Os emails continuam **saindo direto de casa**
(a Oracle bloqueia porta 25 de saída, mas aqui ela não é usada).

```
Internet ──25/465/587/993──▶ VM Oracle (HAProxy) ──Tailscale──▶ esta máquina (servidor de email)
esta máquina ──25──▶ Gmail, Outlook… (como hoje)
```

## 1. Criar a conta (você)

1. Acesse **cloud.oracle.com/free** e clique em *Start for free*.
2. **Home Region: Brazil East (São Paulo)**. Não dá para trocar depois.
3. O cartão é só verificação (pode aparecer uma pré-autorização de ~US$ 1 que é estornada).
   Fique no plano *Free Tier*; não precisa fazer upgrade.

## 2. Criar a VM (você)

Menu ☰ → **Compute → Instances → Create instance**:

| Campo | Valor |
|---|---|
| Name | `mail-vps` |
| Image | **Canonical Ubuntu 24.04** (Change image) |
| Shape | **VM.Standard.E2.1.Micro** (AMD, marcado *Always Free-eligible*) |
| Networking | *Create new virtual cloud network* + *public subnet*, **Assign a public IPv4 address: sim** |
| Add SSH keys | *Paste public keys* e cole a chave abaixo |

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMwuzdixhyPXdxMi966ZX7WwtGVvEmPPPv7g+i+6KpeO mail-vps
```

Clique em *Create* e anote o **Public IP address** quando ficar *Running*.

## 3. Liberar as portas na Oracle (você)

Na página da instância: **Subnet** (link) → **Security Lists** → *Default Security List* →
**Add Ingress Rules**:

| Source CIDR | IP Protocol | Destination Port Range |
|---|---|---|
| `0.0.0.0/0` | TCP | `25,465,587,993` |

## 4. Configurar a VM (eu faço, ou você roda)

```bash
./mail.sh vps-deploy ubuntu@IP_PUBLICO_DA_VM 0
```

O `0` é o modo "só entrada". No meio aparece um link do Tailscale: abra e autorize a VM.

## 5. DNS na Hostinger (você)

`./mail.sh dns IP_PUBLICO_DA_VM` mostra os valores. Na prática, muda só:

| Tipo | Nome | Valor novo |
|---|---|---|
| A | `mail` | IP público da VM (substitui o IP antigo) |
| TXT | `mail` | `v=spf1 mx ip4:<HOME_PUBLIC_IP> -all` (o SPF passa a incluir o IP de casa) |

## 6. Testar

```bash
./mail.sh vps-test
```

E mandar um email de fora (Gmail, Hostinger) para `contato@mail.aggillintelligence.com.br`.

## Limitações (sem custo, mas vale saber)

- **Saída por IP residencial compartilhado (CGNAT)**: sem PTR próprio; Gmail/Outlook podem mandar para
  spam. Se o IP de casa mudar, atualize o SPF.
- A Oracle pode recuperar VMs Always Free **muito ociosas** em contas Free Tier; se sumir, é só recriar
  e rodar o passo 4 de novo.

## Depois de pronto

- **Reiniciar sozinho após reboot**: `crontab -e` e adicionar
  `@reboot cd /CAMINHO/DESTA/PASTA && ./mail.sh up`
- **Certificado TLS** vence em **21/02/2027**: `./mail.sh certs-dns` em janeiro.
- **Backup** da pasta `data/` e de `vps/id_ed25519` (chave de acesso à VM).
