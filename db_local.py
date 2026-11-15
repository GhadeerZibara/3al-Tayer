import csv, json, os
from datetime import datetime

D = "data"
HEADERS = {
    "Orders": ["order_id", "phone", "date", "total_usd", "total_lbp", "currency", "status"],
    "OrderLines": ["order_id", "code", "name", "qty", "unit", "unit_price_usd", "line_total_usd"],
    "OrderHistory": ["order_id", "phone", "date", "code", "qty", "unit"],
    "Conversations": ["phone", "state", "draft_json", "updated_at"],
    "Stock": ["code", "on_hand", "reserved"],
    "Backorders": ["phone", "code", "qty", "date", "status"],
    "Escalations": ["date", "phone", "shop_name", "summary", "status"],
    "Clients": ["phone", "shop_name", "area", "currency", "tier", "status"],
    "PurchaseOrders": ["po_id", "audit_id", "date", "code", "name", "cartons", "pieces", "code_suggested",
                       "reason", "status"],
    "AuditReports": ["audit_id", "date", "brief", "anomalies", "messages_json", "cost_usd", "status", "decided_by"],
}


def rows(name, ttl=None):
    path = f"{D}/{name}.csv"
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_all(name, data):
    with open(f"{D}/{name}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS[name], extrasaction="ignore")
        w.writeheader()
        w.writerows(data)


def _append(name, list_of_lists):
    path = f"{D}/{name}.csv"
    new = not os.path.exists(path)
    if not new:                                   # make sure we start on a new line
        with open(path, "rb") as f:
            f.seek(0, 2)
            if f.tell() and (f.seek(-1, 2) or f.read(1)) != b"\n":
                with open(path, "a", encoding="utf-8") as g:
                    g.write("\n")
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HEADERS[name])
        w.writerows(list_of_lists)


def append_row(name, values):
    _append(name, [values])


def get_client(phone):
    return next((r for r in rows("Clients") if r["phone"] == phone), None)


def get_products():
    return rows("Products")


def get_stock():
    return {r["code"]: r for r in rows("Stock")}


def get_aliases(phone):
    return {r["word"].lower().strip(): r["code"] for r in rows("Aliases") if r["phone"] == phone}


def get_history_codes(phone):
    return list({r["code"] for r in rows("OrderHistory") if r["phone"] == phone})


def get_setting(key, default=None):
    return next((r["value"] for r in rows("Settings") if r["key"] == key), default)


def get_conversation(phone):
    for r in rows("Conversations"):
        if r["phone"] == phone:
            return None, (r["state"] or "IDLE"), json.loads(r["draft_json"] or "{}")
    return None, "IDLE", {}


def save_conversation(phone, state, draft):
    data = [r for r in rows("Conversations") if r["phone"] != phone]
    data.append({"phone": phone, "state": state, "draft_json": json.dumps(draft, ensure_ascii=False),
                 "updated_at": datetime.now().isoformat(timespec="seconds")})
    _write_all("Conversations", data)


def write_order(phone, draft, client, status="confirmed"):
    order_id = "ORD-" + datetime.now().strftime("%m%d%H%M%S")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    _append("Orders", [[order_id, phone, now, draft["total_usd"], draft["total_lbp"],
                        client["currency"], status]])
    _append("OrderLines", [[order_id, l["code"], l["name"], l["qty"], l["unit"],
                            l["unit_price_usd"], l["line_total_usd"]] for l in draft["lines"]])
    _append("OrderHistory", [[order_id, phone, now, l["code"], l["qty"], l["unit"]]
                             for l in draft["lines"]])
    stock = rows("Stock")
    for l in draft["lines"]:
        for r in stock:
            if r["code"] == l["code"]:
                r["reserved"] = int(r["reserved"] or 0) + int(l["pieces"])
    _write_all("Stock", stock)
    return order_id

# ================= cancelling a confirmed order =================
def last_order_row(phone):
    mine = [r for r in rows("Orders") if r["phone"] == phone]
    return mine[-1] if mine else None


def order_lines(order_id):
    return [r for r in rows("OrderLines") if r["order_id"] == order_id]


def cancel_order(order_id):
    orders = rows("Orders")
    for r in orders:
        if r["order_id"] == order_id:
            r["status"] = "cancelled"
    _write_all("Orders", orders)
    lines = order_lines(order_id)
    upc = {p["code"]: int(p.get("units_per_carton") or 1) for p in rows("Products")}
    stock = rows("Stock")
    for l in lines:
        pieces = int(float(l["qty"])) * (upc.get(l["code"], 1) if l["unit"] == "carton" else 1)
        for r in stock:
            if r["code"] == l["code"]:
                r["reserved"] = max(int(r["reserved"] or 0) - pieces, 0)
    _write_all("Stock", stock)
    _write_all("OrderHistory", [r for r in rows("OrderHistory") if r["order_id"] != order_id])
    return lines


def resolve_escalations(phone):
    data = rows("Escalations")
    for r in data:
        if r["phone"] == phone and r["status"] == "open":
            r["status"] = "resolved"
    _write_all("Escalations", data)


# ================= auditor helpers =================
def append_rows(name, list_of_lists):
    _append(name, list_of_lists)


def ensure_tab(name, headers):
    HEADERS.setdefault(name, headers)


def set_status(name, key_col, key, status, **also):
    data = rows(name)
    for r in data:
        if r[key_col] == key:
            r["status"] = status
            r.update({k: v for k, v in also.items() if k in r})
    _write_all(name, data)


def set_status_where(name, match, status):
    data = rows(name)
    for r in data:
        if all(str(r.get(k)) == str(v) for k, v in match.items()) and r.get("status") in ("waiting", "open"):
            r["status"] = status
    _write_all(name, data)