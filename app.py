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
import html as html_lib
import json
import uuid
from urllib.parse import quote
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime

import requests
from flask import Flask, redirect, request, send_from_directory

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
INBOX_FILE = os.path.join(DATA_DIR, "inbox.jsonl")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
PAUSED_FILE = os.path.join(DATA_DIR, "paused.json")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
MAX_UPLOAD = 25 * 1024 * 1024  # 25 MB staff-upload cap


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


# --------------------------------------------------------------------------
# Staff inbox storage — every conversation (both channels) is appended to
# inbox.jsonl; users.json caches display names; paused.json holds the
# "channel:user_id" keys where a human has taken over and the bot must
# stay silent. All ephemeral like the deposit data (cleared on redeploy).
# --------------------------------------------------------------------------

def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        log.exception("could not write %s", path)


def log_message(channel, user_id, direction, text, by="bot", kind="text",
                name=None, media=None, fname=None) -> None:
    rec = {
        "channel": channel, "user_id": user_id, "dir": direction,
        "text": text, "by": by, "kind": kind,
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    if media:
        rec["media"] = media
    if fname:
        rec["fname"] = fname
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(INBOX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("could not log inbox message")
    if name:
        remember_user(channel, user_id, name)


def remember_user(channel, user_id, name) -> None:
    users = _read_json(USERS_FILE, {})
    key = f"{channel}:{user_id}"
    u = users.get(key, {"channel": channel, "user_id": user_id})
    u["name"] = name
    u["last_seen"] = datetime.now().isoformat(timespec="seconds")
    users[key] = u
    _write_json(USERS_FILE, users)


def user_name(channel, user_id) -> str:
    users = _read_json(USERS_FILE, {})
    u = users.get(f"{channel}:{user_id}") or {}
    if u.get("name"):
        return u["name"]
    if channel == "messenger":
        return messenger_name(user_id)
    return f"Viber user •••{user_id[-6:]}" if len(user_id) > 6 else user_id


def messenger_name(psid: str) -> str:
    """Best-effort display name for a Messenger user (Graph profile API)."""
    users = _read_json(USERS_FILE, {})
    key = f"messenger:{psid}"
    if users.get(key, {}).get("name"):
        return users[key]["name"]
    name = None
    try:
        r = requests.get(
            f"https://graph.facebook.com/v21.0/{psid}",
            params={"fields": "name", "access_token": PAGE_TOKEN},
            timeout=10,
        )
        if r.status_code == 200:
            name = r.json().get("name")
    except requests.RequestException:
        pass
    name = name or f"FB user •••{psid[-6:]}"
    remember_user("messenger", psid, name)
    return name


def is_paused(channel, user_id) -> bool:
    return f"{channel}:{user_id}" in set(_read_json(PAUSED_FILE, []))


def set_paused(channel, user_id, on: bool) -> None:
    s = set(_read_json(PAUSED_FILE, []))
    key = f"{channel}:{user_id}"
    if on:
        s.add(key)
    else:
        s.discard(key)
    _write_json(PAUSED_FILE, sorted(s))


def _read_inbox(limit=1000):
    items = []
    try:
        with open(INBOX_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
    except FileNotFoundError:
        pass
    return items[-limit:]


def _post_messenger(psid: str, text: str) -> bool:
    """Raw Messenger send (typing indicator + text). True on success."""
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
            return False
        if r.status_code != 200:
            log.error("send failed %s: %s", r.status_code, r.text[:300])
            return False
    return True


def send_to_messenger(psid: str, text: str) -> None:
    if _post_messenger(psid, text):
        log_message("messenger", psid, "out", text, by="bot")


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


def _viber_deliver(payload) -> bool:
    """POST a send_message payload; True only when Viber's body says
    status 0 (HTTP 200 alone is not proof of delivery)."""
    try:
        r = requests.post(
            f"{VIBER_API}/send_message",
            headers={"X-Viber-Auth-Token": VIBER_TOKEN},
            json=payload,
            timeout=20,
        )
    except requests.RequestException:
        log.exception("viber send request failed")
        return False
    ok = False
    if r.status_code == 200:
        try:
            ok = r.json().get("status") == 0
        except ValueError:
            ok = False
    if not ok:
        log.error("viber send NOT delivered %s: %s", r.status_code, r.text[:300])
    return ok


def viber_send(user_id: str, text: str, keyboard=None, by="bot") -> bool:
    """Send a Viber text message; logs it when Viber confirms delivery."""
    if not VIBER_TOKEN:
        log.error("VIBER_AUTH_TOKEN not set; cannot send Viber message")
        return False
    payload = {
        "receiver": user_id,
        "min_api_version": 7,
        "sender": {"name": VIBER_BOT_NAME},
        "type": "text",
        "text": text[:6000],
    }
    if keyboard:
        payload["keyboard"] = keyboard
    if _viber_deliver(payload):
        log_message("viber", user_id, "out", text, by=by)
        return True
    return False


def viber_send_media(user_id, url, fname, size, is_image, by="staff") -> bool:
    """Send a Viber picture/file message pointing at a hosted URL."""
    if not VIBER_TOKEN:
        return False
    payload = {
        "receiver": user_id,
        "min_api_version": 7,
        "sender": {"name": VIBER_BOT_NAME},
    }
    if is_image:
        payload.update({"type": "picture", "media": url, "thumbnail": url})
    else:
        payload.update({"type": "file", "media": url, "size": size,
                        "file_name": fname})
    if _viber_deliver(payload):
        log_message("viber", user_id, "out",
                    "(ပုံ)" if is_image else f"(ဖိုင်: {fname})",
                    by=by, kind="image" if is_image else "file",
                    media=url, fname=fname)
        return True
    return False


def _post_messenger_media(psid, url, is_image) -> bool:
    """Send a Messenger image/file attachment by public URL."""
    payload = {
        "attachment": {
            "type": "image" if is_image else "file",
            "payload": {"url": url, "is_reusable": True},
        }
    }
    try:
        r = requests.post(
            GRAPH_URL,
            params={"access_token": PAGE_TOKEN},
            json={"recipient": {"id": psid}, "message": payload},
            timeout=20,
        )
    except requests.RequestException:
        log.exception("messenger media send failed")
        return False
    if r.status_code != 200:
        log.error("messenger media NOT delivered %s: %s",
                  r.status_code, r.text[:300])
        return False
    return True


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
    img = next((a for a in attachments if a.get("type") == "image"), None)
    fil = next((a for a in attachments if a.get("type") == "file"), None)
    if img or fil:
        att = img or fil
        url = (att.get("payload") or {}).get("url")
        kind = "image" if img else "file"
        log_message("messenger", psid, "in", "(ပုံ)" if img else "(ဖိုင်)",
                    kind=kind, media=url)
        if is_paused("messenger", psid):
            return  # staff is handling this customer; stay silent
        handle_image(psid, state)  # slip handling (image or file)
        return
    if "text" not in msg:
        return  # ignore stickers, locations, etc. for now

    text = msg["text"].strip()
    log.info("incoming from %s: %s", psid, text[:120])
    log_message("messenger", psid, "in", text)
    if is_paused("messenger", psid):
        return  # staff is handling this customer; stay silent
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
        user = event.get("user") or {}
        user_id = user.get("id", "")
        if user_id and user.get("name"):
            remember_user("viber", user_id, user["name"])
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
        sname = (event.get("sender") or {}).get("name")
        msg = event.get("message") or {}
        if sender:
            if msg.get("type") == "picture":
                log_message("viber", sender, "in", "(ပုံ)", kind="image",
                            name=sname, media=msg.get("media"))
                if not is_paused("viber", sender):
                    viber_deposit_image(sender)
            elif msg.get("type") == "file":
                fname = msg.get("file_name") or "ဖိုင်"
                log_message("viber", sender, "in", f"(ဖိုင်: {fname})",
                            kind="file", name=sname,
                            media=msg.get("media"), fname=fname)
                if not is_paused("viber", sender):
                    viber_deposit_image(sender)  # a file can be the slip too
            elif msg.get("type") == "text":
                text = (msg.get("text") or "").strip()
                if text:
                    log.info("viber incoming from %s: %s", sender, text[:120])
                    log_message("viber", sender, "in", text, name=sname)
                    if is_paused("viber", sender):
                        return "ok", 200  # staff is handling; stay silent
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


# --------------------------------------------------------------------------
# Staff inbox web UI — token-gated pages to read customer conversations
# (Messenger + Viber in one place), reply as a human, and pause the bot
# per customer while staff are handling them.
# --------------------------------------------------------------------------

def _staff_ok(token) -> bool:
    return bool(token) and token == os.environ.get("ADMIN_TOKEN", VERIFY_TOKEN)


_INBOX_CSS = """
body{font-family:sans-serif;max-width:760px;margin:0 auto;padding:12px;background:#f5f5f7}
a{color:#7b2ff7;text-decoration:none}
.card{background:#fff;border-radius:10px;padding:12px 14px;margin:8px 0;box-shadow:0 1px 3px #0001}
.badge{font-size:11px;padding:2px 8px;border-radius:8px;color:#fff}
.fb{background:#1877f2}.vb{background:#7360f2}.paused{background:#e67e22}
.msg{max-width:85%;padding:8px 12px;border-radius:14px;margin:6px 0;white-space:pre-wrap;word-break:break-word}
.in{background:#fff;box-shadow:0 1px 2px #0001}
.out{background:#dcf8c6;margin-left:auto}
.staff{background:#ffe9a8}
.t{font-size:11px;color:#888;margin-top:2px}
.row{display:flex;gap:8px;align-items:flex-start}.row.out{justify-content:flex-end}
a.conv{display:block;color:inherit}
.replybar{position:sticky;bottom:0;background:#f5f5f7;padding:10px 0}
textarea{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #ccc}
button{background:#7b2ff7;color:#fff;border:0;padding:10px 18px;border-radius:8px;font-size:15px}
button.alt{background:#888}
"""


def _page(title, body, refresh=False):
    meta = '<meta http-equiv="refresh" content="30">' if refresh else ""
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta}<title>{html_lib.escape(title)}</title>"
        f"<style>{_INBOX_CSS}</style></head><body>{body}</body></html>"
    )


@app.get("/inbox")
def inbox_list():
    token = request.args.get("token", "")
    if not _staff_ok(token):
        return "forbidden", 403
    convs = {}
    for rec in _read_inbox():
        key = f"{rec['channel']}:{rec['user_id']}"
        c = convs.setdefault(key, {"channel": rec["channel"],
                                   "user_id": rec["user_id"], "count": 0})
        c["last"] = rec
        c["count"] += 1
    ordered = sorted(convs.values(),
                     key=lambda c: c["last"]["time"], reverse=True)
    rows = ""
    for c in ordered:
        last = c["last"]
        preview = html_lib.escape(last["text"][:150])
        who = "👤 " if last["dir"] == "in" else "🤖 "
        badge = ("fb'>Messenger" if c["channel"] == "messenger"
                 else "vb'>Viber")
        p = (" <span class='badge paused'>⏸ bot ရပ်ထား</span>"
             if is_paused(c["channel"], c["user_id"]) else "")
        rows += (
            f"<a class='conv' href='/inbox/chat?token={token}"
            f"&channel={c['channel']}&user={quote(c['user_id'], safe='')}'>"
            f"<div class='card'>"
            f"<b>{html_lib.escape(user_name(c['channel'], c['user_id']))}</b>"
            f" <span class='badge {badge}</span>{p}"
            f"<div class='t'>{last['time']} · {c['count']} messages</div>"
            f"<div>{who}{preview}</div>"
            f"<div class='t'>💬 နှိပ်ပြီး အပြည့်ဖတ် / ပြန်ရေးရန်</div>"
            f"</div></a>"
        )
    if not rows:
        rows = "<div class='card'>စကားဝိုင်း မရှိသေးပါ</div>"
    body = (
        "<h2>📥 Staff Inbox — Test Landing</h2>"
        f"<p><a href='/deposits?token={token}'>💰 ငွေသွင်း မှတ်တမ်း</a></p>"
        + rows
    )
    return _page("Staff Inbox", body, refresh=True)


@app.get("/inbox/chat")
def inbox_chat():
    token = request.args.get("token", "")
    if not _staff_ok(token):
        return "forbidden", 403
    channel = request.args.get("channel", "")
    user_id = request.args.get("user", "")
    name = user_name(channel, user_id)
    msgs = [r for r in _read_inbox()
            if r["channel"] == channel and r["user_id"] == user_id][-100:]
    bubbles = ""
    for r in msgs:
        cls = "in" if r["dir"] == "in" else "out"
        if r["dir"] == "out" and r.get("by") == "staff":
            cls += " staff"
        by = {"bot": "🤖 Bot", "staff": "🧑‍💼 Staff"}.get(r.get("by"), "")
        label = name if r["dir"] == "in" else by
        extra = ""
        if r.get("media"):
            if r.get("kind") == "image":
                extra = (f"<br><img src='{html_lib.escape(r['media'])}' "
                         f"style='max-width:230px;border-radius:8px'>")
            else:
                extra = (f"<br><a href='{html_lib.escape(r['media'])}' "
                         f"target='_blank'>📎 "
                         f"{html_lib.escape(r.get('fname') or 'ဖိုင်')}</a>")
        bubbles += (
            f"<div class='row {cls}'><div class='msg {cls}'>"
            f"{html_lib.escape(r['text'])}{extra}"
            f"<div class='t'>{html_lib.escape(label)} · {r['time']}</div>"
            f"</div></div>"
        )
    paused = is_paused(channel, user_id)
    pbtn = (
        f"<form method='post' action='/inbox/pause' style='display:inline'>"
        f"<input type='hidden' name='token' value='{token}'>"
        f"<input type='hidden' name='channel' value='{channel}'>"
        f"<input type='hidden' name='user' value='{user_id}'>"
        f"<input type='hidden' name='on' value='{'0' if paused else '1'}'>"
        f"<button class='alt' type='submit'>"
        f"{'▶️ Bot ပြန်ဖွင့်' if paused else '⏸ Bot ခဏရပ် (လူ ဝင်ပြောမယ်)'}</button></form>"
    )
    body = (
        f"<p><a href='/inbox?token={token}'>← Inbox</a></p>"
        f"<h3>{html_lib.escape(name)} "
        f"({'Messenger' if channel == 'messenger' else 'Viber'})</h3>"
        f"{'<p style=\"color:#c0392b\">⚠️ <b>ပို့လို့ မရလိုက်ပါ</b> — customer က bot ကို ပိတ်ထား/subscribe မလုပ်ထားတာ ဖြစ်နိုင်ပါတယ်။</p>' if request.args.get('err') else ''}"
        f"{'<p>⏸ <b>Bot ရပ်ထားသည်</b> — ဝန်ထမ်း ပြောနေချိန် bot က ပြန်မဖြေပါ</p>' if paused else ''}"
        f"{bubbles or '<p>စာ မရှိသေးပါ</p>'}"
        f"<div class='replybar'>"
        f"<form method='post' action='/inbox/reply' enctype='multipart/form-data'>"
        f"<input type='hidden' name='token' value='{token}'>"
        f"<input type='hidden' name='channel' value='{channel}'>"
        f"<input type='hidden' name='user' value='{user_id}'>"
        f"<textarea name='text' rows='3' placeholder='ပြန်စာ ရေးပါ...'></textarea>"
        f"<p>📎 ပုံ/ဖိုင် တွဲပို့ရန်: <input type='file' name='file'></p>"
        f"<p><button type='submit'>📤 ပို့မယ်</button></p></form>"
        f"{pbtn}"
        f"</div>"
        f"<p><a href='/inbox/chat?token={token}&channel={channel}&user={quote(user_id, safe='')}'>↻ Refresh</a></p>"
    )
    return _page(f"Chat — {name}", body)


def _save_upload(file_storage):
    """Save a staff upload under a UUID name; returns (name, size) or
    (None, 0) when it exceeds the size cap."""
    orig = file_storage.filename or "file"
    ext = os.path.splitext(orig)[1].lower()[:6]
    if not re.fullmatch(r"\.[a-z0-9]+", ext or ""):
        ext = ".bin"
    name = f"{uuid.uuid4()}{ext}"
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, name)
    file_storage.save(path)
    size = os.path.getsize(path)
    if size > MAX_UPLOAD:
        os.remove(path)
        return None, 0
    return name, size


@app.get("/media/<name>")
def media_file(name):
    """Serve a staff-uploaded file (UUID names only)."""
    if not re.fullmatch(r"[a-f0-9-]{36}\.[a-z0-9]{1,6}", name):
        return "not found", 404
    return send_from_directory(MEDIA_DIR, name)


@app.post("/inbox/reply")
def inbox_reply():
    token = request.form.get("token", "")
    if not _staff_ok(token):
        return "forbidden", 403
    channel = request.form.get("channel", "")
    user_id = request.form.get("user", "")
    text = (request.form.get("text") or "").strip()
    upload = request.files.get("file")
    has_file = upload is not None and bool(upload.filename)
    results = []
    if channel and user_id and (text or has_file):
        if has_file:
            saved, size = _save_upload(upload)
            if saved:
                url = f"{PUBLIC_BASE_URL}/media/{saved}"
                is_image = (upload.mimetype or "").startswith("image/")
                fname = upload.filename or saved
                if channel == "viber":
                    results.append(
                        viber_send_media(user_id, url, fname, size, is_image))
                else:
                    ok = _post_messenger_media(user_id, url, is_image)
                    if ok:
                        log_message(
                            "messenger", user_id, "out",
                            "(ပုံ)" if is_image else f"(ဖိုင်: {fname})",
                            by="staff",
                            kind="image" if is_image else "file",
                            media=url, fname=fname)
                    results.append(ok)
            else:
                results.append(False)  # too large
        if text:
            if channel == "viber":
                results.append(viber_send(user_id, text, by="staff"))
            elif _post_messenger(user_id, text):
                log_message("messenger", user_id, "out", text, by="staff")
                results.append(True)
            else:
                results.append(False)
    back = (f"/inbox/chat?token={token}&channel={channel}"
            f"&user={quote(user_id, safe='')}")
    if (text or has_file) and not (results and all(results)):
        back += "&err=1"
    return redirect(back)


@app.post("/inbox/pause")
def inbox_pause():
    token = request.form.get("token", "")
    if not _staff_ok(token):
        return "forbidden", 403
    channel = request.form.get("channel", "")
    user_id = request.form.get("user", "")
    if channel and user_id:
        set_paused(channel, user_id, request.form.get("on") == "1")
    return redirect(f"/inbox/chat?token={token}&channel={channel}"
                    f"&user={quote(user_id, safe='')}")


@app.get("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
