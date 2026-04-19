import os
import time
import httpx
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Essential Bootstrap (The only ones that can stay in ENV or defaults)
VAULT_URL = os.environ.get("VAULT_URL", "http://mordomo-vault:8200")
VAULT_TOKEN = os.environ.get("VAULT_SERVICE_TOKEN", "root") # Default for dev/seed

def fetch_from_vault(key: str, default: str = None, mandatory: bool = False) -> str:
    """Try to fetch a secret from Vault with retries during startup."""
    if not VAULT_URL:
        return os.environ.get(key, default)

    max_retries = 10
    for i in range(max_retries):
        try:
            # Note: We use the simpler /get interface since we have custom Vault
            resp = httpx.get(f"{VAULT_URL}/get_all", timeout=2.0)
            if resp.status_code == 200:
                data = resp.json()
                val = data.get(key)
                if val:
                    return val
                break # Connected but key missing
        except Exception:
            if i < max_retries - 1:
                logger.info(f"Waiting for Vault ({i+1}/{max_retries})...")
                time.sleep(2)
            else:
                logger.warning(f"Could not reach Vault after {max_retries} attempts.")
    
    # Fallback to ENV
    val = os.environ.get(key, default)
    if mandatory and not val:
        raise RuntimeError(f"MISSING MANDATORY CONFIG: {key} (Not in Vault or ENV)")
    return val

# --- Dynamic Configuration ---
NATS_URL = fetch_from_vault("NATS_URL", "nats://nats:4222")
DATABASE_URL = fetch_from_vault("DATABASE_URL", mandatory=True)
REDIS_URL = fetch_from_vault("REDIS_URL", "redis://redis:6379/1")
PEOPLE_MASTER_KEY_HEX = fetch_from_vault("PEOPLE_MASTER_KEY", mandatory=True)

# Cache / UI Config
RESOLVE_CACHE_TTL = int(os.environ.get("RESOLVE_CACHE_TTL", "300"))
PERMISSIONS_CACHE_TTL = int(os.environ.get("PERMISSIONS_CACHE_TTL", "60"))
HTTP_PORT = int(os.environ.get("PEOPLE_HTTP_PORT", "8000"))
HTTP_HOST = os.environ.get("PEOPLE_HTTP_HOST", "0.0.0.0")

# Vault check vars for UI
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") # We still keep this for the wizard initial check
BIFROST_API_KEY = os.environ.get("BIFROST_API_KEY")
