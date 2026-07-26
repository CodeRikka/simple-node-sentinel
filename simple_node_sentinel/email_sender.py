from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Literal

from .config import EmailConfig
from .database import Database

LOGGER = logging.getLogger(__name__)

NotifyKind = Literal["temperature", "process_end"]


class EmailSender:
    def __init__(self, config: EmailConfig, database: Database) -> None:
        self.config = config
        self.database = database

    def recipients_for(
        self,
        usernames: Iterable[str],
        *,
        include_admins: bool = True,
        notify: NotifyKind | None = None,
    ) -> tuple[list[str], list[str]]:
        recipients: set[str] = set()
        if include_admins:
            recipients.update(self.database.admin_emails())

        missing: list[str] = []
        for username in sorted(set(usernames)):
            user = self.database.get_user_setting(username)
            if user is None or not user.get("active"):
                missing.append(username)
                continue
            if notify == "temperature" and not user.get("notify_temperature"):
                continue
            if notify == "process_end" and not user.get("notify_process_end"):
                continue
            email = (user.get("email") or "").strip()
            if email:
                recipients.add(email)
            else:
                missing.append(username)
        return sorted(recipients), missing

    def send(
        self,
        subject: str,
        body: str,
        usernames: Iterable[str],
        *,
        include_admins: bool = True,
        notify: NotifyKind | None = None,
    ) -> tuple[str, list[str], str | None]:
        recipients, missing = self.recipients_for(
            usernames, include_admins=include_admins, notify=notify
        )
        if missing:
            body += "\n\nNo configured email address for: " + ", ".join(missing)
        if not self.config.enabled:
            return "disabled", recipients, None
        if not recipients:
            return "skipped", recipients, "no recipients configured"
        try:
            password = Path(self.config.password_file).read_text(
                encoding="utf-8"
            ).strip()
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = self.config.from_address
            message["To"] = ", ".join(recipients)
            message.set_content(body)
            with smtplib.SMTP(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=20,
            ) as smtp:
                smtp.ehlo()
                if self.config.use_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if self.config.username:
                    smtp.login(self.config.username, password)
                smtp.send_message(message)
            return "sent", recipients, None
        except (OSError, smtplib.SMTPException) as exc:
            error = f"{type(exc).__name__}: email delivery failed"
            LOGGER.error("%s", error)
            return "failed", recipients, error
