"""Security layer of 3al Tayer: every check reports itself to the dashboard as a "security" event.

event: security {client, layer, check, verdict: pass|flag|block, detail}
layers (where they sit in the pipeline):
  input   Input guard     client -> AI      rate limit, length cap, prompt-injection detector
  plan    Plan validator  AI -> tools       only known actions, only real product codes, valid quantities
  data    Data guard      tools             prices/stock only from the database, a client sees only their own data
  lock    Write lock      -> database       only CODE writes, only after the client's explicit OK
  output  Output guard    -> client         no codes, prices, links or emails in AI-written text
  access  Staff access    team commands     /done /approve /audit only from the registered staff chat
"""
import os, re, time
from collections import defaultdict, deque

from events import emit

RATE_MAX, RATE_WINDOW = 20, 60          # max messages per client per minute (protects cost and the bot)
MAX_TEXT = 500

ALLOWED_OPS = {"set", "remove", "ask", "unknown", "cancel", "clear", "last_order", "browse", "info", "my_orders",
               "cancel_last", "ask_more", "say", "backorder", "skip_rest", "escalate"}

INJECTION = [
    (r"ignore|disregard|forget|override|bypass", r"instruction|rule|prompt|previous|above|system|polic",
     "override instructions"),
    (r"system prompt|your prompt|your instructions|developer mode|jailbreak|DAN\b", None, "prompt extraction"),
    (r"you are now|\bact as\b|pretend (to be|you)|roleplay|new role", None, "role change"),
    (r"api[ _-]?key|token|password|credentials|\.env|creds\.json", None, "secret request"),
    (r"\b(price|prix|se3r|s3r)\w*\s*(=|is|to|:|becomes)?\s*(0(?![.,]?\d)|zero|free)", None, "price manipulation"),
    (r"100\s*%|for free|gratuit|majjanan|ببلاش|مجانا", None, "free goods"),
    (r"other client|all clients|every client|clients list|other shops|kel l zbeyen|database dump", None,
     "other clients' data"),
    (r"<script|select .* from|drop table|;--|\{\{|\$\{", None, "code injection"),
    (r"ignore (all|everything)|تجاهل|انسى التعليمات|ensa l (rules|t3limet)", None, "override instructions"),
]

_rate = defaultdict(deque)


def sec(client, layer, check, verdict, detail=""):
    emit("security", client=client, layer=layer, check=check, verdict=verdict, detail=str(detail)[:200])


def check_input(client, phone, text, rate_limit=True):
    """Runs on EVERY incoming message, before any AI call. Returns (allowed, suspicious_reasons).
    rate_limit=False for the offline evaluation, which sends 20 cases in a few seconds on purpose."""
    now = time.time()
    q = _rate[phone]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    q.append(now)
    if rate_limit and len(q) > RATE_MAX:
        if len(q) == RATE_MAX + 1:                    # log once per burst, not once per message
            sec(client, "input", "rate limit", "block",
                f"more than {RATE_MAX} messages in {RATE_WINDOW}s: further messages get no AI call")
        return False, []
    reasons = []
    if len(text) > MAX_TEXT:
        sec(client, "input", "length cap", "flag", f"{len(text)} chars cut to {MAX_TEXT}")
    low = text.lower()
    for a, b, label in INJECTION:
        if re.search(a, low, re.I) and (b is None or re.search(b, low, re.I)):
            if label not in reasons:
                reasons.append(label)
    if reasons:
        sec(client, "input", "prompt-injection detector", "flag",
            "matched: " + ", ".join(reasons) + " -> AI warned, message treated as data only")
    else:
        sec(client, "input", "input guard", "pass",
            ("rate ok · " if rate_limit else "") + "length ok · no injection pattern")
    return True, reasons


PROMPT_MARKERS = ["you are 3al tayer", "# actions", "# judgment", "# safety", "candidates:", "pending:",
                  "output: one json", "recent chat", "plan_system"]
SAFE_REFUSAL = "Ma fiye sa3dak bi hal shi. Shu baddak tetlob?"


def clean_ai_text(client, text):
    """Output guard for AI-written sentences: the AI may chat, but never leak codes, prices, links, emails,
    secrets or its own instructions."""
    original = str(text)
    low = original.lower()
    secrets = [v for k, v in os.environ.items() if any(w in k for w in ("KEY", "TOKEN", "SECRET")) and v and len(v) > 8]
    if any(m in low for m in PROMPT_MARKERS) or any(s in original for s in secrets) \
            or re.search(r"\b(sk-[A-Za-z0-9_-]{10,}|gsk_[A-Za-z0-9]{10,}|\d{8,10}:[A-Za-z0-9_-]{30,})", original):
        sec(client, "output", "secret/prompt leak", "block", "AI text contained instructions or a key: replaced entirely")
        return SAFE_REFUSAL
    out = re.sub(r"\bP\d{2,4}\b", "", original)                                      # internal product codes
    out = re.sub(r"(\$|usd|lbp|l\.l\.?|ل\.ل)\s*\d[\d,.]*|\d[\d,.]*\s*(\$|usd|lbp|dollar|lira|ليرة)", "[price]",
                 out, flags=re.I)                                                    # prices only come from code
    out = re.sub(r"https?://\S+|www\.\S+", "[link]", out)                            # no links
    out = re.sub(r"\S+@\S+\.\S+", "[email]", out)                                    # no emails
    out = out[:300]
    if out != original[:300]:
        sec(client, "output", "output guard", "block", "removed code/price/link from AI text before sending")
    return out


RATE_REPLY = "Shway shway 🙂 jarreb kamen d2i2a."