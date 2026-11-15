import os, json, time
from datetime import datetime
import gspread
from dotenv import load_dotenv

load_dotenv()
try:                                       # retry automatically when Google says "too many requests" (429)
    from gspread.http_client import BackOffHTTPClient
    gc = gspread.service_account(filename="creds.json", http_client=BackOffHTTPClient)
except (ImportError, TypeError):
    gc = gspread.service_account(filename="creds.json")
ACTIVE_FILE = "active_sheet.txt"          # set by /connect + /approve; overrides SHEET_ID in .env


def active_sheet_id():
    if os.path.exists(ACTIVE_FILE):
        v = open(ACTIVE_FILE, encoding="utf-8").read().strip()
        if v:
            return v
    return os.getenv("SHEET_ID")


sh = gc.open_by_key(active_sheet_id())
_tabs, _cache = {}, {}


def switch_sheet(sheet_id):
    """Point the agent at another distributor sheet, live (no restart)."""
    global sh
    new = gc.open_by_key(sheet_id)
    sh = new
    _tabs.clear()
    _cache.clear()
    with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(sheet_id)


def tab(name):
    if name not in _tabs:
        _tabs[name] = sh.worksheet(name)
    return _tabs[name]


def rows(name, ttl=60):
    """Whole tab as a list of dicts. Cached so we don't hit Google's limit (about 60 reads/min)."""
    now = time.time()
    if ttl and name in _cache and now - _cache[name][0] < ttl:
        return _cache[name][1]
    data = tab(name).get_all_records()
    _cache[name] = (now, data)
    return data


def clear_cache(*names):
    for n in names:
        _cache.pop(n, None)


def append_row(name, values):
    tab(name).append_row(values)
    clear_cache(name)


def get_client(phone):
    return next((r for r in rows("Clients") if str(r["phone"]) == phone), None)


def get_products():
    return rows("Products")


def get_stock():
    return {r["code"]: r for r in rows("Stock", ttl=0)}


def get_aliases(phone):
    return {str(r["word"]).lower().strip(): r["code"] for r in rows("Aliases") if str(r["phone"]) == phone}


def get_history_codes(phone):
    return list({r["code"] for r in rows("OrderHistory") if str(r["phone"]) == phone})


def get_setting(key, default=None):
    return next((r["value"] for r in rows("Settings") if r["key"] == key), default)


def get_conversation(phone):
    for i, r in enumerate(rows("Conversations", ttl=0), start=2):
        if str(r["phone"]) == phone:
            return i, (r["state"] or "IDLE"), json.loads(r["draft_json"] or "{}")
    return None, "IDLE", {}


def save_conversation(phone, state, draft):
    row, _, _ = get_conversation(phone)
    values = [phone, state, json.dumps(draft, ensure_ascii=False),
              datetime.now().isoformat(timespec="seconds")]
    if row:
        tab("Conversations").update(range_name=f"A{row}:D{row}", values=[values])
    else:
        tab("Conversations").append_row(values)


def write_order(phone, draft, client, status="confirmed"):
    order_id = "ORD-" + datetime.now().strftime("%m%d%H%M%S")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tab("Orders").append_row([order_id, phone, now, draft["total_usd"], draft["total_lbp"],
                              client["currency"], status])
    tab("OrderLines").append_rows([[order_id, l["code"], l["name"], l["qty"], l["unit"],
                                    l["unit_price_usd"], l["line_total_usd"]] for l in draft["lines"]])
    tab("OrderHistory").append_rows([[order_id, phone, now, l["code"], l["qty"], l["unit"]]
                                     for l in draft["lines"]])
    stock_rows = rows("Stock", ttl=0)
    for l in draft["lines"]:
        for i, r in enumerate(stock_rows, start=2):
            if r["code"] == l["code"]:
                new_reserved = int(r["reserved"] or 0) + int(l["pieces"])
                tab("Stock").update_cell(i, 3, new_reserved)      # column 3 = reserved
                r["reserved"] = new_reserved
    clear_cache("OrderHistory", "Orders")
    return order_id

# ================= cancelling a confirmed order =================
def last_order_row(phone):
    """The client's most recent row in Orders (any status), or None."""
    mine = [r for r in rows("Orders", ttl=0) if str(r["phone"]) == phone]
    return mine[-1] if mine else None


def order_lines(order_id):
    return [r for r in rows("OrderLines", ttl=0) if str(r["order_id"]) == order_id]


def cancel_order(order_id):
    """Orders.status -> cancelled, release the reserved stock, drop the order from OrderHistory
    (so 'l 3adeh' and the quantity checks ignore it). Returns the order lines."""
    # 1. status
    orders = tab("Orders")
    status_col = orders.row_values(1).index("status") + 1
    for i, r in enumerate(rows("Orders", ttl=0), start=2):
        if str(r["order_id"]) == order_id:
            orders.update_cell(i, status_col, "cancelled")
    # 2. give the reserved pieces back
    lines = order_lines(order_id)
    upc = {p["code"]: int(p.get("units_per_carton") or 1) for p in rows("Products")}
    stock_rows = rows("Stock", ttl=0)
    for l in lines:
        pieces = int(float(l["qty"])) * (upc.get(l["code"], 1) if l["unit"] == "carton" else 1)
        for i, r in enumerate(stock_rows, start=2):
            if r["code"] == l["code"]:
                new_reserved = max(int(r["reserved"] or 0) - pieces, 0)
                tab("Stock").update_cell(i, 3, new_reserved)      # column 3 = reserved
                r["reserved"] = new_reserved
    # 3. remove it from OrderHistory (its rows were appended together, so they are contiguous)
    hist = tab("OrderHistory").get_all_values()
    idx = [i for i, row in enumerate(hist[1:], start=2) if row and row[0] == order_id]
    if idx:
        tab("OrderHistory").delete_rows(min(idx), max(idx))
    clear_cache("Orders", "OrderLines", "OrderHistory", "Stock")
    return lines


def resolve_escalations(phone):
    """Staff finished with this client: their open Escalations rows -> resolved."""
    esc = tab("Escalations")
    status_col = esc.row_values(1).index("status") + 1
    for i, r in enumerate(rows("Escalations", ttl=0), start=2):
        if str(r["phone"]) == phone and str(r["status"]).lower() == "open":
            esc.update_cell(i, status_col, "resolved")
    clear_cache("Escalations")


# ================= auditor helpers =================
def append_rows(name, list_of_lists):
    if list_of_lists:
        tab(name).append_rows(list_of_lists)
        clear_cache(name)


def ensure_tab(name, headers):
    """Create a tab with its header row if it doesn't exist yet."""
    try:
        tab(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=500, cols=len(headers))
        ws.append_row(headers)
        _tabs[name] = ws


def set_status(name, key_col, key, status, **also):
    """Set status (and optional other columns) on every row whose key_col == key."""
    ws = tab(name)
    hdr = ws.row_values(1)
    for i, r in enumerate(rows(name, ttl=0), start=2):
        if str(r[key_col]) == key:
            ws.update_cell(i, hdr.index("status") + 1, status)
            for col, val in also.items():
                if col in hdr:
                    ws.update_cell(i, hdr.index(col) + 1, val)
    clear_cache(name)


def set_status_where(name, match, status):
    """status = ... on every row whose columns equal all of `match` (e.g. phone + code)."""
    ws = tab(name)
    col = ws.row_values(1).index("status") + 1
    for i, r in enumerate(rows(name, ttl=0), start=2):
        if all(str(r.get(k)) == str(v) for k, v in match.items()) and str(r.get("status")) in ("waiting", "open"):
            ws.update_cell(i, col, status)
    clear_cache(name)