"""3al Tayer on WhatsApp (Meta WhatsApp Cloud API). Same agent as Telegram: handle_message() from flow.py.

Run:   uvicorn whatsapp_bot:app --port 8000
       cloudflared tunnel --url http://localhost:8000      -> gives https://xxxx.trycloudflare.com
Meta:  Callback URL = https://xxxx.trycloudflare.com/webhook   Verify token = WHATSAPP_VERIFY_TOKEN

.env:  WHATSAPP_TOKEN=...              (API Setup -> temporary access token, valid 24h)
       WHATSAPP_PHONE_NUMBER_ID=...    (API Setup -> Phone number ID, NOT the phone number)
       WHATSAPP_VERIFY_TOKEN=...       (any secret word you invent, same as in Meta's Configuration page)
       WHATSAPP_APP_SECRET=...         (App settings -> Basic -> App secret: lets us reject fake webhooks)

Staff alerts, /done, /clear, /approve stay in the Telegram team group: keep telegram_bot.py running too.
"""
import hashlib, hmac, json, os, threading
from collections import deque

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import PlainTextResponse

load_dotenv()
from flow import handle_message           # noqa: E402  (after load_dotenv)
from notify import send_whatsapp          # noqa: E402
from security import sec                  # noqa: E402

VERIFY = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
app = FastAPI()
_seen, _seen_order, _lock = set(), deque(maxlen=2000), threading.Lock()


@app.get("/webhook")
def verify(request: Request):
    """Meta's one-time handshake when you save the Callback URL."""
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and VERIFY and q.get("hub.verify_token") == VERIFY:
        print("Webhook verified by Meta")
        return PlainTextResponse(q.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


def _signature_ok(body: bytes, header: str) -> bool:
    if not APP_SECRET:
        return True                         # not configured: accept (set WHATSAPP_APP_SECRET to enforce)
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def _process(phone, text, name, to_digits):
    try:
        reply = handle_message(phone, text, name, True)
    except Exception as e:
        print("ERROR:", repr(e))
        reply = "Sorry, sar fi ghalat. Jarreb kamen mara."
    print(f"IN : {phone} | {text}\nOUT: {(reply or '(silent: human mode)').replace(chr(10), ' / ')}")
    if reply:
        send_whatsapp(to_digits, reply)


@app.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not _signature_ok(body, request.headers.get("X-Hub-Signature-256", "")):
        sec("unknown sender", "input", "webhook signature", "block", "WhatsApp webhook with a bad signature: rejected")
        return PlainTextResponse("bad signature", status_code=403)
    data = json.loads(body or b"{}")
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):                 # status updates (sent/read) are ignored
                with _lock:                                      # Meta may deliver the same message twice
                    if m.get("id") in _seen:
                        continue
                    _seen.add(m.get("id")); _seen_order.append(m.get("id"))
                    if len(_seen) > 2000:
                        _seen.discard(_seen_order[0])
                frm = m.get("from", "")
                if m.get("type") != "text":
                    background.add_task(send_whatsapp, frm, "Ekteble talabak bel kteba please 🙂")
                    continue
                background.add_task(_process, f"whatsapp:+{frm}", m["text"]["body"], names.get(frm), frm)
    return PlainTextResponse("ok")                               # answer fast; work happens in the background