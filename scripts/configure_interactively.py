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


def fill_email_list(
    mapping: dict[str, Any],
    key: str,
    label: str,
    hint: str,
) -> bool:
    current = mapping.get(key) or []
    if not is_empty(current):
        return False
    print(f"\nEmpty field: {label}")
    if not prompt_yes_no("Fill this field now?", default_no=True):
        return False
    print(f"  hint: {hint}")
    print("  Enter one value per line. Press Enter on an empty line to finish.")
    values: list[str] = []
    while True:
        item = prompt("  item", default="")
        if not item:
            break
        values.append(item)
    mapping[key] = values
    return bool(values)


def fill_users(root: dict[str, Any]) -> bool:
    users = root.get("users")
    if isinstance(users, dict) and users:
        return False
    print("\nEmpty field: users")
    print("  Map Linux usernames to email addresses.")
    if not prompt_yes_no("Add users now?", default_no=True):
        root["users"] = {}
        return False
    filled: dict[str, dict[str, str]] = {}
    print("  Enter username, then email. Press Enter on username to finish.")
    while True:
        username = prompt("  Linux username", default="")
        if not username:
            break
        email = prompt(
            f"  email for {username}",
            hint="example: name@example.com",
            default="",
        )
        if email:
            filled[username] = {"email": email}
        else:
            print("  skipped user without email")
    root["users"] = filled
    return bool(filled)


def fill_process_end_users(root: dict[str, Any]) -> bool:
    section = ensure_mapping(root, "process_end_notifications")
    current = section.get("users") or []
    if not is_empty(current):
        return False
    print("\nEmpty field: process_end_notifications.users")
    print("  Linux usernames that should be emailed when their GPU process ends.")
    if not prompt_yes_no("Fill this field now?", default_no=True):
        section["users"] = []
        return False
    known_users = sorted((root.get("users") or {}).keys())
    if known_users:
        print(f"  known users from users map: {', '.join(known_users)}")
    print("  Enter one username per line. Press Enter on an empty line to finish.")
    values: list[str] = []
    while True:
        item = prompt("  username", default="")
        if not item:
            break
        values.append(item)
    section["users"] = values
    if "missing_duration_seconds" not in section:
        section["missing_duration_seconds"] = 20
    return bool(values)


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

    email = ensure_mapping(raw, "email")
    changed = False
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
    changed |= fill_email_list(
        email,
        "admin_emails",
        "email.admin_emails",
        "administrator emails for GPU temperature alerts",
    )
    changed |= fill_users(raw)
    changed |= fill_process_end_users(raw)

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
