"""Test Landing page Messenger chatbot webhook.

Flow:
  Facebook Messenger -> POST /webhook -> AI (Google Gemini) -> reply via Graph API

Environment variables:
  VERIFY_TOKEN        - any secret string you choose; must match the token you
                        enter in the Facebook app's webhook settings
  PAGE_ACCESS_TOKEN   - page access token for the Test Landing page
  GEMINI_API_KEY      - Google AI Studio API key for the AI brain
                        (free tier at https://aistudio.google.com/app/apikey)
  GEMINI_MODEL        - model id (default: gemini-2.5-flash)
  PORT                - listen port (default: 8000)
"""

import glob
import logging
import os
from collections import defaultdict, deque

import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test-landing-bot")

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
PAGE_TOKEN = os.environ["PAGE_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

app = Flask(__name__)

GRAPH_URL = "https://graph.facebook.com/v21.0/me/messages"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Short in-memory conversation history per user (last 6 turns).
# Lost on restart; fine for v1. Use Redis/DB for production scale.
histories: dict[str, deque] = defaultdict(lambda: deque(maxlen=12))

# Message IDs already processed. Facebook retries webhook POSTs when it does
# not get a fast 200; without this the bot would send duplicate replies.
# Bounded so memory cannot grow without limit. Lost on restart (v1).
seen_mids: deque = deque(maxlen=500)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_knowledge() -> str:
    """Load every .md file in knowledge/ as the bot's training data."""
    parts = []
    for path in sorted(glob.glob(os.path.join(BASE_DIR, "knowledge", "*.md"))):
        with open(path, encoding="utf-8") as f:
            parts.append(f"### {os.path.basename(path)}\n{f.read().strip()}")
    return "\n\n".join(parts)


SYSTEM_PROMPT = """\
You are the friendly AI assistant for the Facebook page "Test Landing".
Reply in the SAME language the customer writes in (Burmese or English).
Keep answers short and conversational, like a helpful shop assistant chatting.
Answer ONLY from the knowledge base below. If the answer is not there, say so
honestly (in the customer's language) and offer to connect them with a human.
Never invent prices, addresses, opening hours, or promises not in the knowledge base.

KNOWLEDGE BASE:
{kb}"""


def generate_reply(psid: str, text: str) -> str:
    # Gemini roles are "user"/"model" (not "assistant").
    contents = []
    for h in histories[psid]:
        role = "model" if h["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": h["content"]}]})
    contents.append({"role": "user", "parts": [{"text": text}]})
    payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT.format(kb=load_knowledge())}]
        },
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.7},
    }
    r = requests.post(
        GEMINI_URL, params={"key": GEMINI_API_KEY}, json=payload, timeout=60
    )
    r.raise_for_status()
    reply = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    histories[psid].append({"role": "user", "content": text})
    histories[psid].append({"role": "assistant", "content": reply})
    return reply


def send_to_messenger(psid: str, text: str) -> None:
    payloads = [
        {"sender_action": "typing_on"},
        {"message": {"text": text[:2000]}},
    ]
    for p in payloads:
        try:
            r = requests.post(
                GRAPH_URL,
                params={"access_token": PAGE_TOKEN},
                json={"recipient": {"id": psid}, **p},
                timeout=15,
            )
        except requests.RequestException:
            log.exception("send request failed")
            return
        if r.status_code != 200:
            log.error("send failed %s: %s", r.status_code, r.text[:300])
            return


def handle_event(ev: dict) -> None:
    msg = ev.get("message", {})
    if msg.get("is_echo") or "text" not in msg:
        return  # ignore our own echoes and non-text events
    mid = msg.get("mid")
    if mid and mid in seen_mids:
        return  # Facebook retried this event; already replied
    if mid:
        seen_mids.append(mid)
    psid = ev["sender"]["id"]
    text = msg["text"].strip()
    log.info("incoming from %s: %s", psid, text[:120])
    try:
        reply = generate_reply(psid, text)
    except Exception:
        log.exception("AI generation failed")
        reply = "ခဏလေးစောင့်ပေးပါနော်၊ လူသားဝန်ထမ်းနဲ့ ဆက်သွယ်ပေးပါမယ်။ / One moment please, connecting you with our team."
    send_to_messenger(psid, reply)


@app.get("/webhook")
def verify():
    """Facebook webhook verification handshake."""
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403


@app.post("/webhook")
def incoming():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("object") != "page":
        return "ok", 200
    for entry in data.get("entry", []):
        for ev in entry.get("messaging", []):
            handle_event(ev)
    return "ok", 200


@app.get("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
