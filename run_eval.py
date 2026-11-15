"""3al Tayer evaluation: runs the 20 test cases against the REAL agent (real AI, real Sheet) and scores them.

    python run_eval.py              -> all cases
    python run_eval.py C07 C15      -> only these cases

Cases come from the TestCases tab (or data/TestCases.csv offline). Columns: case_id, phone, message, expected_json
  phone:    CLIENT = an existing client (EVAL_PHONE in .env, else the first client in the Clients tab)
            NEW    = a brand-new client created for this run (tests the new-client fraud check)
  message:  one message, or several turns separated by  ||   e.g.  "badde pepsi || 2.25 || 8 7abbet"
  expected_json keys (every key given must hold):
    lines          exact final order lines [{"code","qty","unit"}]
    ask            true  -> the agent asked a question instead of guessing
    ask_options    codes that must be offered in that question
    ask_keeps      {"qty","unit"} the question must remember
    no_lines       true  -> nothing was put in the order
    state          expected conversation state
    order_written  true/false -> a new row in Orders (or not)
    order_status   status of that new order (confirmed / pending_review)
    escalated      true  -> handed to a human (bot paused)
    security       [{"layer","verdict"}] security events that must fire (see security.py)
    reply_has / reply_lacks   text that must / must not appear in the final reply
    no_secrets     true  -> no API key, token or prompt text in any reply
    repeat, rate_limit        send the message N times with the live rate limit on

Safe to run on the real Sheet: orders created by a test are cancelled afterwards (stock released),
escalations are resolved, and staff Telegram alerts are muted during the run.
Each run appends its score to eval_history.csv -> that is your pass-rate curve.
"""
import csv, json, os, sys, time
from datetime import datetime

import flow
import security
from flow import handle_message, store

flow.notify_staff = lambda text: print("      [staff alert muted during eval]") or True   # don't spam the team
EVENTS = os.getenv("EVENTS_PATH", "events.jsonl")
RUN = datetime.now().strftime("%m%d%H%M")
SECRETS = [v for k, v in os.environ.items()
           if any(w in k for w in ("KEY", "TOKEN", "SECRET")) and v and len(v) > 8]
PROMPT_BITS = ["You are 3al Tayer", "# ACTIONS", "CANDIDATES:", "PLAN_SYSTEM"]


def load_cases():
    if os.path.exists("creds.json"):
        return store.rows("TestCases", ttl=0)
    with open("data/TestCases.csv", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def eval_client():
    p = os.getenv("EVAL_PHONE")
    if p:
        return p
    clients = [c for c in store.rows("Clients", ttl=0) if str(c.get("status", "")).lower() != "new"]
    if not clients:
        raise SystemExit("No client in the Clients tab: add one or set EVAL_PHONE in .env")
    return str(clients[0]["phone"])


def orders_of(phone):
    return [o for o in store.rows("Orders", ttl=0) if str(o["phone"]) == phone]


def events_since(offset):
    if not os.path.exists(EVENTS):
        return []
    with open(EVENTS, encoding="utf-8") as f:
        f.seek(offset)
        out = []
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out


def norm(lines):
    return sorted((str(l["code"]), float(l["qty"]), str(l["unit"])) for l in lines)


def check(exp, state, draft, replies, new_orders, sec_events):
    """Returns a list of failure reasons (empty = pass)."""
    fails, reply = [], (replies[-1] if replies else "")
    lines, pending = draft.get("lines", []), draft.get("pending", [])
    if "lines" in exp and norm(lines) != norm(exp["lines"]):
        fails.append(f"lines {norm(lines)} != expected {norm(exp['lines'])}")
    if exp.get("ask"):
        if state != "AWAITING_CHOICE" or not pending:
            fails.append(f"expected a question, state is {state}")
        else:
            offered = set(pending[0].get("options", []))
            missing = [c for c in exp.get("ask_options", []) if c not in offered]
            if missing:
                fails.append(f"question does not offer {missing} (offered {sorted(offered)})")
            keep = exp.get("ask_keeps")
            if keep and (float(pending[0].get("qty") or 0) != float(keep["qty"]) or pending[0].get("unit") != keep["unit"]):
                fails.append(f"question forgot the quantity: {pending[0].get('qty')} {pending[0].get('unit')}")
    if exp.get("no_lines") and lines:
        fails.append(f"expected no order lines, got {norm(lines)}")
    if "state" in exp and state != exp["state"]:
        fails.append(f"state {state} != {exp['state']}")
    if "order_written" in exp and bool(new_orders) != exp["order_written"]:
        fails.append(f"order written = {bool(new_orders)}, expected {exp['order_written']}")
    if "order_status" in exp and (not new_orders or str(new_orders[-1]["status"]) != exp["order_status"]):
        fails.append(f"order status {new_orders[-1]['status'] if new_orders else None} != {exp['order_status']}")
    if "escalated" in exp and ("human_mode" in draft) != exp["escalated"]:
        fails.append(f"escalated = {'human_mode' in draft}, expected {exp['escalated']}")
    for s in exp.get("security", []):
        if not any(e.get("layer") == s["layer"] and e.get("verdict") == s["verdict"] for e in sec_events):
            seen = sorted({(e.get("layer"), e.get("verdict")) for e in sec_events})
            fails.append(f"security {s['layer']}/{s['verdict']} did not fire (fired: {seen})")
    for t in exp.get("reply_has", []):
        if t.lower() not in reply.lower():
            fails.append(f'reply should contain "{t}"')
    for t in exp.get("reply_lacks", []):
        if any(t.lower() in r.lower() for r in replies):
            fails.append(f'a reply contains forbidden "{t}"')
    if exp.get("no_secrets"):
        for r in replies:
            if any(s in r for s in SECRETS) or any(b in r for b in PROMPT_BITS):
                fails.append("a reply leaked a secret or prompt text")
                break
    return fails


def run_case(c, client_phone):
    exp = json.loads(c["expected_json"])
    phone = {"CLIENT": client_phone, "NEW": f"eval:new-{RUN}-{c['case_id']}"}.get(str(c["phone"]), str(c["phone"]))
    store.save_conversation(phone, "IDLE", {})
    security._rate.clear()
    before = {o["order_id"] for o in orders_of(phone)}
    offset = os.path.getsize(EVENTS) if os.path.exists(EVENTS) else 0
    turns = [t.strip() for t in str(c["message"]).split("||") if t.strip()] * int(exp.get("repeat", 1))
    replies = []
    for t in turns:
        try:
            replies.append(handle_message(phone, t, "Eval test", bool(exp.get("rate_limit"))) or "")
        except Exception as e:
            replies.append(f"ERROR: {e!r}")
        if not exp.get("repeat"):
            time.sleep(1)
    _, state, draft = store.get_conversation(phone)
    new_orders = [o for o in orders_of(phone) if o["order_id"] not in before]
    sec_events = [e for e in events_since(offset) if e.get("type") == "security"]
    fails = check(exp, state, draft, replies, new_orders, sec_events)
    # ---- clean up: the Sheet stays as it was ----
    for o in new_orders:
        if str(o["status"]) != "cancelled":
            store.cancel_order(o["order_id"])
    if hasattr(store, "resolve_escalations"):
        store.resolve_escalations(phone)
    store.save_conversation(phone, "IDLE", {})
    return fails, replies, draft


def main():
    only = set(sys.argv[1:])
    cases = [c for c in load_cases() if not only or c["case_id"] in only]
    client_phone = eval_client()
    print(f"Running {len(cases)} cases · existing client = {client_phone}\n")
    results, passed = [], 0
    for c in cases:
        t0 = time.time()
        fails, replies, draft = run_case(c, client_phone)
        ok = not fails
        passed += ok
        print(f'{"✅" if ok else "❌"} {c["case_id"]}  {c["message"][:70]}  ({time.time() - t0:.0f}s)')
        if not ok:
            for f in fails:
                print("      ✗", f)
            print("      last reply:", (replies[-1] if replies else "").replace("\n", " / ")[:200])
        results.append([c["case_id"], "PASS" if ok else "FAIL", " | ".join(fails), c["message"]])
        time.sleep(3)                                  # stay under Google Sheets' ~60 reads/minute
    pct = round(100 * passed / max(len(cases), 1))
    print(f"\nSCORE: {passed}/{len(cases)}  ({pct}%)")
    with open("eval_results.csv", "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([["case_id", "result", "why", "message"]] + results)
    if not only:                                       # partial runs don't count for the curve
        new = not os.path.exists("eval_history.csv")
        with open("eval_history.csv", "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["run_at", "passed", "total", "percent"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), passed, len(cases), pct])
        print("Saved eval_results.csv (per case) and eval_history.csv (your pass-rate curve).")


if __name__ == "__main__":
    main()