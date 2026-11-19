"""Store a secret in cx_agent/.env without it being shown on screen.

    .venv/bin/python -m sprinto_outreach.set_secret GMAIL_APP_PASSWORD
"""
import getpass
import re
import sys

from .settings import ROOT

ENV = ROOT / ".env"


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "GMAIL_APP_PASSWORD"
    value = getpass.getpass(f"Paste {name} (hidden) and press Enter: ").strip()
    if name == "GMAIL_APP_PASSWORD":
        value = value.replace(" ", "")
        if not re.fullmatch(r"[a-zA-Z]{16}", value):
            sys.exit(f"That doesn't look like a 16-letter app password (got {len(value)} characters). Nothing saved.")
    if not value:
        sys.exit("Empty — nothing saved.")
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    out, done = [], False
    for line in lines:
        if line.startswith(f"{name}="):
            out.append(f"{name}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{name}={value}")
    ENV.write_text("\n".join(out) + "\n")
    print(f"Saved {name} to {ENV} ({len(value)} characters).")


if __name__ == "__main__":
    main()
