import os
import tempfile

# Set before app import. The application reads INSOLIX_DATA_DIR at import time.
os.environ["INSOLIX_DATA_DIR"] = tempfile.mkdtemp(prefix="insolix-pytest-")
os.environ["INSOLIX_AGENTS_ENABLED"] = "0"
os.environ["INSOLIX_INGEST_TOKEN"] = "test-ingest-token"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
