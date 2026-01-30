from flask import Flask, request
import requests
import os
import json
import base64
import time
import threading

_processing_lock = threading.Lock()
_recent_lock = threading.Lock()
_recent_updates = {}

from google.cloud import vision
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from datetime import datetime

app = Flask(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DOC_ID = os.environ["DOC_ID"]
GOOGLE_OAUTH_CLIENT_ID = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
GOOGLE_OAUTH_CLIENT_SECRET = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"]


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

def get_google_clients_oauth():
    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
    ]

    creds = UserCredentials(
        token=None,
        refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=scopes,
    )

    # Refresh access token using refresh token
    creds.refresh(GoogleAuthRequest())

    docs = build("docs", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    return docs, drive

def get_google_clients():
    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive.file",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    docs = build("docs", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    return docs, drive


def upload_image_to_drive(drive, img_bytes: bytes, filename: str) -> str:
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(img_bytes, mimetype="image/jpeg")
    created = drive.files().create(
        body={"name": filename},
        media_body=media,
        fields="id"
    ).execute()

    file_id = created["id"]

    # public read so Docs can fetch the image via URL
    drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    # direct link usable by Docs insertInlineImage
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def append_lesson_to_doc(date_title: str, lesson_title: str, summary_text: str, image_url: str):
    docs, _drive = get_google_clients_oauth()

    # Read doc to find endIndex and whether today's header already exists
    doc = docs.documents().get(documentId=DOC_ID).execute()
    body_content = doc.get("body", {}).get("content", [])
    end_index = body_content[-1].get("endIndex", 1) - 1

    # Grab some tail text to check if the day header already exists
    tail = ""
    for el in body_content[-30:]:
        p = el.get("paragraph")
        if not p:
            continue
        for pe in p.get("elements", []):
            t = pe.get("textRun", {}).get("content")
            if t:
                tail += t

    requests_list = []

    if date_title not in tail:
        # new day section
        requests_list += [
            {"insertPageBreak": {"location": {"index": end_index}}},
            {"insertText": {"location": {"index": end_index + 1}, "text": f"\n📅 {date_title}\n\n"}},
        ]
        end_index += 1 + len(f"\n📅 {date_title}\n\n")

    # lesson title
    requests_list.append({
        "insertText": {"location": {"index": end_index}, "text": f"✏️ {lesson_title}\n"}
    })
    end_index += len(f"✏️ {lesson_title}\n")

    # image
    requests_list.append({
        "insertInlineImage": {
            "location": {"index": end_index},
            "uri": image_url,
            "objectSize": {
                "height": {"magnitude": 300, "unit": "PT"},
                "width": {"magnitude": 450, "unit": "PT"},
            },
        }
    })
    end_index += 1

    # summary text
    block = f"\n\n{summary_text}\n\n──────────────\n\n"
    requests_list.append({
        "insertText": {"location": {"index": end_index}, "text": block}
    })

    docs.documents().batchUpdate(
        documentId=DOC_ID,
        body={"requests": requests_list}
    ).execute()

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
    # בלם לופ: אם כבר מעבדים בקשה אחרת, נחזיר 200 כדי שטלגרם לא ימשיך להפציץ
    if not _processing_lock.acquire(blocking=False):
        return "BUSY", 200

    try:
        data = request.json or {}
        msg = data.get("message") or data.get("edited_message") or {}
        chat_id = (msg.get("chat") or {}).get("id")

        if not chat_id:
            return "OK", 200

        # מניעת כפילויות (idempotency) – אם אותה הודעה מגיעה שוב, לא נעבד שוב
        update_id = data.get("update_id")
        message_id = msg.get("message_id")
        dedupe_key = f"{update_id}:{chat_id}:{message_id}"

        now_ts = time.time()
        with _recent_lock:
            # ניקוי פריטים ישנים
            for k, t0 in list(_recent_updates.items()):
                if now_ts - t0 > 120:  # 2 דקות
                    _recent_updates.pop(k, None)

            if dedupe_key in _recent_updates:
                return "OK", 200

            _recent_updates[dedupe_key] = now_ts

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

        # ----- עיבוד OCR + סיכום -----
        img_bytes = download_telegram_file(file_id)

        ocr_text = ocr_image_bytes(img_bytes)
        if not ocr_text:
            send_message(chat_id, "לא הצלחתי לחלץ טקסט מהתמונה 😕 נסה צילום חד/ישר מול הלוח.")
            return "OK", 200

        send_message(chat_id, "סיימתי OCR ✅ עכשיו מסכם בעזרת AI...")
        summary = summarize_for_students(ocr_text, img_bytes)
        send_message(chat_id, summary)

        # ----- שמירה למסמך: try נפרד כדי שלא יפיל את כל הזרימה -----
        try:
            docs, drive = get_google_clients_oauth()
            image_url = upload_image_to_drive(
                drive,
                img_bytes,
                f"board_{int(datetime.now().timestamp())}.jpg"
            )

            date_title = datetime.now().strftime("%A %d/%m/%Y")

            append_lesson_to_doc(
                date_title=date_title,
                lesson_title="לוח כיתה",
                summary_text=summary,
                image_url=image_url
            )
        except Exception as e:
            # לא מפילים את כל העיבוד – רק לוג
            print("Google Docs/Drive failed:", str(e))
            send_message(chat_id, "הסיכום נשלח ✅ אבל שמירה למסמך נכשלה (בודק לוגים).")

        return "OK", 200

    except Exception as e:
        print("Processing exception:", str(e))
        try:
            send_message(chat_id, f"שגיאה בעיבוד 😕\n{type(e).__name__}: {e}")
        except Exception:
            pass
        return "OK", 200

    finally:
        _processing_lock.release()
