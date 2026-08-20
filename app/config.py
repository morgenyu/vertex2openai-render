import os


def _get_int_env(name: str, default: int, minimum: int = 0) -> int:
    """Read an integer environment variable with a safe lower bound."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return max(minimum, int(raw_value))
    except (TypeError, ValueError):
        print(f"WARNING: Invalid integer value for {name}; using default {default}.")
        return default


def _get_status_codes_env(name: str, default: str) -> set[int]:
    """Read a comma-separated set of HTTP status codes."""
    raw_value = os.environ.get(name, default)
    status_codes = set()
    for item in raw_value.split(','):
        item = item.strip()
        if not item:
            continue
        try:
            status_codes.add(int(item))
        except ValueError:
            print(f"WARNING: Ignoring invalid HTTP status code '{item}' in {name}.")
    return status_codes

# Default password if not set in environment
DEFAULT_PASSWORD = "123456"

# Get password from environment variable or use default
API_KEY = os.environ.get("API_KEY", DEFAULT_PASSWORD)

# HuggingFace Authentication Settings
HUGGINGFACE = os.environ.get("HUGGINGFACE", "false").lower() == "true"
HUGGINGFACE_API_KEY = os.environ.get("HUGGINGFACE_API_KEY", "") # Default to empty string, auth logic will verify if HF_MODE is true and this key is needed

# Directory for service account credential files
CREDENTIALS_DIR = os.environ.get("CREDENTIALS_DIR", "/app/credentials")

# JSON string for service account credentials (can be one or multiple comma-separated)
GOOGLE_CREDENTIALS_JSON_STR = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# API Key for Vertex Express Mode
raw_vertex_keys = os.environ.get("VERTEX_EXPRESS_API_KEY")
if raw_vertex_keys:
    VERTEX_EXPRESS_API_KEY_VAL = [key.strip() for key in raw_vertex_keys.split(',') if key.strip()]
else:
    VERTEX_EXPRESS_API_KEY_VAL = []

# Fake streaming settings for debugging/testing
FAKE_STREAMING_ENABLED = os.environ.get("FAKE_STREAMING", "false").lower() == "true"
FAKE_STREAMING_INTERVAL_SECONDS = float(os.environ.get("FAKE_STREAMING_INTERVAL", "1.0"))

# URL for the remote JSON file containing model lists
MODELS_CONFIG_URL = os.environ.get("MODELS_CONFIG_URL", "https://raw.githubusercontent.com/gzzhongqi/vertex2openai/refs/heads/main/vertexModels.json")

# Constant for the Vertex reasoning tag
VERTEX_REASONING_TAG = "vertex_think_tag"

# Round-robin credential selection strategy
ROUNDROBIN = os.environ.get("ROUNDROBIN", "false").lower() == "true"

# Safety score display setting
SAFETY_SCORE = os.environ.get("SAFETY_SCORE", "false").lower() == "true"
# Validation logic moved to app/auth.py

# Proxy settings
PROXY_URL = os.environ.get("PROXY_URL")
SSL_CERT_FILE = os.environ.get("SSL_CERT_FILE")

# Retry settings for transient upstream failures. RETRY_MAX_ATTEMPTS includes
# the initial request, so a value of 20 means at most 20 total attempts.
RETRY_ENABLED = os.environ.get("RETRY_ENABLED", "true").lower() == "true"
RETRY_MAX_ATTEMPTS = _get_int_env("RETRY_MAX_ATTEMPTS", 20, minimum=1)
RETRY_INTERVAL_MS = _get_int_env("RETRY_INTERVAL_MS", 500, minimum=0)
RETRY_STATUS_CODES = _get_status_codes_env(
    "RETRY_STATUS_CODES",
    "408,409,425,429,500,502,503,504",
)
