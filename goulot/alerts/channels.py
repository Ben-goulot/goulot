"""Alert channels (docs/SPEC.md, section 9; docs/SECURITY.md, threat 18).

- Telegram: the bot only sends, to the owner's chat id; plain text (no `parse_mode`), link previews disabled; it
  never reads incoming messages (no getUpdates, no webhook). Through the allowlist client.
- E-mail: SMTP over TLS to the configured server only, to the owner only; the server name is resolved here, every
  address must be public, and the connection goes to the checked address (no second resolution) with the
  certificate verified against the server name. Rule 6's first exception.
Every text is neutralized (`defang`) before it leaves.
"""

from __future__ import annotations

import socket
import ssl
from email.message import EmailMessage
from smtplib import SMTP, SMTP_SSL, SMTPException
from typing import Any

import httpx2

from goulot.alerts.defang import defang
from goulot.net.transport import is_public_address

TELEGRAM = "https://api.telegram.org/bot{token}/sendMessage"
MAX_TEXT = 3500


class ChannelError(RuntimeError):
    pass


def telegram_payload(chat_id: str, text: str) -> dict[str, Any]:
    return {"chat_id": chat_id, "text": defang(text)[:MAX_TEXT], "link_preview_options": {"is_disabled": True}}


def send_telegram(client: httpx2.Client, token: str, chat_id: str, text: str) -> None:
    if not chat_id.lstrip("-").isdigit():
        raise ChannelError("invalid chat id")
    response = client.post(TELEGRAM.format(token=token), json=telegram_payload(chat_id, text))
    if response.status_code != 200:
        raise ChannelError(f"Telegram HTTP {response.status_code}")


def _public_address(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ChannelError("SMTP DNS failure") from exc
    addresses = [str(i[4][0]) for i in infos]
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise ChannelError("SMTP server resolves to a non-public address")
    return addresses[0]


class _PinnedSMTPSSL(SMTP_SSL):
    """Connects to the address checked beforehand, TLS verified against the server name."""

    def __init__(self, host: str, address: str, port: int, context: ssl.SSLContext, timeout: float) -> None:
        self._pinned = address
        self._server_name = host
        super().__init__(host=host, port=port, context=context, timeout=timeout)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        raw = socket.create_connection((self._pinned, port), timeout)
        return self.context.wrap_socket(raw, server_hostname=self._server_name)


class _PinnedSMTP(SMTP):
    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        self._pinned = address
        super().__init__(host=host, port=port, timeout=timeout)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection((self._pinned, port), timeout)


def build_email(sender: str, recipient: str, subject: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, recipient, defang(subject)[:150]
    msg.set_content(defang(text)[:MAX_TEXT * 2])
    return msg


def send_email(host: str, port: int, user: str, password: str, recipient: str, subject: str, text: str,
               timeout: float = 20.0) -> None:
    address = _public_address(host, port)
    context = ssl.create_default_context()
    msg = build_email(user, recipient, subject, text)
    try:
        if port == 465:
            with _PinnedSMTPSSL(host, address, port, context, timeout) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg, to_addrs=[recipient])
        else:
            with _PinnedSMTP(host, address, port, timeout) as smtp:
                smtp.starttls(context=context)  # refuses to go on without TLS
                smtp.login(user, password)
                smtp.send_message(msg, to_addrs=[recipient])
    except (SMTPException, OSError, ssl.SSLError) as exc:
        raise ChannelError(f"SMTP {type(exc).__name__}") from exc
