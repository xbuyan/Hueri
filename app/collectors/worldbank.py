import logging
import time
from typing import List, Dict, Any
import requests
from app.config import settings

logger = logging.getLogger("collectors.worldbank")

class WorldBankCollectorError(Exception):
    pass

class WorldBankCollector:
    API_URL = "https://search.worldbank.org/api/v2/procnotices"

    def fetch_recent_notices(self) -> List[Dict[str, Any]]:
        params = {
            "format": "json",
            "rows": "15",
            "os": "0",
            "srt": "boarddate",
            "order": "desc",
            "countrycode_exact": "KE",
        }
        
        last_exc = None
        for attempt in range(1, settings.COLLECTOR_MAX_RETRIES + 1):
            try:
                res = requests.get(self.API_URL, params=params, timeout=15)
                res.raise_for_status()
                data = res.json()
                return self._parse_json(data)
            except Exception as exc:
                last_exc = exc
                logger.warning("WorldBank attempt %d failed: %s", attempt, exc)
                time.sleep(2 ** (attempt - 1))

        raise WorldBankCollectorError(f"WorldBank API collection failed: {last_exc}")

    def _parse_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        notices = []
        procnotices = data.get("procnotices", {})
        
        for key, item in procnotices.items():
            if not isinstance(item, dict):
                continue
            notices.append({
                "external_id": f"WB-{item.get('id', key)}",
                "source": "World Bank STEP",
                "title": item.get("project_name", "") + " - " + item.get("notice_title", ""),
                "buyer": item.get("owner", "World Bank Group"),
                "url": item.get("url", "https://projects.worldbank.org"),
                "deadline_str": item.get("submission_date", "N/A"),
                "raw_summary": item.get("notice_type", "") + ": " + item.get("notice_title", ""),
            })
        return notices

worldbank_collector = WorldBankCollector()
