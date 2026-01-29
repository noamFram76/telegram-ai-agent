from flask import Flask, request
import requests
import os

app = Flask(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if "message" in data and "photo" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        file_id = data["message"]["photo"][-1]["file_id"]

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
        file_path = requests.get(url).json()["result"]["file_path"]

        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        img = requests.get(file_url).content

        os.makedirs("images", exist_ok=True)
        with open(f"images/{file_id}.jpg", "wb") as f:
            f.write(img)

    return "OK"
