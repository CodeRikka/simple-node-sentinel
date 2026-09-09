#!/usr/bin/env python3
"""Interactively fill empty fields in a Simple Node Sentinel config."""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
import tempfile
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


def maybe_enable_email(email: dict[str, Any], password_ready: bool) -> bool:
    if email.get("enabled") or not password_ready:
        return False
    required = [email.get("smtp_host"), email.get("from_address"), email.get("password_file")]
    if any(is_empty(value) for value in required):
        return False
    print("\nEmail SMTP fields look complete.")
    if prompt_yes_no("Enable email sending now (email.enabled=true)?", default_no=True):
        email["enabled"] = True
        return True
    return False


def atomic_write(path: Path, text: str, mode: int) -> None:
    """Replace a file only after a complete write; secret temp files stay 0600."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_config(path: Path, data: dict[str, Any]) -> None:
    header = (
        "# Generated/updated by scripts/configure_interactively.py\n"
        "# Edit manually anytime with: sudoedit /etc/simple-node-sentinel/config.yaml\n"
        "# User notification and fan-curve settings are edited in the dashboard.\n"
    )
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    atomic_write(path, header + text, stat.S_IMODE(path.stat().st_mode))


def configure_smtp_password(password_file: Path) -> bool:
    password_file = password_file.resolve()
    has_password = (
        password_file.exists()
        and bool(password_file.read_text(encoding="utf-8").strip())
    )
    print(f"\nSMTP password file: {password_file}")
    question = (
        "A password is already saved. Replace it?"
        if has_password else "No password is saved. Fill it now?"
    )
    if not prompt_yes_no(question, default_no=True):
        print("  existing password kept" if has_password else "  no password saved")
        return has_password

    print("  hint: paste the SMTP password or app password; input is hidden")
    try:
        password = getpass.getpass("SMTP password (Enter to keep unchanged): ").strip()
    except EOFError:
        print()
        password = ""
    if not password:
        print("  no password entered; file left unchanged")
        return has_password
    atomic_write(password_file, password + "\n", 0o600)
    print(f"  password saved to {password_file} (permissions: 600)")
    return True


def check_writable(path: Path) -> None:
    """Check access before prompting, without creating files or reading secrets."""
    path = path.resolve()
    if path.exists() and not os.access(path, os.R_OK | os.W_OK):
        raise PermissionError(13, "Permission denied", str(path))
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        raise PermissionError(13, "Permission denied", str(parent))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--password-file-default",
        default="/etc/simple-node-sentinel/smtp-password",
    )
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "No interactive terminal; nothing was written. "
            "Run this script in a terminal without </dev/null or output redirection.",
            file=sys.stderr,
        )
        return 1

    try:
        return configure(args)
    except PermissionError as exc:
        print(
            f"Permission denied: {exc.filename or args.config}. "
            "Run this configuration script with sudo. "
            "The production SMTP password should remain root-owned with mode 600.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"Could not save/read {exc.filename or args.config}: {exc.strerror}", file=sys.stderr)
        return 1
    except yaml.YAMLError:
        print(f"Invalid YAML in {args.config}; configuration not saved.", file=sys.stderr)
        return 1


def configure(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    check_writable(config_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"invalid configuration: {config_path}")
    email = ensure_mapping(raw, "email")
    if email.get("password_file"):
        password_file = Path(str(email["password_file"]))
        if not password_file.is_absolute():
            print("email.password_file must be an absolute path.", file=sys.stderr)
            return 1
        check_writable(password_file)

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

    password_file = Path(str(email["password_file"]))
    if not password_file.is_absolute():
        print("email.password_file must be an absolute path; configuration not saved.", file=sys.stderr)
        return 1
    check_writable(password_file)
    password_ready = configure_smtp_password(password_file)
    changed |= maybe_enable_email(email, password_ready)

    if changed:
        write_config(config_path, raw)
        print(f"\nUpdated {config_path}")
    else:
        print("\nNo configuration fields were changed.")

    if not password_ready:
        print("\nSMTP password is still empty; authentication is not ready.")
    print("\nInteractive configuration finished. Restart the service to load config changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
