import json
import requests
from bs4 import BeautifulSoup

# Target URL for the background search API
SEARCH_URL = "https://ungm.org"

# Headers mimicking a standard web browser to bypass basic bot-detection
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://ungm.org"
}

# The corrected payload matching the current UNGM system
PAYLOAD = {
    "PageIndex": 0,
    "PageSize": 25,
    "Title": "",
    "Description": "",
    "Reference": "",
    "PublishedFrom": None,
    "PublishedTo": None,
    "DeadlineFrom": None,
    "DeadlineTo": None,
    "Countries": [],
    "Agencies": [],
    "UNSPSCs": [],
    "NoticeTypes": [],
    "TypeOfCompetitions": [],
    "NoticeTASStatus": [],
    "SortField": "DatePublished",
    "SortAscending": False,
    "isPicker": False,
    "IsSustainable": False,
    "NoticeDisplayType": 1, # Specifies table view layout
    "NoticeSearchTotalLabelId": "noticeSearchTotal"
}

def search_notices():
    print("Searching UNGM notices...")
    try:
        # Send the POST request with the JSON payload
        response = requests.post(SEARCH_URL, json=PAYLOAD, headers=HEADERS, timeout=30)
        
        # Check for HTTP errors (like 400, 403, 500)
        response.raise_for_status()
        
        # The endpoint returns a JSON object containing an 'html' string key
        data = response.json()
        return data.get("html", "")
        
    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred: {http_err}")
        if response.text:
            print(f"Server response snippet: {response.text[:500]}")
        return None
    except Exception as err:
        print(f"An error occurred: {err}")
        return None

def parse_notices(html_content):
    if not html_content:
        return []
        
    soup = BeautifulSoup(html_content, "html.parser")
    # UNGM dynamic table rows typically sit inside 'tableRow' or 'row' divs/tr elements
    # and contain a 'data-noticeid' attribute
    rows = soup.find_all(attrs={"data-noticeid": True})
    
    parsed_notices = []
    
    for row in rows:
        notice_id = row["data-noticeid"]
        
        # Extract individual columns (cells)
        # Class designations usually look like 'tableCell' or plain 'div/span' inside the row
        cells = row.find_all(class_="tableCell") or row.find_all(["div", "td"])
        
        if len(cells) >= 4:
            # Safely grab textual components based on layout positions
            title_el = row.find(class_="noticeTitle") or cells[0]
            title = title_el.get_text(strip=True)
            
            agency = cells[1].get_text(strip=True)
            description = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            deadline = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            
            # Formulate the definitive link to the opportunity
            notice_url = f"https://ungm.org/{notice_id}"
            
            parsed_notices.append({
                "id": notice_id,
                "title": title,
                "agency": agency,
                "description": description,
                "deadline": deadline,
                "url": notice_url
            })
            
    return parsed_notices

def main():
    # Make sure you are inside your virtual environment (.venv) when running!
    raw_html = search_notices()
    
    if raw_html:
        notices = parse_notices(raw_html)
        print(f"\nSuccessfully parsed {len(notices)} notices:")
        print("-" * 50)
        
        for index, notice in enumerate(notices, 1):
            print(f"{index}. Title: {notice['title']}")
            print(f"   Agency: {notice['agency']}")
            print(f"   Deadline: {notice['deadline']}")
            print(f"   Link: {notice['url']}")
            print("-" * 50)
    else:
        print("Failed to retrieve or parse notices.")

if __name__ == "__main__":
    main()

