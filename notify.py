"""Human-in-the-loop: alerts the sales team on Telegram when the agent escalates.

Setup (once): add the bot to a Telegram group of your team (or open a private chat with it from a
second account) and send /staff there. That chat now receives every escalation.
A teammate REPLIES to an alert -> telegram_bot.py forwards the answer to the client.
"""
import json, os, re, urllib.error, urllib.request

STAFF_FILE = "staff_chat.txt"


def staff_chat():
    if os.path.exists(STAFF_FILE):
        with open(STAFF_FILE, encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    return os.getenv("STAFF_CHAT_ID", "").strip()


def set_staff_chat(chat_id):
    with open(STAFF_FILE, "w", encoding="utf-8") as f:
        f.write(str(chat_id))


def send_telegram(chat_id, text):
    """Send a Telegram message from plain (non-async) code. Never crashes the agent."""
    token = os.getenv("TELEGRAM_TOKEN")
    if not (token and chat_id):
        return False
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text[:4000]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        print("   [telegram] send failed:", repr(e)[:80])
        return False


def notify_staff(text):
    """Send an alert to the staff chat."""
    chat = staff_chat()
    if not chat:
        print("   [notify] no staff chat yet: send /staff to the bot from the team chat")
        return False
    ok = send_telegram(chat, text)
    print("   [notify] staff alerted" if ok else "   [notify] failed")
    return ok


def client_of_alert(alert_text):
    """Find which client an alert is about (the 'Client ID:' line)."""
    m = re.search(r"Client ID:\s*(\S+)", alert_text or "")
    return m.group(1) if m else None


def send_whatsapp(to_digits, text):
    """Send a WhatsApp text through Meta's Cloud API (free test number works for up to 5 verified numbers)."""
    token, phone_id = os.getenv("WHATSAPP_TOKEN"), os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not (token and phone_id):
        print("   [whatsapp] WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID missing in .env")
        return False
    ver = os.getenv("WHATSAPP_API_VERSION", "v25.0")
    try:
        req = urllib.request.Request(
            f"https://graph.facebook.com/{ver}/{phone_id}/messages",
            data=json.dumps({"messaging_product": "whatsapp", "to": str(to_digits).lstrip("+"),
                             "type": "text", "text": {"body": text[:4096]}}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except urllib.error.HTTPError as e:
        print("   [whatsapp] send failed:", e.code, e.read()[:200])
        return False
    except Exception as e:
        print("   [whatsapp] send failed:", repr(e)[:120])
        return False


def send_to_client(client_id, text):
    """client_id is 'telegram:<chat id>' or 'whatsapp:+<number>'. Returns True if sent."""
    channel, _, addr = str(client_id).partition(":")
    if channel == "telegram":
        return send_telegram(addr, text)
    if channel == "whatsapp":
        return send_whatsapp(addr, text)
    print(f"   [send] no channel for {client_id}")
    return False