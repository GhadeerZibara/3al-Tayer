import re
from rapidfuzz import fuzz

STOP = {"bade", "badde", "baddi", "bde", "bdi", "w", "wa", "and", "et", "svp", "please", "men", "mn",
        "l", "el", "al", "zid", "bala", "bas", "kamen", "kaman", "fi", "3tine", "a3tine", "jibli",
        "ma3", "with", "avec", "kartouneh", "kartoneh", "carton", "cartons", "7abbe", "piece", "pieces",
        "بدي", "و", "من", "كمان", "زيد", "بلا", "كرتونة"}
WORD = r"[a-z0-9\u0600-\u06FF.']+"


def tokens(text):
    out = []
    for t in re.findall(WORD, text.lower()):
        if t in STOP or len(t) < 2:
            continue
        if re.fullmatch(r"\d+", t) and int(t) < 100:      # a quantity (5, 10, 24), not a size
            continue
        out.append(t.strip("."))
    return out


def candidates(text, products, aliases, extra_codes=(), k=12):
    """Shortlist the products a message most likely refers to. No AI involved."""
    toks = tokens(text)
    scores = {}
    for p in products:
        words = set(re.findall(WORD, f'{p["name"]} {p["brand"]} {p["category"]} '
                                     f'{p["size"]} {p["keywords"]}'.lower()))
        s = 0.0
        for t in toks:
            best = max((fuzz.ratio(t, w) for w in words), default=0)
            s += 1 if best >= 85 else 0.5 if best >= 75 else 0
        if s:
            scores[p["code"]] = s
    for t in toks:                                   # this client's own words win
        if t in aliases:
            scores[aliases[t]] = scores.get(aliases[t], 0) + 10
    ranked = [c for c, _ in sorted(scores.items(), key=lambda x: -x[1])][:k]
    return ranked + [c for c in extra_codes if c not in ranked]