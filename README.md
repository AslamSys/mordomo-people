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
- ✅ **Credenciais criptografadas** — API keys, tokens de serviços externos (ex: Asaas, corretoras)
- ✅ **Lookup por nome** — resolve "João" → `{ pix_key: "+5511..." }`

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
│  │ credenciais (AES-256)    ││
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

// Busca credencial criptografada (só para módulos autorizados)
Topic: "mordomo.people.credential.get"
Payload: { "key": "asaas_api_key", "requester": "mordomo-financas-pix" }
→ Response: { "value": "decrypted_secret" }

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

-- Credenciais externas (criptografadas)
CREATE TABLE credenciais (
  key TEXT PRIMARY KEY,              -- 'asaas_api_key', 'b3_token'
  value_encrypted BYTEA NOT NULL,   -- AES-256-GCM
  nonce BYTEA NOT NULL,
  owner_module TEXT NOT NULL,        -- módulo que usa essa credencial
  updated_at TIMESTAMPTZ DEFAULT NOW()
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
    - PEOPLE_MASTER_KEY=${PEOPLE_MASTER_KEY}  # nunca hardcode
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
| `mordomo-financas-pix` | Busca chave PIX do destinatário + credencial Asaas |
| `mordomo-speaker-verification` | Verifica se voz pertence a pessoa com permissão |
| `seguranca-face-recognition` | Verifica identidade por rosto |
| `mordomo-action-dispatcher` | Checa permissões antes de executar ação |
