"""Backend environment loading shared by API and provider modules."""
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
BACKEND_ENV_FILE = BACKEND_DIR / ".env"
ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Load the backend-local or root file regardless of the process working directory.
# Existing deployment environment variables take precedence over local values.
if BACKEND_ENV_FILE.exists():
    load_dotenv(BACKEND_ENV_FILE, override=False)
if ROOT_ENV_FILE.exists():
    load_dotenv(ROOT_ENV_FILE, override=False)
