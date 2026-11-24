"""One-time provisioning of the phone's runtime credentials over adb.

Reads the operator's local secret files, builds a JSON blob in memory and streams it on stdin
through `adb shell run-as <pkg>` into the app's private files/provision.json (debuggable build
only). The app encrypts it with a hardware-backed Keystore key and deletes the plaintext.
Values are never printed, logged or passed as command-line arguments; only key NAMES are shown.

  python tools/provision_phone_secrets.py --dry-run     # names only, nothing sent
  python tools/provision_phone_secrets.py               # push to the one attached device
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "com.nightwatch.etoro"
FILE_SOURCES = {  # key -> secret file holding the bare value
    "PHONE_PUSH_SECRET": ROOT / "secrets" / "phone_push_secret.txt",
    "COMMAND_HMAC_SECRET": ROOT / "secrets" / "command_hmac_secret.txt",
}
ENV_FILE = ROOT / "config" / ".secrets.env"
ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def collect(env_file: Path = ENV_FILE, file_sources=None) -> dict:
    values = {}
    for key, path in (file_sources or FILE_SOURCES).items():
        if path.is_file() and (text := path.read_text(encoding="utf-8").strip()):
            values[key] = text
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            name, value = name.strip(), value.strip().strip("'\"")
            if sep and name in ENV_KEYS and value:
                values[name] = value
    return values


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--serial")
    args = ap.parse_args(argv)
    values = collect()
    wanted = [*FILE_SOURCES, *ENV_KEYS]
    print("found:  ", ", ".join(k for k in wanted if k in values) or "(none)")
    print("missing:", ", ".join(k for k in wanted if k not in values) or "(none)")
    if args.dry_run or not values:
        return 0 if args.dry_run else 1
    adb = ["adb", *(["-s", args.serial] if args.serial else [])]
    payload = json.dumps(values).encode("utf-8")
    result = subprocess.run(
        [*adb, "shell", f"run-as {PACKAGE} sh -c 'umask 077; cat > files/provision.json'"],
        input=payload, capture_output=True)
    if result.returncode != 0:
        print("adb push failed (is the build debuggable and the device authorized?)", file=sys.stderr)
        return 1
    print("pushed; the app imports and deletes the plaintext on its next start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
