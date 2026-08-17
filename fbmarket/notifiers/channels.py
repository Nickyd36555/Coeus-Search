"""Concrete delivery channels: phone push, chat, email, webhook, desktop."""

from __future__ import annotations

import json
import logging
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage

import requests

from ..models import Listing
from .base import Notifier, html_body, plain_body

log = logging.getLogger(__name__)
TIMEOUT = 20


class TelegramNotifier(Notifier):
    """Push to a Telegram chat. Fastest path to a phone alert."""

    type_name = "telegram"

    def send(self, subject: str, listings: list[Listing], note: str = "") -> None:
        token, chat_id = self._require("bot_token", "chat_id")
        for listing in listings:
            text = (
                f"*{_md(subject)}*\n"
                f"[{_md(listing.title)}]({listing.url})\n"
                f"{_md(listing.price_text or 'No price listed')}"
            )
            details = []
            if listing.mileage is not None:
                details.append(f"{listing.mileage:,} mi")
            if listing.location:
                details.append(listing.location)
            if details:
                text += f"\n{_md(' · '.join(details))}"
            if note:
                text += f"\n_{_md(note)}_"

            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": False,
                },
                timeout=TIMEOUT,
            )
            response.raise_for_status()


def _md(text: str) -> str:
    """Escape Telegram MarkdownV2 reserved characters."""
    for char in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\\{char}")
    return text


class NtfyNotifier(Notifier):
    """Push via ntfy.sh — install the app, subscribe to a topic, done."""

    type_name = "ntfy"

    def send(self, subject: str, listings: list[Listing], note: str = "") -> None:
        (topic,) = self._require("topic")
        server = str(self.config.get("server", "https://ntfy.sh")).rstrip("/")
        priority = str(self.config.get("priority", "default"))

        headers_base = {"Priority": priority, "Tags": "car"}
        token = self.config.get("token")
        if token:
            headers_base["Authorization"] = f"Bearer {token}"

        for listing in listings:
            headers = dict(headers_base)
            headers["Title"] = _ascii(f"{subject}: {listing.title}"[:200])
            headers["Click"] = listing.url
            headers["Actions"] = f"view, Open listing, {listing.url}"

            lines = [listing.price_text or "No price listed"]
            if listing.mileage is not None:
                lines.append(f"{listing.mileage:,} mi")
            if listing.location:
                lines.append(listing.location)
            if note:
                lines.append(note)

            response = requests.post(
                f"{server}/{topic}",
                data=" · ".join(lines).encode("utf-8"),
                headers=headers,
                timeout=TIMEOUT,
            )
            response.raise_for_status()


def _ascii(text: str) -> str:
    """ntfy headers must be latin-1 safe."""
    return text.encode("ascii", "ignore").decode("ascii") or "New listing"


class DiscordNotifier(Notifier):
    """Post rich embeds to a Discord channel webhook."""

    type_name = "discord"

    def send(self, subject: str, listings: list[Listing], note: str = "") -> None:
        (url,) = self._require("webhook_url")
        for batch in _chunks(listings, 10):  # Discord caps embeds per message
            embeds = []
            for listing in batch:
                fields = []
                if listing.mileage is not None:
                    fields.append(
                        {"name": "Mileage", "value": f"{listing.mileage:,} mi", "inline": True}
                    )
                if listing.location:
                    fields.append(
                        {"name": "Location", "value": listing.location, "inline": True}
                    )
                embed = {
                    "title": listing.title[:250],
                    "url": listing.url,
                    "description": listing.price_text or "No price listed",
                    "color": 0x1877F2,
                    "fields": fields,
                }
                if listing.image_url:
                    embed["thumbnail"] = {"url": listing.image_url}
                embeds.append(embed)
            payload = {"content": f"**{subject}**" + (f"\n{note}" if note else ""), "embeds": embeds}
            response = requests.post(url, json=payload, timeout=TIMEOUT)
            response.raise_for_status()


class EmailNotifier(Notifier):
    """Send one HTML digest per batch over SMTP."""

    type_name = "email"

    def send(self, subject: str, listings: list[Listing], note: str = "") -> None:
        host, user, password, to = self._require(
            "smtp_host", "username", "password", "to"
        )
        port = int(self.config.get("smtp_port", 587))
        use_ssl = bool(self.config.get("use_ssl", port == 465))
        recipients = to if isinstance(to, list) else [to]

        message = EmailMessage()
        message["Subject"] = f"{subject} ({len(listings)})" if len(listings) > 1 else subject
        message["From"] = self.config.get("from") or user
        message["To"] = ", ".join(recipients)
        message.set_content(plain_body(listings, note))
        message.add_alternative(html_body(subject, listings, note), subtype="html")

        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=TIMEOUT) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.send_message(message)


class WebhookNotifier(Notifier):
    """POST raw JSON anywhere — Slack, Zapier, Home Assistant, your own server."""

    type_name = "webhook"

    def send(self, subject: str, listings: list[Listing], note: str = "") -> None:
        (url,) = self._require("url")
        headers = {"Content-Type": "application/json"}
        headers.update(self.config.get("headers") or {})
        payload = {
            "subject": subject,
            "note": note,
            "count": len(listings),
            "listings": [listing.to_dict() for listing in listings],
        }
        response = requests.post(
            url, data=json.dumps(payload), headers=headers, timeout=TIMEOUT
        )
        response.raise_for_status()


class DesktopNotifier(Notifier):
    """Native desktop notification on macOS or Linux."""

    type_name = "desktop"

    def send(self, subject: str, listings: list[Listing], note: str = "") -> None:
        for listing in listings:
            body = f"{listing.price_text or 'No price'} — {listing.title}"
            try:
                if sys.platform == "darwin":
                    script = (
                        f'display notification {json.dumps(body)} '
                        f'with title {json.dumps(subject)}'
                    )
                    subprocess.run(["osascript", "-e", script], check=False, timeout=10)
                elif shutil.which("notify-send"):
                    subprocess.run(
                        ["notify-send", subject, f"{body}\n{listing.url}"],
                        check=False,
                        timeout=10,
                    )
                else:
                    print(f"[{subject}] {body}\n{listing.url}")
            except Exception as exc:  # noqa: BLE001 - never kill the loop over a toast
                log.warning("desktop notification failed: %s", exc)


class ConsoleNotifier(Notifier):
    """Print to stdout. Handy for `--dry-run` and for testing filters."""

    type_name = "console"

    def send(self, subject: str, listings: list[Listing], note: str = "") -> None:
        print(f"\n=== {subject} ===")
        if note:
            print(note)
        for listing in listings:
            print(f"\n{listing.to_text()}")
        print()


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


REGISTRY = {
    cls.type_name: cls
    for cls in (
        TelegramNotifier,
        NtfyNotifier,
        DiscordNotifier,
        EmailNotifier,
        WebhookNotifier,
        DesktopNotifier,
        ConsoleNotifier,
    )
}
