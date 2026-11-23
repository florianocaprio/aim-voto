"""Create the one-time local configuration using only the standard library."""

import base64
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    config = (ROOT / ".env.example").read_text(encoding="utf-8")
    secrets_to_set = {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
        "VOTING_LINK_KEYS": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "POSTGRES_ADMIN_PASSWORD": secrets.token_urlsafe(32),
    }
    lines = []
    for line in config.splitlines():
        key, _, _ = line.partition("=")
        lines.append(f"{key}={secrets_to_set[key]}" if key in secrets_to_set else line)
    try:
        descriptor = os.open(ROOT / ".env", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SystemExit(".env esiste già. Conserva i segreti esistenti.") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    print("Configurazione locale creata. I segreti sono stati salvati in .env.")


if __name__ == "__main__":
    main()
