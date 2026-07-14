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
    "Thanks for reaching out! For a faster response, please call or "
    "Viber us directly at 09178350100.",
)
LEAD_LABEL_ID = os.environ.get("LEAD_LABEL_ID")

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
        f"{i + 1}. Q: {p['question']}\n"
        f"   A: {p['answer']}\n"
        f"   Link: {p.get('link') or '(none)'}"
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
                        "reply": {
                            "type": ["string", "null"],
                            "description": (
                                "The final message to send the customer. "
                                "If matched_index points to a preset, this "
                                "must be a natural-sounding rephrasing of "
                                "that preset's answer — same facts, numbers, "
                                "and terms, different wording each time. If "
                                "that preset has a Link, weave it into the "
                                "reply naturally. If matched_index is 0, "
                                "this is a short safe general reply, or "
                                "null if the question needs business facts "
                                "not in the presets."
                            ),
                        },
                        "is_lead": {
                            "type": "boolean",
                            "description": (
                                "True if this message shows genuine buying "
                                "intent — asking about price to decide, "
                                "wanting to order/purchase, asking to set "
                                "an appointment/demo, or requesting specs "
                                "to make a purchase decision. False for "
                                "general curiosity, small talk, or "
                                "unrelated questions."
                            ),
                        },
                    },
                    "required": ["matched_index", "reply", "is_lead"],
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
                    "owner has prepared, each with an optional reference "
                    "link (a video or image URL; a preset may have more "
                    "than one URL separated by spaces — include whichever "
                    "one(s) are actually relevant to your reply, not "
                    "necessarily all of them):\n"
                    f"{catalog}\n\n"
                    "Step 1: Check if a preset is a genuinely close match "
                    "in meaning (not just shared keywords) and set "
                    "matched_index accordingly (0 if none match).\n\n"
                    "Step 2: Write the reply.\n"
                    "- If a preset matched: rephrase that preset's answer "
                    "in your own natural voice so it doesn't sound like a "
                    "canned script, but you MUST keep every specific fact "
                    "exactly the same — every price, number, name, date, "
                    "and specific term must be preserved exactly as "
                    "written. Do not add, remove, soften, or guess at any "
                    "fact. Only the phrasing/sentence structure should "
                    "vary. If that preset has a Link (not '(none)'): for "
                    "any URL ending in .jpg/.jpeg/.png/.gif/.webp, do NOT "
                    "put that URL in your reply text — it will be sent "
                    "separately as an actual photo, so just write your "
                    "reply naturally (e.g. mention 'sending a picture' if "
                    "it fits). For any other URL (like a video link), "
                    "include it naturally in the reply text (e.g. 'here's "
                    "a video showing it: <link>'). Never invent or guess "
                    "a link that wasn't given to you.\n"
                    "- If no preset matched: if this is a simple, general "
                    "question you can safely answer yourself (greetings, "
                    "thanks, small talk, generic questions about laser "
                    "engraving as a craft or technology), write a short "
                    "safe reply. Do NOT invent or guess specific facts "
                    "about this business (prices, hours, turnaround time, "
                    "guarantees, stock, policies) that aren't given to you "
                    "in the presets — set reply to null in that case so a "
                    "human can follow up instead.\n\n"
                    "CRITICAL RULE: you have no ability to actually "
                    "schedule, book, or confirm anything — no appointments, "
                    "demos, orders, meetings, or reservations. NEVER say or "
                    "imply that something has been booked, confirmed, or "
                    "set (e.g. never say things like 'naka-set na tayo' or "
                    "'confirmed na po'). If the customer wants to set an "
                    "appointment, book a demo, or arrange a visit, this is "
                    "NOT a safe general question — only answer it using an "
                    "actual matching preset about appointments if one "
                    "exists; otherwise set reply to null so the fallback "
                    "message (which tells them to call/Viber directly) is "
                    "used instead.\n\n"
                    "Voice: sound like a real Filipino small business "
                    "owner personally texting back a customer, not a "
                    "formal AI assistant. Use natural Taglish (mixing "
                    "Tagalog and English the way people actually chat) "
                    "when it fits the customer's own message. Keep it "
                    "warm and casual — avoid stiff or corporate phrases "
                    "like 'I'd be happy to assist you' or 'As an AI'. "
                    "Keep the reply to 1-3 short sentences, like a real "
                    "chat message, never a long paragraph.\n\n"
                    "Step 3: Set is_lead to true only if this specific "
                    "message shows genuine buying intent (see schema), "
                    "not just because a preset happened to match."
                ),
            }
        ],
    )

    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    return (
        result.get("reply"),
        result.get("is_lead", False),
        result.get("matched_index", 0),
    )


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def image_links_for(preset):
    link_field = (preset.get("link") or "").split()
    return [
        url
        for url in link_field
        if url.split("?")[0].lower().endswith(IMAGE_EXTENSIONS)
    ]


def send_message(recipient_id, text):
    requests.post(
        "https://graph.facebook.com/v21.0/me/messages",
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        timeout=10,
    )


def send_image(recipient_id, image_url):
    requests.post(
        "https://graph.facebook.com/v21.0/me/messages",
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": image_url, "is_reusable": True},
                }
            },
        },
        timeout=10,
    )


def apply_lead_label(psid):
    if not LEAD_LABEL_ID:
        return
    requests.post(
        f"https://graph.facebook.com/v21.0/{LEAD_LABEL_ID}/label",
        params={"user": psid, "access_token": PAGE_ACCESS_TOKEN},
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
            reply, is_lead, matched_index = generate_reply(
                message["text"], presets
            )
            send_message(sender_id, reply or FALLBACK_MESSAGE)
            if is_lead:
                apply_lead_label(sender_id)
            if 1 <= matched_index <= len(presets):
                for image_url in image_links_for(presets[matched_index - 1]):
                    send_image(sender_id, image_url)

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
