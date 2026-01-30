from flask import Flask, request
import requests
import os
import json
from google.cloud import vision

app = Flask(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

def get_vision_client():
    creds = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    return vision.ImageAnnotatorClient.from_service_account_info(creds)

def send_message(chat_id: int, text: str):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text[:3500]},
        timeout=20
    )

@app.route("/", methods=["GET"])
def home():
    return "OK - telegram-ai-agent is running", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    print("UPDATE:", data)

    msg = data.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")

    # אם אין chat_id, אין לאן לענות
    if not chat_id:
        print("No chat_id in update")
        return "OK", 200

    # 1) בדיקת דופק: נענה תמיד
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "קיבלתי את ההודעה ✅ מתחיל עיבוד..."},
            timeout=20
        )
        print("sendMessage status:", r.status_code, r.text[:200])
    except Exception as e:
        print("sendMessage exception:", str(e))
        return "OK", 200

    # 2) עכשיו נבדוק אם זו תמונה
    if "photo" not in msg:
        print("No photo in message. Keys:", list(msg.keys()))
        return "OK", 200

    # אם כן – המשך הקוד שלך להורדת קובץ + OCR...
    # (אפשר להשאיר את ההמשך כפי שהוא)
    return "OK", 200
