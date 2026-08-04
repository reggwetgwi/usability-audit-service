# -*- coding: utf-8 -*-
"""Показывает последние действия claude-сессии из jsonl-журнала (запускается на сервере)."""
import glob
import json
import os
import sys

files = glob.glob("/home/audit/.claude/projects/*/*.jsonl")
path = max(files, key=os.path.getmtime)
print("журнал:", path, os.path.getsize(path) // 1024 // 1024, "MB")

events = []
with open(path, encoding="utf-8", errors="replace") as f:
    for line in f:
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message", {})
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            t = b.get("type")
            if t == "tool_use":
                events.append(("TOOL " + str(b.get("name")), str(b.get("input"))[:160]))
            elif t == "text" and b.get("text", "").strip():
                events.append(("TEXT", b["text"][:160]))

print("всего событий:", len(events))
for kind, info in events[-15:]:
    print(f"{kind}: {info}")
