#!/usr/bin/env python3
"""Throwaway spike probe for autopilot continue. Not product code.

observe: sample every agent pane's tail every 5s, print diffs, for --seconds.
press-once: send one `tab` to the given pane (or the first agent pane) and
print the runtime's reply JSON. See docs/spikes/ for the report.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cc_autopilot_notify import pebrel_runtime  # noqa: E402

APPDATA = Path("/mnt/c/Users/Anonymous/AppData/Roaming")


def panes():
    out = []
    for a in pebrel_runtime.agents(APPDATA):
        info = a.get("agent") or {}
        out.append((int(a.get("window_id", 0)), int(a.get("pane_id", 0)),
                    info.get("session_id")))
    return out


def observe(seconds: int) -> None:
    seen: dict[int, str] = {}
    end = time.time() + seconds
    while time.time() < end:
        for _, pane_id, sid in panes():
            d = pebrel_runtime.dialog(APPDATA, pane_id)
            if d["text"] != seen.get(pane_id):
                seen[pane_id] = d["text"]
                stamp = time.strftime("%H:%M:%S")
                print(f"--- {stamp} pane={pane_id} sid={sid} state={d['state']} ---",
                      flush=True)
                print(d["text"][-900:], flush=True)
        time.sleep(5)


def press_once(pane_id: int, key: str) -> None:
    print(json.dumps(pebrel_runtime.press(APPDATA, pane_id, key, 1)), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["observe", "press-once", "list"])
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--pane", type=int, default=None)
    ap.add_argument("--key", default="tab")
    args = ap.parse_args()
    if args.command == "list":
        for w, p, s in panes():
            print(f"window={w} pane={p} session={s}")
    elif args.command == "observe":
        observe(args.seconds)
    else:
        pane = args.pane if args.pane is not None else panes()[0][1]
        press_once(pane, args.key)


if __name__ == "__main__":
    main()
