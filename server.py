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


def generate_reply(message_text, presets):
    catalog = "\n".join(
        f"{i + 1}. Q: {p['question']}\n   A: {p['answer']}"
        for i, p in enumerate(presets)
    ) or "(no presets configured)"

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
                        },
                        "general_answer": {
                            "type": ["string", "null"],
                            "description": (
                                "A short, safe reply for simple general "
                                "questions (greetings, thanks, generic "
                                "questions about laser engraving as a craft) "
                                "when no preset matches, written in a warm, "
                                "casual, personal Taglish tone. Null if the "
                                "question requires business-specific facts "
                                "(pricing, hours, turnaround time, "
                                "guarantees, availability, policies) that "
                                "aren't in the presets."
                            ),
                        },
                    },
                    "required": ["matched_index", "general_answer"],
                    "additionalProperties": False,
                },
            }
        },
        messages=[
            {
                "role": "user",
                "content": (
                    "A customer sent this message to a Facebook Page for a "
                    "small business:\n"
                    f'"{message_text}"\n\n'
                    "Here are the preset questions and answers the page "
                    "owner has prepared:\n"
                    f"{catalog}\n\n"
                    "First, check if a preset is a genuinely close match in "
                    "meaning (not just shared keywords) and set "
                    "matched_index accordingly (0 if none match).\n\n"
                    "If no preset matches, decide whether this is a simple, "
                    "general question you can safely answer yourself "
                    "(greetings, thanks, small talk, generic questions "
                    "about laser engraving as a craft or technology) and "
                    "write a short general_answer for it. Do NOT invent or "
                    "guess specific facts about this business (prices, "
                    "hours, turnaround time, guarantees, stock, policies) "
                    "that aren't given to you in the presets — leave "
                    "general_answer as null in that case so a human can "
                    "follow up instead.\n\n"
                    "When you do write a general_answer, sound like a real "
                    "Filipino small business owner personally texting back "
                    "a customer, not like a formal AI assistant. Use "
                    "natural Taglish (mixing Tagalog and English the way "
                    "people actually chat) when it fits the customer's own "
                    "message. Keep it short, warm, and casual — avoid "
                    "stiff or corporate phrases like 'I'd be happy to "
                    "assist you' or 'As an AI'. Keep general_answer to 1-2 "
                    "short sentences, like a real chat reply, never a long "
                    "paragraph."
                ),
            }
        ],
    )

    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)

    index = result.get("matched_index", 0)
    if 1 <= index <= len(presets):
        return presets[index - 1]["answer"]

    return result.get("general_answer")


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
            reply = generate_reply(message["text"], presets) or FALLBACK_MESSAGE
            send_message(sender_id, reply)

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
