from flask import Flask, request
import requests
import os
import json
from google.cloud import vision

app = Flask(__name__)

# Render Environment Variables:
# BOT_TOKEN = <telegram bot token>
# GOOGLE_CREDENTIALS_JSON = <full JSON content of the service account key>
BOT_TOKEN = os.environ["BOT_TOKEN"]


def get_vision_client():
    creds = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    return vision.ImageAnnotatorClient.from_service_account_info(creds)


def send_message(chat_id: int, text: str):
    text = (text or "").strip()
    if not text:
        text = "לא הצלחתי לחלץ טקסט מהתמונה 😕 נסה צילום יותר חד/ישר מול הלוח."

    # Telegram message limit ~4096 chars
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


@app.route("/", methods=["GET"])
def home():
    return "OK - telegram-ai-agent is running", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    # Support normal + edited messages
    msg = data.get("message") or data.get("edited_message") or {}
    print("UPDATE keys:", list(data.keys()))
    print("message keys:", list(msg.keys()))

    chat_id = (msg.get("chat") or {}).get("id")
    if not chat_id:
        print("No chat_id found in update")
        return "OK", 200

    # Quick ack so user knows it's working
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "קיבלתי ✅ מתחיל OCR..."},
            timeout=20,
        )
    except Exception as e:
        print("Ack sendMessage exception:", str(e))

    # Photo (compressed) OR Document (sent as file)
    file_id = None

    if "photo" in msg and msg["photo"]:
        file_id = msg["photo"][-1]["file_id"]

    elif "document" in msg and msg["document"]:
        mime = (msg["document"].get("mime_type") or "").lower()
        if mime.startswith("image/"):
            file_id = msg["document"]["file_id"]

    if not file_id:
        send_message(
            chat_id,
            "לא זיהיתי תמונה בהודעה.\n"
            "שלח תמונה רגילה מהגלריה, או 'שלח כקובץ' (Document) של תמונה.",
        )
        return "OK", 200

    try:
        img_bytes = download_telegram_file(file_id)
        text = ocr_image_bytes(img_bytes)
        send_message(chat_id, text)
    except Exception as e:
        print("Processing exception:", str(e))
        send_message(chat_id, f"קרה משהו לא צפוי בעיבוד 😕\n{type(e).__name__}: {e}")

    return "OK", 200
