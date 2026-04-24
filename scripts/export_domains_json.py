#!/usr/bin/env python3
"""
Export the DOMAINS dict from mwmbl/hn_top_domains_filtered.py to JSON
so it can be embedded in the Rust crate at compile time.

Usage:
    python scripts/export_domains_json.py
"""
import json
import sys
from pathlib import Path

# Add repo root to path
repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

from mwmbl.hn_top_domains_filtered import DOMAINS

output_path = repo_root / "mwmbl_rank" / "data" / "hn_top_domains_filtered.json"
output_path.parent.mkdir(parents=True, exist_ok=True)

with open(output_path, "w", encoding="utf-8") as f:
    json.dump(DOMAINS, f, ensure_ascii=False, indent=None, separators=(",", ":"))

print(f"Exported {len(DOMAINS)} domains to {output_path}")
