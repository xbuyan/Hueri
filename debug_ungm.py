import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36"}

response = requests.get("https://www.ungm.org/Public/Notice",
                        headers=HEADERS, timeout=30)

print("Status code:", response.status_code)          # 200 = OK, 403 = blocked
print("HTML length:", len(response.text))            # very short = JS-rendered or blocked

html = response.text

# What kinds of notice links exist in the HTML?
for pattern in ["/Public/Notice/", "Notice/", "notice", "tender"]:
    print(f"'{pattern}' appears:", html.count(pattern), "times")

# Save the raw HTML so we can inspect it properly
with open("ungm_page.html", "w") as f:
    f.write(html)
print("\nSaved raw HTML to ungm_page.html")
