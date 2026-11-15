import csv, json, os, time
import streamlit as st

st.set_page_config(page_title="3al Tayer · Agent Console", layout="wide")
PATH = os.getenv("EVENTS_PATH", "events.jsonl")

STATE_STYLE = {
    "COLLECTING": "🟡",
    "AWAITING_CONFIRM": "🟠",
    "CONFIRMED": "🟢",
    "ESCALATED": "🔴",
    "IDLE": "⚪",
    "CANCELLED": "⚫",
}

# security layers: where each check sits, and what it does
LAYERS = {
    "input":  ("🛡️ Input guard", "Rate limit, length cap, prompt-injection detector. Runs before any AI call."),
    "plan":   ("🛡️ Plan validator", "Only known actions and real product codes; invalid quantities rejected."),
    "data":   ("🛡️ Data guard", "Prices and stock only from the database; each client sees only their own data."),
    "lock":   ("🔒 Write lock", "Only code writes orders, and only after the client's explicit OK."),
    "output": ("🛡️ Output guard", "No product codes, prices, links or emails in AI-written text."),
    "access": ("🔑 Staff access", "/done, /approve, /audit only from the registered staff chat."),
}
VERDICT = {"pass": "✅", "flag": "⚠️", "block": "⛔"}


def load():
    if not os.path.exists(PATH):
        return []
    out = []
    with open(PATH, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # half-written line, skip it
    return out


# ---- sidebar ----
with st.sidebar:
    st.header("Controls")
    if st.button("Reset demo (clear events)"):
        open(PATH, "w").close()
        st.rerun()

events = load()
st.title("3al Tayer · live order-desk agent")

# ---- top metrics ----
sec_events = [e for e in events if e.get("type") == "security"]
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Orders written",
          sum(e.get("type") == "db_write" and e.get("table") == "Orders" for e in events))
m2.metric("AI calls", sum(e.get("type") == "plan" for e in events))
m3.metric("AI cost",
          f'${sum(e.get("cost", 0) for e in events if e.get("type") == "plan"):.4f}')
m4.metric("Handed to a human", sum(e.get("type") == "escalation" for e in events))
m5.metric("Security blocks", sum(e.get("verdict") == "block" for e in sec_events),
          delta=f'{sum(e.get("verdict") == "flag" for e in sec_events)} flagged', delta_color="off")

# ---- live pipeline diagram ----
NODE_FOR = {
    "message_in": "client", "plan": "llm", "tool_call": "tools",
    "tool_result": "tools", "state": "order", "db_write": "db",
    "escalation": "human", "message_out": "reply",
}
GUARD_FOR = {"input": "g_in", "plan": "g_plan", "data": "tools", "lock": "g_lock",
             "output": "g_out", "access": "human"}
NODE_LABEL = {
    "client": "Client\\n(Telegram)", "g_in": "🛡 input", "llm": "AI plans",
    "g_plan": "🛡 validate", "tools": "Tools\\n(prices, stock)", "order": "Live order",
    "g_lock": "🔒 lock", "db": "Database", "g_out": "🛡 output",
    "human": "Human\\nhandoff", "reply": "Reply\\nto client",
}
GUARDS = {"g_in", "g_plan", "g_lock", "g_out"}
EDGES = [("client", "g_in"), ("g_in", "llm"), ("llm", "g_plan"), ("g_plan", "tools"), ("tools", "order"),
         ("order", "g_lock"), ("g_lock", "db"), ("db", "g_out"), ("order", "g_out"), ("g_out", "reply"),
         ("llm", "human")]


def flow_dot(events):
    last_in = max((i for i, e in enumerate(events) if e.get("type") == "message_in"), default=0)
    recent = events[last_in:]
    visited = {NODE_FOR[e["type"]] for e in recent if e.get("type") in NODE_FOR}
    flagged = {GUARD_FOR.get(e.get("layer")) for e in recent if e.get("type") == "security" and e.get("verdict") == "flag"}
    blocked = {GUARD_FOR.get(e.get("layer")) for e in recent if e.get("type") == "security" and e.get("verdict") == "block"}
    active = NODE_FOR.get(events[-1].get("type")) if events else None
    lines = ['digraph G {', 'rankdir=LR; bgcolor="transparent";',
             'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=14, '
             'margin="0.25,0.15", color="#555555", fillcolor="#262730", fontcolor="#cccccc"];',
             'edge [color="#888888"];']
    small = 'fontsize=9, margin="0.06,0.03", height=0.25, width=0.4'      # guards stay small and quiet
    for key, label in NODE_LABEL.items():
        if key in blocked:
            style = 'fillcolor="#8b1a1a", fontcolor="white", color="#ff4b4b", penwidth=2'
            label += " · BLOCKED"
            style += (", " + small) if key in GUARDS else ""
        elif key in flagged:
            style = 'fillcolor="#8a6d00", fontcolor="white", color="#ffbf00", penwidth=2'
            label += " · FLAGGED"
            style += (", " + small) if key in GUARDS else ""
        elif key in GUARDS:              # passed or idle: a thin gray tag, never competing with the green steps
            style = ('style="rounded,dashed", fillcolor="transparent", fontcolor="#8a8f98", color="#4a4f58", '
                     'penwidth=1, ' + small)
        elif key == active:
            style = 'fillcolor="#ff4b4b", fontcolor="white", color="#ff4b4b", penwidth=3'
        elif key in visited:
            style = 'fillcolor="#1f7a4d", fontcolor="white", color="#1f7a4d"'
        else:
            style = ""
        lines.append(f'{key} [label="{label}", {style}];')
    lines += [f"{a} -> {b};" for a, b in EDGES]
    lines.append("}")
    return "\n".join(lines)


st.graphviz_chart(flow_dot(events), use_container_width=True)

chat, agent, order = st.columns([1.1, 1.4, 1])

# ---- left: the conversation ----
def as_lines(text):
    """Keep the bot's line breaks exactly (markdown would merge numbered options into one list item)."""
    return "  \n".join(str(text).replace("*", "\\*").split("\n"))


with chat:
    st.subheader("Client chat")
    st.caption("Newest first")
    msgs = [e for e in events if e.get("type") in ("message_in", "message_out")]
    for e in reversed(msgs[-16:]):                     # newest on top: the live exchange is always in view
        if e["type"] == "message_in":
            st.chat_message("user").markdown(f'**{e.get("client", "")}**: {as_lines(e.get("text", ""))}')
        else:
            st.chat_message("assistant").markdown(as_lines(e.get("text", "")))

# ---- middle: what the agent is doing (newest first) ----
with agent:
    st.subheader("What the agent is doing")
    for e in reversed(events[-25:]):
        t = e.get("type")
        if t == "plan":
            ops = ", ".join(a.get("op", "") for a in e.get("actions", []))
            st.info(f'Decided: {ops} · {e.get("model", "")} · '
                    f'{e.get("tokens_in", 0)}→{e.get("tokens_out", 0)} tokens · '
                    f'${e.get("cost", 0):.5f}')
        elif t == "tool_call":
            st.write(f'🔧 `{e.get("tool", "?")}` {e.get("args", {})}')
        elif t == "tool_result":
            (st.success if e.get("ok") else st.warning)(e.get("summary", ""))
        elif t == "db_write":
            st.success(f'🗄️ Database · {e.get("table", "")}: {e.get("summary", "")}')
        elif t == "escalation":
            st.error(f'🙋 Handed to a human: {e.get("summary", "")}')
        elif t == "security" and e.get("verdict") != "pass":
            (st.error if e.get("verdict") == "block" else st.warning)(
                f'{VERDICT.get(e.get("verdict"))} Security · {e.get("check", "")}: {e.get("detail", "")}')

# ---- right: the live order ----
with order:
    st.subheader("Live order")
    last = next((e for e in reversed(events) if e.get("type") == "state"), None)
    if last:
        s = last.get("state", "")
        st.caption(f'{STATE_STYLE.get(s, "⚪")} State: {s} · {last.get("client", "")}')
        if last.get("lines"):
            st.table([{"Item": l.get("name", ""),
                       "Qty": f'{l.get("qty", "")} {l.get("unit", "")}',
                       "Total": f'${l.get("line_total_usd", 0):.2f}'}
                      for l in last["lines"]])
            st.markdown(f'### Total: ${last.get("total_usd", 0):.2f}')
    else:
        st.caption("Waiting for an order...")

# ---- security: where every check sits, and what it caught ----
st.divider()
st.subheader("🛡️ Security layers")
table = []
for key, (name, what) in LAYERS.items():
    ev = [e for e in sec_events if e.get("layer") == key]
    table.append({"Layer": name, "What it checks": what,
                  "✅": sum(e.get("verdict") == "pass" for e in ev),
                  "⚠️": sum(e.get("verdict") == "flag" for e in ev),
                  "⛔": sum(e.get("verdict") == "block" for e in ev)})
st.dataframe(table, hide_index=True, width="content")
st.caption("🔐 API keys live only in .env on the server: never sent to the AI or to clients. "
           "Blocks and flags also appear live in \"What the agent is doing\".")

# ---- test results (written by run_eval.py) ----
st.divider()
st.subheader("✅ Test results")
if os.path.exists("eval_results.csv"):
    with open("eval_results.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    passed = sum(r["result"] == "PASS" for r in rows)
    c1, c2 = st.columns(2)
    c1.metric("Last run", f"{passed} / {len(rows)}")
    c2.metric("Pass rate", f"{round(100 * passed / max(len(rows), 1))}%")
    st.dataframe([{"Case": r["case_id"],
                   "Result": "✅ PASS" if r["result"] == "PASS" else "❌ FAIL",
                   "Message": r["message"],
                   "Why it failed": r["why"]} for r in rows],
                 hide_index=True, width="stretch")
    if os.path.exists("eval_history.csv"):
        with open("eval_history.csv", encoding="utf-8") as f:
            hist = list(csv.DictReader(f))
        if hist:
            st.caption("Pass-rate curve (one point per full run)")
            st.line_chart({"pass rate (%)": [int(h["percent"]) for h in hist]})
else:
    st.caption("No test run yet: python run_eval.py")

time.sleep(1)
st.rerun()