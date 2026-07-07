import csv
import hashlib
import hmac
import json
import os

import anthropic
import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

load_dotenv()

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
PAGE_ACCESS_TOKEN = os.environ["PAGE_ACCESS_TOKEN"]
APP_SECRET = os.environ.get("APP_SECRET")
PRESETS_PATH = os.environ.get("PRESETS_PATH", "presets.csv")
FALLBACK_MESSAGE = os.environ.get(
    "FALLBACK_MESSAGE",
    "Thanks for reaching out! We'll get back to you soon.",
)

app = Flask(__name__)
claude = anthropic.Anthropic()


def load_presets():
    with open(PRESETS_PATH, newline="", encoding="utf-8") as f:
        return [
            row
            for row in csv.DictReader(f)
            if row.get("question") and row.get("answer")
        ]


def verify_signature(payload, signature_header):
    if not APP_SECRET or not signature_header:
        return True
    expected = "sha256=" + hmac.new(
        APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def match_preset(message_text, presets):
    if not presets:
        return None

    catalog = "\n".join(
        f"{i + 1}. Q: {p['question']}\n   A: {p['answer']}"
        for i, p in enumerate(presets)
    )

    response = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "matched_index": {
                            "type": "integer",
                            "description": (
                                "1-based index of the best-matching preset, "
                                "or 0 if none reasonably match"
                            ),
                        }
                    },
                    "required": ["matched_index"],
                    "additionalProperties": False,
                },
            }
        },
        messages=[
            {
                "role": "user",
                "content": (
                    "A customer sent this message to a Facebook Page:\n"
                    f'"{message_text}"\n\n'
                    "Here are the preset questions and answers the page owner "
                    "has prepared:\n"
                    f"{catalog}\n\n"
                    "Which preset best answers the customer's message? Only "
                    "match if it's a genuinely close fit in meaning, not just "
                    "shared keywords."
                ),
            }
        ],
    )

    text = next(b.text for b in response.content if b.type == "text")
    index = json.loads(text).get("matched_index", 0)
    if not (1 <= index <= len(presets)):
        return None
    return presets[index - 1]["answer"]


def send_message(recipient_id, text):
    requests.post(
        "https://graph.facebook.com/v21.0/me/messages",
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        timeout=10,
    )


@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    abort(403)


@app.route("/webhook", methods=["POST"])
def receive():
    if not verify_signature(
        request.get_data(), request.headers.get("X-Hub-Signature-256")
    ):
        abort(403)

    data = request.get_json(silent=True) or {}
    presets = load_presets()

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message")
            if not message or message.get("is_echo") or "text" not in message:
                continue
            sender_id = event["sender"]["id"]
            reply = match_preset(message["text"], presets) or FALLBACK_MESSAGE
            send_message(sender_id, reply)

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
