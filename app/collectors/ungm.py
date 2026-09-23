import logging
import time
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class UNGMCollectorError(Exception):
    pass

class UNGMCollector:
    def __init__(self):
        self.url = "https://www.ungm.org/Public/Notice"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }

    def fetch_recent_notices(self) -> List[Dict[str, Any]]:
        max_retries = 3
        backoff_factor = 2

        for attempt in range(max_retries):
            try:
                response = requests.get(self.url, headers=self.headers, timeout=15)
                response.raise_for_status()
                return self._parse_html(response.text)
            except Exception as e:
                logger.warning("UNGM fetch attempt %d failed: %s", attempt + 1, e)
                if attempt == max_retries - 1:
                    raise UNGMCollectorError(f"Failed to fetch UNGM notices after {max_retries} attempts: {e}")
                time.sleep(backoff_factor ** attempt)

        return []

    def _parse_html(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        notices = []
        rows = soup.select("table.table tbody tr")

        for idx, row in enumerate(rows[:10]):
            cols = row.find_all("td")
            if len(cols) >= 3:
                title_link = cols[0].find("a")
                title = title_link.get_text(strip=True) if title_link else cols[0].get_text(strip=True)
                
                href = title_link.get("href") if title_link else ""
                notice_url = f"https://www.ungm.org{href}" if href.startswith("/") else (href or "https://www.ungm.org/Public/Notice")
                external_id = href.split("/")[-1] if href else f"UNGM-{idx + 1000}"
                
                buyer = cols[1].get_text(strip=True) if len(cols) > 1 else "UN Agency"
                deadline = cols[3].get_text(strip=True) if len(cols) >= 4 else "Open"

                notices.append({
                    "external_id": external_id,
                    "source": "UNGM",
                    "title": title,
                    "buyer": buyer,
                    "url": notice_url,
                    "deadline_str": deadline,
                })

        return notices

ungm_collector = UNGMCollector()
