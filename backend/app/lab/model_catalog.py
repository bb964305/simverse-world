"""Fixed provider model catalog shared by Lab services without app settings."""

FLASH_MODEL = "deepseek-v4-flash"
PRO_MODEL = "deepseek-v4-pro"
ALLOWED_MODELS = frozenset({FLASH_MODEL, PRO_MODEL})
RESOURCE_PROFILES = {
    "low": {"cpu_cores": 2, "memory_mb": 2048},
    "high": {"cpu_cores": 4, "memory_mb": 4096},
}
MODEL_GATEWAY_ISSUER = "simverse-lab"
MODEL_GATEWAY_AUDIENCE = "lab-model-gateway"
