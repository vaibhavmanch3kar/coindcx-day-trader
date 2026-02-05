import requests
import json
import time
import hmac
import hashlib
import os

API_KEY = os.environ.get("COINDCX_API_KEY", "")
SECRET = os.environ.get("COINDCX_API_SECRET", "")
BASE_URL = os.environ.get("COINDCX_BASE_URL", "https://api.coindcx.com")

def sign(body):
    s = json.dumps(body, separators=(',', ':'))
    return hmac.new(SECRET.encode('utf-8'), s.encode('utf-8'), hashlib.sha256).hexdigest()

def main():
    if not API_KEY or not SECRET:
        print("Missing COINDCX_API_KEY/COINDCX_API_SECRET")
        return
    body = {"timestamp": int(time.time() * 1000)}
    sig = sign(body)
    s = json.dumps(body, separators=(',', ':'))
    headers = {
        'Content-Type': 'application/json',
        'X-AUTH-APIKEY': API_KEY,
        'X-AUTH-SIGNATURE': sig
    }
    url = BASE_URL + "/exchange/v1/users/info"
    r = requests.post(url, data=s, headers=headers)
    print(f"HTTP {r.status_code}")
    try:
        print(r.text)
    except Exception:
        pass

if __name__ == "__main__":
    main()
