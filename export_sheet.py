"""Export every tab of the Google Sheet to CSV files so they can be committed to GitHub.

Run:  python export_sheet.py                 (uses the active sheet the agent is connected to)
      python export_sheet.py <sheet link or id>
Output: sheet_export/<TabName>.csv
"""
import csv, os, re, sys

import gspread

OUT = "sheet_export"
# Tabs with client phone numbers / chats. Skipped unless you pass --all.
PRIVATE = {"Clients", "ChatLog", "Escalations", "Sessions"}


def sheet_id(arg=None):
    if arg:
        m = re.search(r"/d/([A-Za-z0-9_-]+)", arg)
        return m.group(1) if m else arg
    try:
        from db import active_sheet_id
        return active_sheet_id()
    except Exception:
        sys.exit("Give the sheet link: python export_sheet.py <link>")


def main():
    args = [a for a in sys.argv[1:] if a != "--all"]
    include_all = "--all" in sys.argv
    sh = gspread.service_account(filename="creds.json").open_by_key(sheet_id(args[0] if args else None))
    os.makedirs(OUT, exist_ok=True)
    for ws in sh.worksheets():
        if ws.title in PRIVATE and not include_all:
            print(f"skip  {ws.title} (private, use --all to include)")
            continue
        rows = ws.get_all_values()
        name = re.sub(r"[^\w-]", "_", ws.title)
        with open(os.path.join(OUT, f"{name}.csv"), "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        print(f"saved {OUT}/{name}.csv  ({max(len(rows) - 1, 0)} rows)")


if __name__ == "__main__":
    main()