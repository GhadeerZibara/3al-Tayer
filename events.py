import json, time, os, threading

PATH = os.getenv("EVENTS_PATH", "events.jsonl")
_lock = threading.Lock()


def emit(type, **data):
    """Record one step of the agent. The UI reads these lines live."""
    e = {"ts": time.time(), "type": type, **data}
    with _lock, open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")