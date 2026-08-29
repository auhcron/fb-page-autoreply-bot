import csv
import hashlib
import hmac
import json
import os
import threading
import time

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
HUMAN_PAUSE_SECONDS = int(os.environ.get("HUMAN_PAUSE_HOURS", "2")) * 3600
BOT_METADATA_TAG = "bot_reply"
FOLLOWUP_SECONDS = int(os.environ.get("FOLLOWUP_MINUTES", "10")) * 60
FOLLOWUP_MESSAGE = os.environ.get(
    "FOLLOWUP_MESSAGE",
    "Hi po! Sana nakatulong yung sagot namin — may iba pa po ba kayong "
    "tanong, o gusto niyo na mag-order? 😊",
)

app = Flask(__name__)
claude = anthropic.Anthropic()

paused_until = {}
processed_message_ids = set()
last_activity = {}
followup_timers = {}


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
                    "Business context: this Facebook Page belongs to a "
                    "small business that SELLS laser engraving machines "
                    "(CO2 Galvo, UV Laser, Fiber Laser). It does NOT offer "
                    "engraving services — never suggest or imply that the "
                    "business will engrave something for the customer. "
                    "Watch for even a HINT that the customer wants a "
                    "service done for them (e.g. 'can you make me a "
                    "trophy', 'pa-engrave po ng pangalan ko', 'gawan niyo "
                    "ako ng...'), not just an explicit direct request — "
                    "whenever you notice this, politely remind them that "
                    "the business only sells the machines themselves, not "
                    "engraving/marking services. All the machines "
                    "this business sells are GALVO systems, which are for "
                    "engraving/marking ONLY — never claim or imply any of "
                    "them can CUT materials. Cutting requires a completely "
                    "different type of machine (a gantry system, not a "
                    "galvo), and for fiber laser specifically, efficient "
                    "cutting needs at least 1000 watts on a gantry setup — "
                    "far beyond what a galvo does. If a customer asks about "
                    "cutting, clarify that these machines only engrave/mark, "
                    "not cut, and that this business does not currently "
                    "carry cutting/gantry machines.\n\n"
                    "A customer sent this message to the Page:\n"
                    f'"{message_text}"\n\n'
                    "The customer may write in casual Filipino texting "
                    "shorthand — interpret common abbreviations naturally "
                    "(e.g. 'HM' or 'magkano' = how much, 'po'/'opo' = "
                    "polite particles, 'pwede' = can/is it possible, "
                    "'meron ba' = do you have). Don't get confused by "
                    "shorthand; understand the intent behind it.\n\n"
                    "Here are the preset questions and answers the page "
                    "owner has prepared, each with an optional reference "
                    "link (a video or image URL; a preset may have more "
                    "than one URL separated by spaces — include whichever "
                    "one(s) are actually relevant to your reply, not "
                    "necessarily all of them):\n"
                    f"{catalog}\n\n"
                    "Step 1: Check if a preset is a genuinely close match "
                    "in meaning (not just shared keywords) and set "
                    "matched_index accordingly (0 if none match). If the "
                    "customer's question relates to a specific machine "
                    "(fiber/CO2/UV) that has its own preset with a photo "
                    "attached, prefer matching to that specific preset "
                    "over a more generic one, since showing the customer "
                    "a picture of the actual machine helps sell it — send "
                    "a picture as often as you reasonably can when a "
                    "relevant one exists.\n\n"
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
                    "- If no preset matched: you are a genuinely capable, "
                    "intelligent assistant — freely use your own knowledge "
                    "to answer math, trivia, general facts, casual "
                    "conversation, greetings, small talk, or general "
                    "questions about laser engraving/materials/technology "
                    "as a craft, exactly as a smart, well-informed person "
                    "would. Do NOT default to a generic non-answer for "
                    "things you actually know. The ONLY thing you must "
                    "never do is state or imply a specific fact ABOUT THIS "
                    "BUSINESS (its prices, hours, turnaround time, "
                    "guarantees, stock, policies, or anything it does or "
                    "offers) that wasn't given to you in the presets — for "
                    "those specific business-fact questions only, if "
                    "nothing in the presets covers it, set reply to null "
                    "so a human can follow up instead. General knowledge "
                    "questions unrelated to this business's specific facts "
                    "are always safe to answer directly.\n\n"
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
                    "Sales expertise: act like an experienced, helpful "
                    "salesperson, not a passive FAQ lookup. When a "
                    "customer's need is vague or general (e.g. they just "
                    "say 'HM' with no context, or ask about engraving "
                    "without saying what material), proactively ask a "
                    "short clarifying question to guide them toward the "
                    "right machine — e.g. what material/item they want to "
                    "engrave, or which wattage they're considering for a "
                    "fiber laser — the same way a knowledgeable salesperson "
                    "would, instead of giving a flat non-answer. Only ask "
                    "one clarifying question at a time, keep it natural.\n\n"
                    "Voice: sound like a real Filipino small business "
                    "owner personally texting back a customer — polite "
                    "and professional, but genuinely human, never robotic "
                    "or obviously AI-generated. Use natural Taglish "
                    "(mixing Tagalog and English the way people actually "
                    "chat) when it fits the customer's own message. Avoid "
                    "patterns that read as AI-written: overly perfect or "
                    "textbook grammar, repeating the customer's question "
                    "back before answering, mechanical lists, or stiff "
                    "corporate phrases like 'I'd be happy to assist you' "
                    "or 'As an AI'. Vary your sentence openers and "
                    "phrasing naturally the way a real person texting on "
                    "their phone would, while staying respectful and "
                    "professional throughout. Keep the reply to 1-3 short "
                    "sentences, like a real chat message, never a long "
                    "paragraph.\n\n"
                    "Always-available help: whenever you're not fully "
                    "confident you've properly answered the customer's "
                    "actual question — whether because it's outside what "
                    "the presets cover, ambiguous, or something you're "
                    "genuinely unsure about — politely let them know they "
                    "can call or Viber 09178350100 for more help, in "
                    "addition to whatever you were able to answer.\n\n"
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


def call_send_api(payload):
    response = requests.post(
        "https://graph.facebook.com/v21.0/me/messages",
        params={"access_token": PAGE_ACCESS_TOKEN},
        json=payload,
        timeout=10,
    )
    if not response.ok:
        print(f"Send API error {response.status_code}: {response.text}")


def send_message(recipient_id, text):
    call_send_api(
        {
            "recipient": {"id": recipient_id},
            "message": {"text": text, "metadata": BOT_METADATA_TAG},
        }
    )


def send_image(recipient_id, image_url):
    call_send_api(
        {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": image_url, "is_reusable": True},
                },
                "metadata": BOT_METADATA_TAG,
            },
        }
    )


def schedule_followup(psid):
    marker = time.time()
    last_activity[psid] = marker

    existing_timer = followup_timers.get(psid)
    if existing_timer:
        existing_timer.cancel()

    def maybe_send():
        if last_activity.get(psid) != marker:
            return
        if paused_until.get(psid, 0) > time.time():
            return
        send_message(psid, FOLLOWUP_MESSAGE)

    timer = threading.Timer(FOLLOWUP_SECONDS, maybe_send)
    timer.daemon = True
    timer.start()
    followup_timers[psid] = timer


def apply_lead_label(psid):
    if not LEAD_LABEL_ID:
        return
    requests.post(
        f"https://graph.facebook.com/v21.0/{LEAD_LABEL_ID}/label",
        params={"user": psid, "access_token": PAGE_ACCESS_TOKEN},
        timeout=10,
    )


@app.route("/")
def home():
    return "Precision Laser Machine Engravers Philippines — Messenger auto-reply bot is running.", 200


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
            if not message:
                continue

            if message.get("is_echo"):
                if message.get("metadata") != BOT_METADATA_TAG:
                    customer_id = event.get("recipient", {}).get("id")
                    if customer_id:
                        paused_until[customer_id] = (
                            time.time() + HUMAN_PAUSE_SECONDS
                        )
                continue

            if "text" not in message:
                continue

            message_id = message.get("mid")
            if message_id:
                if message_id in processed_message_ids:
                    continue
                processed_message_ids.add(message_id)

            sender_id = event["sender"]["id"]
            if paused_until.get(sender_id, 0) > time.time():
                continue

            reply, is_lead, matched_index = generate_reply(
                message["text"], presets
            )
            send_message(sender_id, reply or FALLBACK_MESSAGE)
            if is_lead:
                apply_lead_label(sender_id)
            if 1 <= matched_index <= len(presets):
                for image_url in image_links_for(presets[matched_index - 1]):
                    send_image(sender_id, image_url)
            schedule_followup(sender_id)

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
