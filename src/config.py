import os
import requests
import logging

logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")

DATABASE_URL = os.environ["DATABASE_URL"]

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# Vault Config
VAULT_URL = os.environ.get("VAULT_URL", "http://mordomo-vault:8200")
VAULT_TOKEN = os.environ.get("VAULT_SERVICE_TOKEN")

def get_master_key():
    """Retrieve encryption key from Vault or environment."""
    # 1. Check Vault first (Preferred)
    if VAULT_TOKEN:
        try:
            resp = requests.get(
                f"{VAULT_URL}/v1/secret/data/mordomo/people/security",
                headers={"X-Vault-Token": VAULT_TOKEN},
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["data"]["data"]["master_key"]
        except Exception as e:
            logger.warning(f"Could not reach Vault for master key: {e}")

    # 2. Fallback to Environment (Seeds/Dev)
    key = os.environ.get("PEOPLE_MASTER_KEY")
    if not key:
        raise RuntimeError("PEOPLE_MASTER_KEY not found in Vault or Environment")
    return key

# Chave mestre para criptografia AES-256-GCM dos dados sensíveis.
# Deve ter exatamente 32 bytes (256 bits) em hex (64 chars).
PEOPLE_MASTER_KEY_HEX = get_master_key()

# Cache TTL (segundos)
RESOLVE_CACHE_TTL = int(os.environ.get("RESOLVE_CACHE_TTL", "300"))   # 5 min
PERMISSIONS_CACHE_TTL = int(os.environ.get("PERMISSIONS_CACHE_TTL", "60"))  # 1 min

# HTTP Server Config
HTTP_PORT = int(os.environ.get("PEOPLE_HTTP_PORT", "8000"))
HTTP_HOST = os.environ.get("PEOPLE_HTTP_HOST", "0.0.0.0")
