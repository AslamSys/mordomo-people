import os


NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")

DATABASE_URL = os.environ["DATABASE_URL"]

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# Chave mestre para criptografia AES-256-GCM dos dados sensíveis.
# Deve ter exatamente 32 bytes (256 bits) em hex (64 chars).
PEOPLE_MASTER_KEY_HEX = os.environ["PEOPLE_MASTER_KEY"]

# Cache TTL (segundos)
RESOLVE_CACHE_TTL = int(os.environ.get("RESOLVE_CACHE_TTL", "300"))   # 5 min
PERMISSIONS_CACHE_TTL = int(os.environ.get("PERMISSIONS_CACHE_TTL", "60"))  # 1 min
