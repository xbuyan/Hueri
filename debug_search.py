import requests

SEARCH_URL = "https://www.ungm.org/Public/Notice/Search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Content-Type": "application/json",
    "Referer": "https://www.ungm.org/Public/Notice",
}

# Attempt 1: full payload (same as before, just to read the error body)
payload_full = {
    "PageIndex": 0, "PageSize": 25, "Title": "", "Description": "",
    "Reference": "", "PublishedFrom": "", "PublishedTo": "",
    "DeadlineFrom": "", "DeadlineTo": "", "Countries": [],
    "Agencies": [], "UNSPSCs": [], "NoticeTypes": [],
    "SortField": "DatePublished", "SortAscending": False,
    "isPicker": False, "NoticeTASStatus": [], "IsSustainable": False,
    "NoticeDisplayType": None, "NoticeSearchTotalLabelId": "noticeSearchTotal",
    "TypeOfCompetitions": [],
}

r = requests.post(SEARCH_URL, json=payload_full, headers=HEADERS, timeout=30)
print("=== Attempt 1 (full payload) ===")
print("Status:", r.status_code)
print("Body:", r.text[:1500])   # <-- the server usually explains itself here

# Attempt 2: stripped-down payload (removes suspicious UI fields)
payload_min = {
    "PageIndex": 0, "PageSize": 25,
    "SortField": "DatePublished", "SortAscending": False,
}
r = requests.post(SEARCH_URL, json=payload_min, headers=HEADERS, timeout=30)
print("\n=== Attempt 2 (minimal payload) ===")
print("Status:", r.status_code)
print("Body:", r.text[:1500])
