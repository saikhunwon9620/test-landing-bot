"""Test Landing page Messenger chatbot webhook.

Flow:
  Facebook Messenger -> POST /webhook -> AI (Google Gemini) -> reply via Graph API

Guided deposit flow (simple state machine, stored on disk so it survives
across gunicorn workers in the same container):
  customer says deposit intent -> bot asks payment type (KPay / Wave Pay)
  -> bot sends that method's account number as text
  -> customer sends slip image -> bot asks the amount -> transaction logged
  -> staff can review recent deposits at GET /deposits?token=<ADMIN_TOKEN>

Environment variables:
  VERIFY_TOKEN        - any secret string you choose; must match the token you
                        enter in the Facebook app's webhook settings
  PAGE_ACCESS_TOKEN   - page access token for the Test Landing page
  GEMINI_API_KEY      - Google AI Studio API key for the AI brain
                        (free tier at https://aistudio.google.com/app/apikey)
  GEMINI_MODEL        - model id (default: gemini-3.5-flash)
  KPAY_NUMBER         - KPay account shown to customers (default below)
  WAVEPAY_NUMBER      - Wave Pay account shown to customers (default below)
  ADMIN_TOKEN         - token for GET /deposits (default: VERIFY_TOKEN)
  VIBER_AUTH_TOKEN    - Viber bot auth token from admin.viber.com (optional;
                        the Viber channel stays off until this is set)
  VIBER_BOT_NAME      - display name used when sending Viber messages
  PUBLIC_BASE_URL     - public base URL of this service (for webhook setup)
  PORT                - listen port (default: 8000)
"""

import glob
import hashlib
import hmac
import json
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime

import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test-landing-bot")

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
PAGE_TOKEN = os.environ["PAGE_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
KPAY_NUMBER = os.environ.get("KPAY_NUMBER", "09777888999")
WAVEPAY_NUMBER = os.environ.get("WAVEPAY_NUMBER", "09946946294")
MIN_DEPOSIT = 10000  # kyats; business rule from the FAQ (2026-10-20)
VIBER_TOKEN = os.environ.get("VIBER_AUTH_TOKEN", "")
VIBER_BOT_NAME = os.environ.get("VIBER_BOT_NAME", "Test Landing")
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://test-landing-bot-production-b590.up.railway.app"
).rstrip("/")
VIBER_API = "https://chatapi.viber.com/pa"

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

# Deposit-flow state + transaction log live on disk (shared by all gunicorn
# workers in this container). Ephemeral: cleared when the service redeploys.
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "deposit_state.json")
TXN_FILE = os.path.join(DATA_DIR, "transactions.jsonl")
VIBER_STATE_FILE = os.path.join(DATA_DIR, "viber_deposit_state.json")


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
Match questions FLEXIBLY: if the customer rephrases something covered in the
knowledge base with different wording (e.g. "ဂိမ်း" for the listed sites),
answer from it. Only say you don't know when the topic is genuinely absent.
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
        "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.3},
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


def _fb_send(psid: str, text: str, keyboard=None) -> None:
    """Messenger adapter matching the shared flow's send() signature
    (Messenger has no custom keyboards here, so `keyboard` is ignored)."""
    send_to_messenger(psid, text)


# --------------------------------------------------------------------------
# Deposit flow
# --------------------------------------------------------------------------

PAYMENT_TYPES = {
    "kpay": {
        "label": "KPay",
        "number": KPAY_NUMBER,
        "name": "Min Min Oo",
        "keywords": ("kpay", "k pay", "kbzpay", "kbz pay", "ကေပေး"),
    },
    "wave": {
        "label": "Wave Pay",
        "number": WAVEPAY_NUMBER,
        "name": None,
        "keywords": ("wave", "wavepay", "wave pay", "ဝေ့ဗ်"),
    },
}


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        log.exception("could not save deposit state")


def _log_txn(txn: dict) -> None:
    # Always in the app logs; also appended to a file staff can review.
    log.info("DEPOSIT %s", json.dumps(txn, ensure_ascii=False))
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TXN_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(txn, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("could not write transaction log")


_MYANMAR_DIGITS = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")


def parse_amount(text: str):
    """Extract a kyat amount from free text. Handles Myanmar digits and '50k'."""
    t = text.translate(_MYANMAR_DIGITS).replace(",", "").replace(" ", "")
    m = re.search(r"(\d+)\s*[kK]", t)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r"\d+", t)
    return int(m.group()) if m else None


# Strong *intent* phrases only, so info questions ("ငွေဘယ်လိုသွင်းရမလဲ",
# "ငွေသွင်းပြီးရင် ဘယ်လောက်ကြာလဲ") still go to the AI/FAQ as before.
DEPOSIT_INTENTS = (
    "ငွေသွင်းချင်",
    "ငွေဖြည့်ချင်",
    "ငွေထည့်ချင်",
    "ယူနစ်ဝယ်ချင်",
    "ယူနစ်ဖြည့်ချင်",
    "သွင်းချင်တယ်",
    "သွင်းချင်",
    "သွင်းမယ်",
    "ငွေသွင်းမယ်",
    "ဖြည့်မယ်",
    "ထည့်မယ်",
    "deposit",
)


def wants_deposit(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in DEPOSIT_INTENTS)


def detect_pay_type(text: str):
    low = text.lower()
    for key, info in PAYMENT_TYPES.items():
        if any(k in low for k in info["keywords"]):
            return key
    return None


def send_payment_details(psid, ptype, state, send, save) -> bool:
    """Send the chosen method's account details as text; wait for the slip."""
    info = PAYMENT_TYPES[ptype]
    state[psid] = {"step": "slip", "pay_type": ptype}
    save(state)
    name_line = f" ({info['name']})" if info.get("name") else ""
    send(
        psid,
        f"{info['label']} အကောင့်: {info['number']}{name_line}\n"
        "ငွေလွှဲပြီးရင် slip ပုံကို ဒီမှာ ပြန်ပို့ပေးပါနော် 📸",
    )
    return True


def _complete_deposit(psid, state, send, save, channel="messenger") -> bool:
    st = state.pop(psid, {})
    save(state)
    amount = st.get("amount") or 0
    _log_txn(
        {
            "psid": psid,
            "channel": channel,
            "amount": amount,
            "units": amount // 1000 if amount else None,
            "pay_type": st.get("pay_type"),
            "time": datetime.now().isoformat(timespec="seconds"),
            "slip": True,
        }
    )
    send(
        psid,
        f"✅ Slip လက်ခံရရှိပါပြီ! {amount:,} ကျပ် ({amount // 1000} ယူနစ်) ကို "
        "ဝန်ထမ်းက စစ်ဆေးပြီး မကြာခင် အကောင့်ထဲ ထည့်ပေးပါမယ်။ "
        "ကျေးဇူးတင်ပါတယ်ရှင့် 🙏",
    )
    return True


def _check_amount(psid, text, state, send, save, channel="messenger"):
    """Validate an amount reply. Returns True/False if handled, None if invalid."""
    st = state[psid]
    amount = parse_amount(text)
    if amount is None:
        send(psid, "ပမာဏကို နံပါတ်နဲ့ ပို့ပေးပါနော် (ဥပမာ 10000) 🙏")
        return True
    if amount < MIN_DEPOSIT:
        send(
            psid,
            "အနည်းဆုံး ၁၀,၀၀၀ ကျပ်ကနေ စသွင်းလို့ရပါတယ်ရှင့်။ "
            "ပမာဏ ပြန်ပို့ပေးပါနော် 🙏",
        )
        return True
    if amount % 1000 != 0:
        send(
            psid,
            "ယူနစ်က ၁ ယူနစ် = ၁၀၀၀ ကျပ် နှုန်းမို့ ၁၀၀၀ နဲ့ စားလို့ပြတ်တဲ့ "
            "ပမာဏလေး ပြန်ပို့ပေးပါနော် (ဥပမာ 10000, 20000) 🙏",
        )
        return True
    st["amount"] = amount
    save(state)
    return _complete_deposit(psid, state, send, save, channel)


def handle_deposit_text(psid, text, state, send=_fb_send, save=_save_state,
                        channel="messenger", paytype_keyboard=None) -> bool:
    """Drive the deposit state machine for a text message (any channel).

    Returns True when the message was consumed by the flow."""
    st = state.get(psid)
    low = text.lower()

    if st and ("cancel" in low or "မလုပ်တော့" in text or "ရပ်မယ်" in text):
        state.pop(psid, None)
        save(state)
        send(
            psid,
            "ရပါတယ်နော်၊ ငွေသွင်းတာ ရပ်လိုက်ပါပြီ။ "
            "တခြား သိချင်တာ ရှိရင် မေးလို့ရပါတယ် 😊",
        )
        return True

    ptype = detect_pay_type(text)

    if not st:
        if wants_deposit(text):
            if ptype:  # e.g. "KPay နဲ့ ငွေသွင်းချင်တယ်" — type already known
                return send_payment_details(psid, ptype, state, send, save)
            state[psid] = {"step": "paytype"}
            save(state)
            send(
                psid,
                "ဘယ် payment အမျိုးအစားနဲ့ ငွေသွင်းချင်ပါသလဲရှင့်။ "
                "KPay, Wave Pay စသည့် ငွေပေးချေမှု အမျိုးအစားတွေနဲ့ "
                "ပေးသွင်းနိုင်ပါတယ်။ အနည်းဆုံး ၁၀,၀၀၀ ကျပ်က စသွင်းနိုင်ပါတယ်။",
                keyboard=paytype_keyboard,
            )
            return True
        return False

    step = st.get("step")
    if step == "paytype":
        if ptype:
            return send_payment_details(psid, ptype, state, send, save)
        send(
            psid,
            "ငွေသွင်းဖို့ KPay လား, Wave Pay လား ရွေးပေးပါနော် 🙏 "
            "(မသွင်းတော့ဘူးဆိုရင် 'cancel' လို့ ပို့လို့ရပါတယ်)",
            keyboard=paytype_keyboard,
        )
        return True

    if step == "slip":
        send(
            psid,
            "လွှဲပြီးရင် slip ပုံကို ပို့ပေးပါနော် 📸 "
            "(မသွင်းတော့ဘူးဆိုရင် 'cancel' လို့ ပို့လို့ရပါတယ်)",
        )
        return True

    if step == "amount":  # slip already received; waiting for the amount
        return _check_amount(psid, text, state, send, save, channel)

    return False


def handle_image(psid, state, send=_fb_send, save=_save_state) -> None:
    """A customer image is treated as a payment slip."""
    st = state.get(psid)
    if st and st.get("step") == "amount":
        return  # already holding a slip; waiting for the amount text
    if not st:
        state[psid] = {"step": "amount", "slip": True}
    else:
        # Flow in progress (paytype/slip step): remember the slip, ask amount.
        state[psid] = {**st, "step": "amount", "slip": True}
    save(state)
    send(
        psid,
        "Slip ပုံ လက်ခံရရှိပါပြီ 📸 သွင်းခဲ့တဲ့ ပမာဏလေးကို "
        "နံပါတ်နဲ့ ပို့ပေးပါနော် (ဥပမာ 10000)",
    )


# --------------------------------------------------------------------------
# Viber channel — same brain (Gemini + knowledge base) and the same deposit
# flow, different transport. Active only when VIBER_AUTH_TOKEN is set.
# --------------------------------------------------------------------------

WELCOME_TEXT = (
    "မင်္ဂလာပါရှင့်။ Test Landing ကနေ ကြိုဆိုပါတယ်။ "
    "ဘောလုံး၊ ကာစီနိုတွေ ကစားဖို့အတွက် ဘာများ ဝန်ဆောင်မှုပေးရမလဲရှင့်။ "
    "သိချင်တာလေး မေးလို့ရပါတယ် 🙏"
)


def _viber_load_state() -> dict:
    try:
        with open(VIBER_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _viber_save_state(state: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(VIBER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        log.exception("could not save viber deposit state")


def viber_send(user_id: str, text: str, keyboard=None) -> None:
    if not VIBER_TOKEN:
        log.error("VIBER_AUTH_TOKEN not set; cannot send Viber message")
        return
    payload = {
        "receiver": user_id,
        "min_api_version": 7,
        "sender": {"name": VIBER_BOT_NAME},
        "type": "text",
        "text": text[:6000],
    }
    if keyboard:
        payload["keyboard"] = keyboard
    try:
        r = requests.post(
            f"{VIBER_API}/send_message",
            headers={"X-Viber-Auth-Token": VIBER_TOKEN},
            json=payload,
            timeout=15,
        )
        if r.status_code != 200:
            log.error("viber send failed %s: %s", r.status_code, r.text[:300])
    except requests.RequestException:
        log.exception("viber send request failed")


def _paytype_keyboard() -> dict:
    return {
        "Type": "keyboard",
        "DefaultHeight": True,
        "Buttons": [
            {"Columns": 3, "Rows": 1, "Text": "KPay",
             "ActionType": "reply", "ActionBody": "KPay"},
            {"Columns": 3, "Rows": 1, "Text": "Wave Pay",
             "ActionType": "reply", "ActionBody": "Wave Pay"},
        ],
    }


def viber_deposit_text(user_id: str, text: str) -> bool:
    return handle_deposit_text(
        user_id, text, _viber_load_state(),
        send=viber_send, save=_viber_save_state,
        channel="viber", paytype_keyboard=_paytype_keyboard(),
    )


def viber_deposit_image(user_id: str) -> None:
    handle_image(
        user_id, _viber_load_state(), send=viber_send, save=_viber_save_state
    )


# --------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------

def handle_event(ev: dict) -> None:
    msg = ev.get("message", {})
    if msg.get("is_echo"):
        return  # ignore our own echoes
    mid = msg.get("mid")
    if mid and mid in seen_mids:
        return  # Facebook retried this event; already replied
    if mid:
        seen_mids.append(mid)
    psid = ev["sender"]["id"]

    state = _load_state()
    attachments = msg.get("attachments") or []
    if any(a.get("type") == "image" for a in attachments):
        handle_image(psid, state)
        return
    if "text" not in msg:
        return  # ignore stickers, locations, etc. for now

    text = msg["text"].strip()
    log.info("incoming from %s: %s", psid, text[:120])
    if handle_deposit_text(psid, text, state):
        return
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


@app.get("/deposits")
def deposits():
    """Staff view: recent deposit transactions (token-protected)."""
    token = os.environ.get("ADMIN_TOKEN", VERIFY_TOKEN)
    if request.args.get("token") != token:
        return "forbidden", 403
    items = []
    try:
        with open(TXN_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
    except FileNotFoundError:
        pass
    return {"count": len(items), "deposits": items[-50:]}, 200


@app.post("/viber/webhook")
def viber_webhook():
    """Viber bot events. Signature: HMAC-SHA256 of the raw body, keyed with
    the bot auth token, sent as X-Viber-Content-Signature."""
    raw = request.get_data()
    if VIBER_TOKEN:
        sig = request.headers.get("X-Viber-Content-Signature", "")
        expected = hmac.new(VIBER_TOKEN.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return "bad signature", 403
    event = request.get_json(silent=True) or {}
    etype = event.get("event")

    if etype == "conversation_started":
        # Returning a message object sends it as the welcome message.
        user_id = (event.get("user") or {}).get("id", "")
        return {
            "receiver": user_id,
            "min_api_version": 7,
            "sender": {"name": VIBER_BOT_NAME},
            "type": "text",
            "text": WELCOME_TEXT,
        }, 200

    if etype == "message":
        token = event.get("message_token")
        if token and f"viber:{token}" in seen_mids:
            return "ok", 200  # Viber retried this event; already replied
        if token:
            seen_mids.append(f"viber:{token}")
        sender = (event.get("sender") or {}).get("id")
        msg = event.get("message") or {}
        if sender:
            if msg.get("type") == "picture":
                viber_deposit_image(sender)
            elif msg.get("type") == "text":
                text = (msg.get("text") or "").strip()
                if text:
                    log.info("viber incoming from %s: %s", sender, text[:120])
                    if not viber_deposit_text(sender, text):
                        try:
                            reply = generate_reply(f"viber:{sender}", text)
                        except Exception:
                            log.exception("AI generation failed")
                            reply = "ခဏလေးစောင့်ပေးပါနော်၊ လူသားဝန်ထမ်းနဲ့ ဆက်သွယ်ပေးပါမယ်။"
                        viber_send(sender, reply)
    return "ok", 200


@app.get("/viber/setup")
def viber_setup():
    """One-time: register this service's webhook URL with Viber.
    Open /viber/setup?token=<ADMIN_TOKEN> in a browser after deploying."""
    token = os.environ.get("ADMIN_TOKEN", VERIFY_TOKEN)
    if request.args.get("token") != token:
        return "forbidden", 403
    if not VIBER_TOKEN:
        return "VIBER_AUTH_TOKEN is not set on the server", 400
    try:
        r = requests.post(
            f"{VIBER_API}/set_webhook",
            headers={"X-Viber-Auth-Token": VIBER_TOKEN},
            json={
                "url": f"{PUBLIC_BASE_URL}/viber/webhook",
                "event_types": [
                    "message", "conversation_started", "subscribed",
                    "unsubscribed", "delivered", "seen", "failed",
                ],
                "send_name": True,
                "send_photo": True,
            },
            timeout=15,
        )
        return r.text, r.status_code
    except requests.RequestException:
        log.exception("viber set_webhook failed")
        return "request to Viber failed", 502


@app.get("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

