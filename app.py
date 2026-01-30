from flask import Flask, request
import requests
import os
import json
import base64
from google.cloud import vision
from openai import OpenAI

app = Flask(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# GOOGLE_CREDENTIALS_JSON = כל ה-JSON של Google service account
def get_vision_client():
    creds = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    return vision.ImageAnnotatorClient.from_service_account_info(creds)

def send_message(chat_id: int, text: str):
    text = (text or "").strip()
    if not text:
        text = "לא הצלחתי לחלץ טקסט 😕"
    if len(text) > 3500:
        text = text[:3500] + "\n\n...(קיצרתי כי זה ארוך)"

    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=25,
    )
    print("sendMessage:", r.status_code, r.text[:200])

def download_telegram_file(file_id: str) -> bytes:
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=25,
    )
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    return requests.get(file_url, timeout=40).content

def ocr_image_bytes(img_bytes: bytes) -> str:
    client = get_vision_client()
    image = vision.Image(content=img_bytes)
    resp = client.text_detection(image=image)

    text = ""
    if resp.text_annotations:
        text = resp.text_annotations[0].description.strip()

    if getattr(resp, "error", None) and resp.error.message:
        print("Vision error:", resp.error.message)

    return text

import base64

def summarize_for_students(ocr_text: str, img_bytes: bytes) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)

    img_b64 = base64.b64encode(img_bytes).decode("utf-8")

    prompt = """
אתה עוזר הוראה שמסכם לוח מתמטיקה בעברית.
אתה מקבל גם תמונה של הלוח וגם טקסט OCR מבולגן.
המטרה מספר 1: לחלץ ולהדגיש את שיעורי הבית.

חוקים קריטיים:
- כל אזכור של עמודים/חוברת/טווח (למשל 66–69) = שיעורי בית.
- אם יש טבלה עם מספרים ובחלק מהם יש ✓/X ובחלק אין — 
  המספרים שלא סומנו הם תרגול להשלים בבית → הם חלק משיעורי הבית.
- מותר להסיק "להשלים את הטבלה" אם זה משתמע מהמבנה, גם אם לא כתוב במילים.
- אל תמציא מספרים שלא מופיעים בתמונה.

החזר בפורמט הבא בלבד:

שיעורי בית (הדבר הכי חשוב):
נושא השיעור:
הכלל המרכזי:
כפולות/רשימות מהלוח:
בדיקות בטבלה (אם יש):

השתמש בתמונה כדי להבין ✓/X ומה לא סומן, וב-OCR כהשלמה.
"""



    resp = client.responses.create(
        model="gpt-5.2",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_text", "text": f"OCR (עזר בלבד):\n{ocr_text}"},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{img_b64}"},
                ],
            }
        ],
    )

    return (resp.output_text or "").strip()

@app.route("/", methods=["GET"])
def home():
    return "OK - telegram-ai-agent is running", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    msg = data.get("message") or data.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")

    if not chat_id:
        return "OK", 200

    # Ack מהיר
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "קיבלתי ✅ מתחיל OCR..."},
            timeout=20,
        )
    except Exception as e:
        print("Ack exception:", str(e))

    # Photo (compressed) OR Document (sent as file)
    file_id = None

    if "photo" in msg and msg["photo"]:
        file_id = msg["photo"][-1]["file_id"]
    elif "document" in msg and msg["document"]:
        mime = (msg["document"].get("mime_type") or "").lower()
        if mime.startswith("image/"):
            file_id = msg["document"]["file_id"]

    if not file_id:
        send_message(chat_id, "לא זיהיתי תמונה. שלח תמונה רגילה או כקובץ (Document) מסוג image.")
        return "OK", 200

    try:
        img_bytes = download_telegram_file(file_id)
        ocr_text = ocr_image_bytes(img_bytes)

        if not ocr_text:
            send_message(chat_id, "לא הצלחתי לחלץ טקסט מהתמונה 😕 נסה צילום חד/ישר מול הלוח.")
            return "OK", 200

        send_message(chat_id, "סיימתי OCR ✅ עכשיו מסכם בעזרת AI...")
        summary = summarize_for_students(ocr_text, img_bytes)
        send_message(chat_id, summary)

    except Exception as e:
        print("Processing exception:", str(e))
        send_message(chat_id, f"שגיאה בעיבוד 😕\n{type(e).__name__}: {e}")

    return "OK", 200
