"""
PostgreSQL access layer — mordomo-people.
Uses asyncpg for async queries against the people schema.
"""
import asyncpg
from typing import Optional
from src.config import DATABASE_URL
from src.crypto import encrypt, decrypt


_pool: Optional[asyncpg.Pool] = None


async def ensure_schema(conn: asyncpg.Connection) -> None:
    """Creates the 'people' schema and tables if they don't exist."""
    print("  [DB] Ensuring 'people' schema and tables...")
    await conn.execute("CREATE SCHEMA IF NOT EXISTS people;")
    
    # Tables
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS people.pessoas (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL,
            password_hash TEXT,
            aliases TEXT[] DEFAULT '{}',
            voice_profile_id TEXT,
            face_profile_id TEXT,
            is_owner BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        
        CREATE TABLE IF NOT EXISTS people.contatos (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            person_id UUID REFERENCES people.pessoas(id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            value_enc TEXT NOT NULL,
            label TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        
        CREATE TABLE IF NOT EXISTS people.permissoes (
            person_id UUID REFERENCES people.pessoas(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (person_id, key)
        );
    """)

    # UNIQUE INDEX for ON CONFLICT (lower(name))
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pessoas_name_lower 
        ON people.pessoas (lower(name));
    """)
    
    # Ensure password_hash column exists (Migration support)
    await conn.execute("ALTER TABLE people.pessoas ADD COLUMN IF NOT EXISTS password_hash TEXT;")


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    
    # Ensure schema on startup
    async with _pool.acquire() as conn:
        await ensure_schema(conn)


async def close_pool() -> None:
    if _pool:
        await _pool.close()


def _pool_conn():
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool.acquire()


# ── Resolve ────────────────────────────────────────────────────────────────────

async def resolve_person(name: str) -> Optional[dict]:
    """Find a person by exact name or alias. Returns full profile with contacts."""
    async with _pool_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.id, p.name, p.aliases, p.voice_profile_id, p.face_profile_id,
                   p.is_owner
            FROM people.pessoas p
            WHERE lower(p.name) = lower($1)
               OR $1 = ANY(p.aliases)
            LIMIT 1
            """,
            name,
        )
        if not row:
            return None

        person = dict(row)

        # Fetch contacts (decrypt sensitive values)
        contacts_rows = await conn.fetch(
            "SELECT type, value_enc, label FROM people.contatos WHERE person_id = $1",
            person["id"],
        )
        contacts = []
        for c in contacts_rows:
            contacts.append({
                "type": c["type"],
                "value": decrypt(c["value_enc"]),
                "label": c["label"],
            })

        person["contacts"] = contacts
        person["id"] = str(person["id"])
        return person


async def resolve_person_by_contact(identifier: str, channel: str) -> Optional[dict]:
    """Find a person by a contact identifier (e.g. whatsapp number)."""
    async with _pool_conn() as conn:
        # 1. Find the person_id that owns this contact (decrypt and match)
        # Since contact values are encrypted, we fetch all for that channel and check
        # (Alternatively, we could use a hashed_value for indexing, but for now we'll match decrypted)
        rows = await conn.fetch(
            "SELECT person_id, value_enc FROM people.contatos WHERE type = $1",
            channel
        )
        
        target_person_id = None
        for r in rows:
            if decrypt(r["value_enc"]) == identifier:
                target_person_id = r["person_id"]
                break
        
        if not target_person_id:
            return None
            
        # 2. Re-use the existing resolve_person with the person's name (found via ID)
        name = await conn.fetchval("SELECT name FROM people.pessoas WHERE id = $1", target_person_id)
        return await resolve_person(name)


# ── Permissions ────────────────────────────────────────────────────────────────

async def get_permissions(person_id: str) -> dict:
    async with _pool_conn() as conn:
        rows = await conn.fetch(
            "SELECT key, value FROM people.permissoes WHERE person_id = $1",
            person_id,
        )
        perms: dict = {}
        for r in rows:
            val = r["value"]
            # Coerce to bool or float where applicable
            if val.lower() in ("true", "false"):
                perms[r["key"]] = val.lower() == "true"
            else:
                try:
                    perms[r["key"]] = float(val)
                except ValueError:
                    perms[r["key"]] = val
        return perms


# ── Upsert ─────────────────────────────────────────────────────────────────────

async def upsert_person(data: dict) -> str:
    """
    Create or update a person. Returns person UUID.
    data keys: name, aliases, voice_profile_id, face_profile_id,
               is_owner, contacts (list), permissions (dict)
    """
    async with _pool_conn() as conn:
        async with conn.transaction():
            # Upsert pessoa
            person_id = await conn.fetchval(
                """
                INSERT INTO people.pessoas (name, aliases, voice_profile_id, face_profile_id, is_owner)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (lower(name))
                DO UPDATE SET
                    aliases          = EXCLUDED.aliases,
                    voice_profile_id = EXCLUDED.voice_profile_id,
                    face_profile_id  = EXCLUDED.face_profile_id,
                    is_owner         = EXCLUDED.is_owner,
                    updated_at       = NOW()
                RETURNING id
                """,
                data["name"],
                data.get("aliases", []),
                data.get("voice_profile_id"),
                data.get("face_profile_id"),
                data.get("is_owner", False),
            )

            # Replace contacts
            if "contacts" in data:
                await conn.execute(
                    "DELETE FROM people.contatos WHERE person_id = $1", person_id
                )
                for c in data["contacts"]:
                    await conn.execute(
                        """
                        INSERT INTO people.contatos (person_id, type, value_enc, label)
                        VALUES ($1, $2, $3, $4)
                        """,
                        person_id,
                        c["type"],
                        encrypt(c["value"]),
                        c.get("label"),
                    )

            # Replace permissions
            if "permissions" in data:
                await conn.execute(
                    "DELETE FROM people.permissoes WHERE person_id = $1", person_id
                )
                for key, val in data["permissions"].items():
                    await conn.execute(
                        """
                        INSERT INTO people.permissoes (person_id, key, value)
                        VALUES ($1, $2, $3)
                        """,
                        person_id,
                        key,
                        str(val),
                    )

            return str(person_id)
