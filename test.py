import requests
print(requests.get("https://lrclib.net", timeout=10).status_code)