"""Onboarding a distributor's Google Sheet.

The distributor OWNS the sheet with their catalog, prices, stock and clients, and shares it with the agent's
account as Editor. The agent adds only its own working tabs (Orders, Escalations, ...), never touches existing
data, checks that the catalog is usable, and only switches to it after a human /approve in the staff chat.

Staff chat:  /connect                    -> instructions for the owner
             /connect <sheet link>       -> agent checks access, builds missing tabs, reports
             reply /approve to the report -> agent starts using that sheet
"""
import json, os, re

from events import emit
from security import sec

PENDING_FILE = "pending_sheet.txt"

# every tab the agent needs, with its header row (same as the original 3al Tayer sheet)
TABS = {
    "Products": ["code", "name", "brand", "category", "size", "unit", "units_per_carton", "price_usd_A",
                 "price_usd_B", "keywords"],
    "Stock": ["code", "on_hand", "reserved"],
    "Clients": ["phone", "shop_name", "area", "currency", "tier", "status"],
    "Aliases": ["phone", "word", "code"],
    "OrderHistory": ["order_id", "phone", "date", "code", "qty", "unit"],
    "Orders": ["order_id", "phone", "date", "total_usd", "total_lbp", "currency", "status"],
    "OrderLines": ["order_id", "code", "name", "qty", "unit", "unit_price_usd", "line_total_usd"],
    "Backorders": ["phone", "code", "qty", "date", "status"],
    "Escalations": ["date", "phone", "shop_name", "summary", "status"],
    "Conversations": ["phone", "state", "draft_json", "updated_at"],
    "Settings": ["key", "value"],
    "TestCases": ["case_id", "phone", "message", "expected_json"],
    "PurchaseOrders": ["po_id", "audit_id", "date", "code", "name", "cartons", "pieces", "code_suggested",
                       "reason", "status"],
    "AuditReports": ["audit_id", "date", "brief", "anomalies", "messages_json", "cost_usd", "status", "decided_by"],
}
DEFAULT_SETTINGS = [["usd_lbp_rate", 89500], ["lbp_rounding", 1000], ["flag_multiplier", 3],
                    ["new_client_max_pieces", 120], ["flag_total_usd", 500], ["cancel_window_minutes", 60],
                    ["human_timeout_minutes", 30]]


def agent_email():
    try:
        with open("creds.json", encoding="utf-8") as f:
            return json.load(f)["client_email"]
    except Exception:
        return "(the client_email in creds.json)"


def instructions():
    return ("\U0001f4c4 To connect a distributor's sheet:\n"
            "1. The owner opens the Google Sheet that holds their catalog, prices, stock and clients.\n"
            f"2. Share \u2192 add {agent_email()} as Editor.\n"
            "3. Send the link here: /connect <link>\n"
            "The agent reads their data, adds its own order-log tabs (Orders, Escalations...) without touching "
            "anything that exists, checks the catalog, then waits for your /approve.")


def sheet_id_from(text):
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", text or "") or re.fullmatch(r"\s*([A-Za-z0-9_-]{30,})\s*", text or "")
    return m.group(1) if m else None


def prepare(gc, link, who):
    """Open the sheet, build missing tabs, report. Returns the text for the staff chat."""
    sid = sheet_id_from(link)
    if not sid:
        return "That doesn't look like a Google Sheet link.\n\n" + instructions()
    try:
        book = gc.open_by_key(sid)
    except Exception:
        sec(who, "access", "sheet access", "block", "sheet not shared with the agent: nothing opened")
        return (f"❌ I can't open that sheet. The owner must share it with {agent_email()} as Editor, "
                f"then send /connect again.")
    existing = {ws.title: ws for ws in book.worksheets()}
    created, problems = [], []
    for name, headers in TABS.items():
        ws = existing.get(name)
        if ws is None:
            ws = book.add_worksheet(title=name, rows=500, cols=len(headers))
            ws.append_row(headers)
            if name == "Settings":
                ws.append_rows(DEFAULT_SETTINGS)
            created.append(name)
            continue
        first = [h.strip() for h in ws.row_values(1)]
        if not any(first):                                    # empty tab: give it headers
            ws.append_row(headers)
            created.append(name + " (headers)")
        else:
            missing = [h for h in headers if h not in first]
            if missing:
                problems.append(f"{name}: missing columns {missing}")
    if "Sheet1" in existing and len(existing) == 1 and created:
        try:
            book.del_worksheet(existing["Sheet1"])            # a brand-new sheet's empty default tab
        except Exception:
            pass
    n_products = max(len(book.worksheet("Products").get_all_values()) - 1, 0)
    n_stock = max(len(book.worksheet("Stock").get_all_values()) - 1, 0)
    ready = n_products > 0 and not problems
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        f.write(sid)
    emit("db_write", client="Onboarding", table="Sheet",
         summary=f"{book.title}: {len(created)} tab(s) built, {n_products} products, waiting for approval")
    lines = [f"\U0001f4c4 SHEET CHECK: {book.title}",
             f"Tabs built: {', '.join(created) if created else 'none needed (all present)'}",
             f"Products: {n_products} rows · Stock: {n_stock} rows"]
    lines += [f"⚠️ {p}" for p in problems]
    if not n_products:
        lines.append("⚠️ The catalog is empty: the owner must fill Products (and Stock) before the agent can use it.")
    lines.append(f"Sheet ID: {sid}")
    lines.append("Reply /approve to switch the agent to this sheet." if ready
                 else "Fix the points above, then send /connect again.")
    return "\n".join(lines)


def approve(store, sid, who):
    """THE LOCK: the agent only switches databases after a human approves, and only to the checked sheet."""
    pending = open(PENDING_FILE, encoding="utf-8").read().strip() if os.path.exists(PENDING_FILE) else ""
    if not sid or sid != pending:
        return "This isn't the sheet that was just checked. Send /connect <link> again."
    if not hasattr(store, "switch_sheet"):
        return "Offline mode (no creds.json): can't switch sheets."
    store.switch_sheet(sid)
    os.remove(PENDING_FILE)
    sec(who, "lock", "sheet switch", "pass", f"agent switched to the distributor sheet, approved by {who}")
    emit("db_write", client="Onboarding", table="Sheet", summary=f"agent now uses sheet {sid[:12]}… (approved by {who})")
    return f"✅ Approved by {who}. The agent now reads and writes the distributor's sheet."