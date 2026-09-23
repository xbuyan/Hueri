"""
Central config. Fill in UNGM_API_URL (and UNGM_API_PARAMS if needed)
after running discover_api.py and inspecting discovered/ — see README.md.
"""

# The page a human would visit. Also used as the Playwright fallback target.
UNGM_NOTICE_PAGE = "https://www.ungm.org/Public/Notice"

# Fill this in from Phase 1 (discover_api.py). Leave as None to force the
# Playwright DOM-scrape fallback in ungm_collector.py.
UNGM_API_URL: str | None = None

# Extra query params / JSON body the API needs (page size, notice type
# filter, etc.) — inspect the captured request in discovered/ for these.
UNGM_API_PARAMS: dict = {}

# Networking
TIMEOUT_MS = 45_000          # Playwright waits, in milliseconds
REQUEST_TIMEOUT_S = 30       # requests.get/post timeout, in seconds
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_S = 2     # 2s, 4s, 8s between retries

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Where things get written
DISCOVERED_DIR = "discovered"
OUTPUT_JSONL = "notices.jsonl"
LOG_FILE = "collector.log"
