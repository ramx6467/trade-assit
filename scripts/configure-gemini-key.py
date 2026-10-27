#!/usr/bin/env python3
"""Store a Google Gemini API key in the ignored backend/.env without echoing it."""
from __future__ import annotations

import os
import stat
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "backend" / ".env"


def main() -> int:
    key = getpass("Enter a Google Gemini API key (input hidden): ").strip()
    if not key:
        print("No key entered; backend configuration was not changed.")
        return 1

    model = input("Enter Gemini model [default: gemini-flash-latest]: ").strip() or "gemini-flash-latest"

    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value

    values["AI_PROVIDER"] = "gemini"
    values["GEMINI_API_KEY"] = key
    values["GEMINI_MODEL"] = model

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)
    print("Saved Gemini credentials to backend/.env with owner-only permissions. POST /api/v1/ai/verify to confirm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
