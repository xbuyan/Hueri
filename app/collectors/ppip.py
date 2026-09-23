import logging
import time
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup
from app.config import settings

logger = logging.getLogger("collectors.ppip")

class PPIPCollectorError(Exception):
    pass

class PPIPCollector:
    BASE_URL = "https://tenders.go.ke/tenders"

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        }

    def fetch_recent_notices(self) -> List[Dict[str, Any]]:
        last_exc = None
        for attempt in range(1, settings.COLLECTOR_MAX_RETRIES + 1):
            try:
                response = requests.get(self.BASE_URL, headers=self.headers, timeout=20)
                response.raise_for_status()
                return self._parse_html(response.text)
            except Exception as exc:
                last_exc = exc
                logger.warning("PPIP attempt %d failed: %s", attempt, exc)
                time.sleep(2 ** (attempt - 1))
        raise PPIPCollectorError(f"PPIP collection failed: {last_exc}")

    def _parse_html(self, html_content: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html_content, "html.parser")
        notices = []
        rows = soup.find_all("tr", class_="tender-row") or soup.find_all("tr")[1:]
        
        for idx, row in enumerate(rows):
            cols = row.find_all("td")
            if len(cols) >= 4:
                title_elem = cols[0].find("a")
                title = title_elem.text.strip() if title_elem else cols[0].text.strip()
                tender_url = title_elem["href"] if title_elem and "href" in title_elem.attrs else self.BASE_URL
                buyer = cols[1].text.strip()
                deadline = cols[3].text.strip()
                
                notices.append({
                    "external_id": f"PPIP-{idx + 1000}",
                    "source": "PPIP (Kenya)",
                    "title": title,
                    "buyer": buyer,
                    "url": tender_url if tender_url.startswith("http") else f"https://tenders.go.ke{tender_url}",
                    "deadline_str": deadline,
                    "raw_summary": f"Public tender issued by {buyer}: {title}",
                })
        return notices

ppip_collector = PPIPCollector()
