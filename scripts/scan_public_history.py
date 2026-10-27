#!/usr/bin/env python3
"""Heuristic Git-history hygiene check, not a complete secrets audit.

Run after fetching relevant refs. Prints categories and counts, never matches.
"""
from __future__ import annotations
import re
import subprocess
import sys

PATTERNS = {
    "private_key": rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "github_token": rb"\bgh[pousr]_[A-Za-z0-9]{20,}",
    "openai_style_token": rb"\bsk-[A-Za-z0-9]{30,}",
    "private_ipv4": rb"\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b",
}


def main():
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--patch", "--no-ext-diff",
             "--no-color", "--format="],
            capture_output=True, timeout=45, check=False,
        )
    except subprocess.TimeoutExpired:
        print("History scan timeout", file=sys.stderr)
        return 2
    if result.returncode or len(result.stdout) > 32 * 1024 * 1024:
        print("History scan incomplete", file=sys.stderr)
        return 2
    counts = {name: len(re.findall(pattern, result.stdout))
              for name, pattern in PATTERNS.items()}
    print("History scan: " + ", ".join(f"{name}={n}" for name, n in counts.items()))
    return int(any(counts.values()))


if __name__ == "__main__":
    raise SystemExit(main())
