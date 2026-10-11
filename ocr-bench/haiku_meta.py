# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = []
# ///
"""Meta d'une page Haiku : uv run ocr-bench/haiku_meta.py ID SECONDES TOKENS.

Les sous-agents Haiku sont lancés depuis Claude Code ; la durée et les tokens
viennent de la notification de fin d'agent (tokens = total de l'agent, prompt
système de Claude Code compris, donc pas comparable à un appel API nu).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent


def main() -> None:
    item_id, seconds, tokens = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
    items = {i["id"]: i for i in json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))["items"]}
    item = items[item_id]
    out = BENCH / "runs" / "haiku" / f"{item_id}{item['ext']}"
    text = out.read_text(encoding="utf-8").strip() if out.exists() else ""
    meta = {
        "engine": "haiku",
        "model": "claude-haiku-5-5 (sous-agent Claude Code)",
        "seconds": round(seconds, 2),
        "agent_tokens": tokens,
        "chars": len(text),
        "status": "ok" if text else ("missing" if not out.exists() else "empty"),
    }
    (out.parent / f"{item_id}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{item_id}: {meta['status']} {meta['seconds']}s {meta['chars']} car.")


if __name__ == "__main__":
    main()
