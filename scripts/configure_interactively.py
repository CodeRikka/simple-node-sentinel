#!/usr/bin/env python3
"""Interactively fill empty fields in a Simple Node Sentinel config."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any

import yaml


def prompt(message: str, hint: str = "", default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    if hint:
        print(f"  hint: {hint}")
    try:
        value = input(f"{message}{suffix}: ").strip()
    except EOFError:
        print()
        return default
    return value or default


def prompt_yes_no(message: str, default_no: bool = True) -> bool:
    default = "y/N" if default_no else "Y/n"
    answer = prompt(f"{message} ({default})", default="")
    if not answer:
        return not default_no
    return answer.lower() in {"y", "yes"}


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def ensure_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        value = {}
        data[key] = value
    return value


def fill_scalar(
    mapping: dict[str, Any],
    key: str,
    label: str,
    hint: str,
    default: str = "",
) -> bool:
    current = mapping.get(key, "")
    if not is_empty(current):
        return False
    print(f"\nEmpty field: {label}")
    if not prompt_yes_no("Fill this field now?", default_no=True):
        return False
    value = prompt("Value", hint=hint, default=default)
    if value:
        mapping[key] = value
        return True
    print("  skipped")
    return False


def maybe_enable_email(email: dict[str, Any]) -> None:
    if email.get("enabled"):
        return
    required = [email.get("smtp_host"), email.get("from_address"), email.get("password_file")]
    if any(is_empty(value) for value in required):
        return
    print("\nEmail SMTP fields look complete.")
    if prompt_yes_no("Enable email sending now (email.enabled=true)?", default_no=True):
        email["enabled"] = True


def write_config(path: Path, data: dict[str, Any]) -> None:
    header = (
        "# Generated/updated by scripts/configure_interactively.py\n"
        "# Edit manually anytime with: sudoedit /etc/simple-node-sentinel/config.yaml\n"
        "# User notification and fan-curve settings are edited in the dashboard.\n"
    )
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.write_text(header + text, encoding="utf-8")


def configure_smtp_password(password_file: Path) -> None:
    created = False
    if not password_file.exists():
        password_file.parent.mkdir(parents=True, exist_ok=True)
        password_file.write_text("", encoding="utf-8")
        password_file.chmod(0o600)
        created = True
        print(f"\nCreated empty SMTP password file: {password_file}")
    else:
        print(f"\nSMTP password file already exists: {password_file}")

    current = password_file.read_text(encoding="utf-8").strip()
    if current:
        print("  password file is already non-empty; leaving it unchanged")
        return

    if not prompt_yes_no(
        "Fill SMTP password now?" if created else "Password file is empty. Fill it now?",
        default_no=True,
    ):
        print("  skipped; you can edit it later with:")
        print(f"    sudoedit {password_file}")
        return

    print("  hint: paste the SMTP password or app password; input is hidden")
    try:
        password = getpass.getpass("SMTP password (Enter to skip): ")
    except EOFError:
        print()
        password = ""
    if not password:
        print("  skipped")
        return
    password_file.write_text(password.strip() + "\n", encoding="utf-8")
    password_file.chmod(0o600)
    print("  password saved")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--password-file-default",
        default="/etc/simple-node-sentinel/smtp-password",
    )
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Non-interactive terminal detected; skipping prompts.")
        return 0

    config_path: Path = args.config
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"invalid configuration: {config_path}")

    print(f"Checking empty fields in {config_path}")
    print("Press Enter to skip any prompt.")
    print(
        "User emails / admin / process-end notify and per-GPU fan curves "
        "are configured in the web dashboard, not here."
    )

    # Drop obsolete YAML sections if an older template was copied.
    changed = False
    if "users" in raw:
        raw.pop("users", None)
        changed = True
    email = ensure_mapping(raw, "email")
    if "admin_emails" in email:
        email.pop("admin_emails", None)
        changed = True
    process_end = ensure_mapping(raw, "process_end_notifications")
    if "users" in process_end:
        process_end.pop("users", None)
        changed = True
    fan = ensure_mapping(raw, "fan_control")
    for obsolete in (
        "minimum_percent",
        "maximum_percent",
        "idle_temperature_celsius",
        "idle_duration_seconds",
        "emergency_temperature_celsius",
        "emergency_fan_percent",
    ):
        if obsolete in fan:
            fan.pop(obsolete, None)
            changed = True

    changed |= fill_scalar(
        email,
        "smtp_host",
        "email.smtp_host",
        "example: smtp.gmail.com or smtp.office365.com",
    )
    changed |= fill_scalar(
        email,
        "username",
        "email.username",
        "SMTP login username, often the mailbox address",
    )
    changed |= fill_scalar(
        email,
        "from_address",
        "email.from_address",
        "example: monitor@example.com",
    )
    if is_empty(email.get("password_file")):
        changed |= fill_scalar(
            email,
            "password_file",
            "email.password_file",
            "absolute path to the SMTP password file",
            default=args.password_file_default,
        )

    if is_empty(email.get("password_file")):
        email["password_file"] = args.password_file_default
        changed = True

    maybe_enable_email(email)

    if changed:
        write_config(config_path, raw)
        print(f"\nUpdated {config_path}")
    else:
        print("\nNo configuration fields were changed.")

    password_file = Path(str(email.get("password_file") or args.password_file_default))
    configure_smtp_password(password_file)
    print("\nInteractive configuration finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
