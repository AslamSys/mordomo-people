# 👤 Mordomo People

## 🔗 Navegação

**[🏠 AslamSys](https://github.com/AslamSys)** → **[📚 _system](https://github.com/AslamSys/_system)** → **mordomo-people**

### Containers Relacionados (mordomo)
- [mordomo-brain](https://github.com/AslamSys/mordomo-brain)
- [mordomo-speaker-verification](https://github.com/AslamSys/mordomo-speaker-verification)
- [mordomo-financas-pix](https://github.com/AslamSys/mordomo-financas-pix)

---

**Container:** `mordomo-people`  
**Stack:** Python + PostgreSQL + AES-256  
**Hardware:** Orange Pi 5 16GB (junto com mordomo central)

---

## 📋 Propósito

Identity store central do Mordomo. Guarda perfis de pessoas, permissões, contatos e credenciais externas — tudo criptografado em repouso. É a fonte de verdade quando qualquer módulo precisa saber quem é alguém ou como alcançá-la.

---

## 🎯 Responsabilidades

- ✅ **Perfis** — nome, apelidos reconhecidos pelo Mordomo, voz (referência ao `mordomo-speaker-verification`), rosto (referência ao `seguranca-face-recognition`)
- ✅ **Permissões** — o que cada pessoa pode pedir ao Mordomo (ex: só o dono pode autorizar PIX alto)
- ✅ **Contatos** — livro de endereços com chaves PIX, telefone, email
- ✅ **Lookup por nome** — resolve "João" → `{ pix_key: "+5511..." }`

> **Credenciais de sistema** (API keys, tokens de serviços externos) pertencem ao módulo que as usa — gerenciadas via variáveis de ambiente Docker, não aqui.

---

## 🔐 Segurança

- Dados sensíveis criptografados com **AES-256-GCM** em repouso
- Master key derivada de variável de ambiente (`PEOPLE_MASTER_KEY`) — nunca persiste no banco
- Acesso apenas via NATS (não expõe HTTP externamente)
- Permissões verificadas antes de retornar credenciais

```
┌─────────────────────────────┐
│  PostgreSQL (mordomo-postgres)│
│  ┌──────────┐ ┌───────────┐ │
│  │ pessoas  │ │contatos   │ │
│  │ (perfis) │ │(pix keys) │ │
│  └──────────┘ └───────────┘ │
│  ┌──────────────────────────┐│
│  │ permissoes               ││
│  └──────────────────────────┘│
└─────────────────────────────┘
```

---

## 🔌 NATS Topics

### Subscribe

```javascript
// Busca pessoa por nome (para resolver "João" em pagamentos)
Topic: "mordomo.people.resolve"
Payload: { "name": "João" }
→ Response: { "id": "uuid", "pix_key": "+5511...", "email": "joao@..." }

// Busca permissões de uma pessoa
Topic: "mordomo.people.permissions.get"
Payload: { "person_id": "uuid" }
→ Response: { "can_authorize_pix": true, "max_pix_amount": 500.00, "is_owner": false }

// Cria ou atualiza pessoa
Topic: "mordomo.people.upsert"
Payload: {
  "name": "João Silva",
  "aliases": ["João", "Joãozinho"],
  "pix_key": "+5511999998888",
  "permissions": { "can_authorize_pix": true, "max_pix_amount": 500.00 }
}
```

### Publish

```javascript
Topic: "mordomo.people.resolved"
Payload: { "query": "João", "person_id": "uuid", "found": true }
```

---

## 🗄️ Schema

```sql
-- Perfis
CREATE TABLE pessoas (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  aliases TEXT[],                    -- nomes reconhecidos pelo Mordomo
  voice_profile_id TEXT,             -- referência ao mordomo-speaker-verification
  face_profile_id TEXT,              -- referência ao seguranca-face-recognition
  is_owner BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Contatos (PIX, email, telefone)
CREATE TABLE contatos (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id UUID REFERENCES pessoas(id),
  type TEXT NOT NULL,                -- 'pix_key', 'email', 'phone'
  value TEXT NOT NULL,
  label TEXT                         -- 'pessoal', 'trabalho'
);

-- Permissões
CREATE TABLE permissoes (
  person_id UUID REFERENCES pessoas(id),
  key TEXT NOT NULL,                 -- 'can_authorize_pix', 'max_pix_amount'
  value TEXT NOT NULL,
  PRIMARY KEY (person_id, key)
);
```

---

## 🚀 Docker Compose

```yaml
mordomo-people:
  build: ./mordomo-people
  environment:
    - NATS_URL=nats://mordomo-nats:4222
    - DATABASE_URL=postgresql://postgres:password@mordomo-postgres:5432/mordomo
    - PEOPLE_MASTER_KEY=${PEOPLE_MASTER_KEY}  # chave mestre para dados sensíveis em repouso
  deploy:
    resources:
      limits:
        cpus: '0.3'
        memory: 256M
```

---

## 🔗 Integrações

| Módulo | Como usa |
|---|---|
| `mordomo-brain` | Resolve nomes em comandos (`"Faz PIX pro João"`) |
| `mordomo-financas-pix` | Busca chave PIX do destinatário pelo nome |
| `mordomo-speaker-verification` | Verifica se voz pertence a pessoa com permissão |
| `seguranca-face-recognition` | Verifica identidade por rosto |
| `mordomo-action-dispatcher` | Checa permissões antes de executar ação |

---

## 🏗️ Estrutura do Repositório

```
mordomo-people/
├── src/
│   ├── main.py        # Entry point — asyncio loop, NATS connect, signal handlers
│   ├── config.py      # Configuração via variáveis de ambiente
│   ├── crypto.py      # AES-256-GCM encrypt/decrypt para dados sensíveis
│   ├── db.py          # asyncpg — queries: resolve_person, get_permissions, upsert_person
│   ├── cache.py       # Redis (db 0) — cache de lookups e permissões com TTL
│   └── handlers.py    # NATS handlers: handle_resolve, handle_permissions_get, handle_upsert
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

## ⚙️ Variáveis de Ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `NATS_URL` | — | Default: `nats://nats:4222` |
| `DATABASE_URL` | ✅ | URL do Postgres da infra |
| `REDIS_URL` | — | Default: `redis://redis:6379/0` (db 0) |
| `PEOPLE_MASTER_KEY` | ✅ | 32 bytes hex (64 chars) para AES-256-GCM |
| `RESOLVE_CACHE_TTL` | — | TTL cache de resolve em segundos (default: 300) |
| `PERMISSIONS_CACHE_TTL` | — | TTL cache de permissões em segundos (default: 60) |

Gere a master key com:
```bash
python -c "import os, binascii; print(binascii.hexlify(os.urandom(32)).decode())"
```

## 🚀 Como rodar

```bash
# Pré-requisitos: infra rodando (nats, postgres, redis)
cp .env.example .env
# Preencha DATABASE_URL e PEOPLE_MASTER_KEY no .env

docker compose up -d
```
