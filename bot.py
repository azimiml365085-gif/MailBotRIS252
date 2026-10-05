"""Forward Mail.ru messages to a Telegram group.

The script is designed to run as a short GitHub Actions job.  It keeps the
source message in INBOX until Telegram has accepted every part and the IMAP
archive operation has succeeded.
"""

from __future__ import annotations

import email
import html
import imaplib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from email.policy import default as default_email_policy
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("mailbot")

DEFAULT_IMAP_SERVER = "imap.mail.ru"
DEFAULT_SKIP_SENDERS = "security@id.mail.ru"
DEFAULT_SEEN_FOLDER = "SeenByBot"
DEFAULT_IGNORED_FOLDER = "IgnoredByBot"

TELEGRAM_TEXT_LIMIT = 4096
BODY_CHUNK_UNITS = 3500
MAX_TEXT_MESSAGES = 8
LONG_BODY_PREVIEW_UNITS = 900
MAX_TELEGRAM_FILE_BYTES = 50_000_000
DEFAULT_SEND_INTERVAL = 3.1
MAX_TELEGRAM_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 300
DEFAULT_MAX_EMAILS_PER_RUN = 50
DEFAULT_IMAP_TIMEOUT = 30

FLAG_COMPLETE = "BOT_SENT_COMPLETE"
FLAG_IGNORED = "BOT_IGNORED"
FLAG_ARCHIVED = "BOT_ARCHIVED"
FLAG_ATTACHMENT_WARNING = "BOT_ATTACHMENT_WARNING_SENT"


class MailBotError(Exception):
    """An actionable error safe to include in a workflow log."""


class ConfigurationError(MailBotError):
    pass


class ImapError(MailBotError):
    pass


class TelegramError(MailBotError):
    pass


@dataclass(frozen=True)
class Settings:
    imap_server: str
    email_user: str
    email_password: str
    telegram_token: str
    telegram_chat_id: str
    seen_folder: str = DEFAULT_SEEN_FOLDER
    ignored_folder: str = DEFAULT_IGNORED_FOLDER
    skip_senders: tuple[str, ...] = ("security@id.mail.ru",)
    send_interval: float = DEFAULT_SEND_INTERVAL
    max_text_messages: int = MAX_TEXT_MESSAGES
    max_emails_per_run: int = DEFAULT_MAX_EMAILS_PER_RUN
    imap_timeout: int = DEFAULT_IMAP_TIMEOUT

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "EMAIL_USER": os.getenv("EMAIL_USER", "").strip(),
            "EMAIL_PASSWORD": os.getenv("EMAIL_PASSWORD", ""),
            "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN", "").strip(),
            "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError(
                "Не заданы обязательные переменные: " + ", ".join(missing)
            )

        def positive_int(name: str, default: int) -> int:
            raw = os.getenv(name, str(default)).strip()
            try:
                value = int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name} должен быть целым числом.") from exc
            if value < 1:
                raise ConfigurationError(f"{name} должен быть больше нуля.")
            return value

        try:
            send_interval = float(os.getenv("TELEGRAM_SEND_INTERVAL", str(DEFAULT_SEND_INTERVAL)))
        except ValueError as exc:
            raise ConfigurationError("TELEGRAM_SEND_INTERVAL должен быть числом.") from exc
        if send_interval < 0:
            raise ConfigurationError("TELEGRAM_SEND_INTERVAL не может быть отрицательным.")

        skip_senders = tuple(
            address.strip().casefold()
            for address in os.getenv("SKIP_SENDERS", DEFAULT_SKIP_SENDERS).split(",")
            if address.strip()
        )

        seen_folder = os.getenv("SEEN_FOLDER", DEFAULT_SEEN_FOLDER).strip() or DEFAULT_SEEN_FOLDER
        ignored_folder = (
            os.getenv("IGNORED_FOLDER", DEFAULT_IGNORED_FOLDER).strip()
            or DEFAULT_IGNORED_FOLDER
        )
        if seen_folder.casefold() == ignored_folder.casefold():
            raise ConfigurationError("SEEN_FOLDER и IGNORED_FOLDER должны различаться.")

        return cls(
            imap_server=os.getenv("IMAP_SERVER", DEFAULT_IMAP_SERVER).strip() or DEFAULT_IMAP_SERVER,
            email_user=required["EMAIL_USER"],
            email_password=required["EMAIL_PASSWORD"],
            telegram_token=required["TELEGRAM_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            seen_folder=seen_folder,
            ignored_folder=ignored_folder,
            skip_senders=skip_senders,
            send_interval=send_interval,
            max_text_messages=positive_int("MAX_TEXT_MESSAGES", MAX_TEXT_MESSAGES),
            max_emails_per_run=positive_int(
                "MAX_EMAILS_PER_RUN", DEFAULT_MAX_EMAILS_PER_RUN
            ),
            imap_timeout=positive_int("IMAP_TIMEOUT", DEFAULT_IMAP_TIMEOUT),
        )


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class MailContent:
    subject: str
    sender: str
    body: str
    attachments: tuple[Attachment, ...]
    message_id: str


@dataclass(frozen=True)
class DeliveryPlan:
    messages: tuple[str, ...]
    attachments: tuple[Attachment, ...]


def decode_mime_words(value: str | None) -> str:
    """Decode RFC 2047 headers without failing on a bad/unknown charset."""
    pieces: list[str] = []
    for part, encoding in decode_header(value or ""):
        if isinstance(part, bytes):
            encodings = [encoding, "utf-8", "cp1251", "latin-1"]
            decoded = None
            for candidate in encodings:
                if not candidate:
                    continue
                try:
                    decoded = part.decode(candidate)
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            pieces.append(decoded if decoded is not None else part.decode("utf-8", "replace"))
        else:
            pieces.append(part)
    return "".join(pieces).strip()


_HTML_TAG_RE = re.compile(
    r"</?(?:html|body|div|p|span|a|br|table|thead|tbody|tr|td|ul|ol|li|h[1-6]|style|section|article)\b[^>]*>",
    re.IGNORECASE,
)


def looks_like_html(text: str) -> bool:
    """Recognize HTML accidentally placed in a text/plain MIME part."""
    return bool(_HTML_TAG_RE.search(text or ""))


def html_to_text(source: str) -> str:
    """Render useful email HTML as clean text; never forward markup or CSS."""
    class Renderer(HTMLParser):
        SKIP_TAGS = {"head", "link", "meta", "noscript", "script", "style", "title"}
        BLOCK_TAGS = {
            "address", "article", "blockquote", "div", "dl", "fieldset", "footer",
            "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
            "main", "ol", "p", "section", "table", "tbody", "td", "th", "tr", "ul",
        }

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.output: list[str] = []
            self.skipping: list[str] = []
            self.anchors: list[tuple[str, int]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            tag = tag.casefold()
            if self.skipping:
                if tag == self.skipping[-1]:
                    self.skipping.append(tag)
                return
            if tag in self.SKIP_TAGS:
                self.skipping.append(tag)
                return

            attributes = dict(attrs)
            style = re.sub(r"\s+", "", str(attributes.get("style") or "")).casefold()
            if (
                "hidden" in attributes
                or str(attributes.get("aria-hidden") or "").casefold() == "true"
                or "display:none" in style
                or "visibility:hidden" in style
            ):
                self.skipping.append(tag)
                return
            if tag == "br" or tag == "hr":
                self.output.append("\n")
            elif tag == "li":
                self.output.append("\n• ")
            elif tag in self.BLOCK_TAGS:
                self.output.append("\n")
            elif tag == "a":
                self.anchors.append((str(attributes.get("href") or "").strip(), len(self.output)))
            elif tag == "img":
                alt = str(attributes.get("alt") or "").strip()
                if alt:
                    self.output.append(f"[{alt}]")

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

        def handle_endtag(self, tag: str) -> None:
            tag = tag.casefold()
            if self.skipping:
                if tag == self.skipping[-1]:
                    self.skipping.pop()
                return
            if tag == "a" and self.anchors:
                href, output_start = self.anchors.pop()
                label = re.sub(r"\s+", " ", "".join(self.output[output_start:])).strip()
                try:
                    scheme = urlsplit(href).scheme.casefold() if href else ""
                except ValueError:
                    scheme = ""
                if href and scheme in {"http", "https", "mailto"} and href not in label:
                    self.output.append(f" ({href})" if label else href)
            if tag in self.BLOCK_TAGS:
                self.output.append("\n")

        def handle_data(self, data: str) -> None:
            if not self.skipping:
                self.output.append(data)

    parser = Renderer()
    parser.feed(source or "")
    parser.close()
    rendered = "".join(parser.output)
    rendered = rendered.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t ]+", " ", line).strip() for line in rendered.split("\n")]

    cleaned: list[str] = []
    blank_count = 0
    for line in lines:
        if line:
            cleaned.append(line)
            blank_count = 0
        elif cleaned and blank_count == 0:
            cleaned.append("")
            blank_count = 1
    rendered = "\n".join(cleaned).strip()
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", rendered)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""

    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _safe_filename(name: str) -> str:
    name = decode_mime_words(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name).strip(" .")
    return name[:180] or "attachment.bin"


def extract_mail_content(msg: Message) -> MailContent:
    """Choose one readable body and collect real/inline file attachments."""
    subject = re.sub(r"\s+", " ", decode_mime_words(msg.get("Subject"))).strip() or "Без темы"
    raw_from = re.sub(r"\s+", " ", decode_mime_words(msg.get("From"))).strip()
    display_name, address = parseaddr(raw_from)
    sender = display_name.strip() or address.strip() or raw_from.strip() or "Неизвестный отправитель"
    if address and display_name:
        sender = f"{display_name.strip()} <{address.strip()}>"

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        is_attachment = disposition == "attachment" or bool(filename)
        payload = part.get_payload(decode=True)

        if is_attachment or disposition == "inline" and content_type not in {"text/plain", "text/html"}:
            if payload is None:
                raw = part.get_payload()
                payload = raw.encode("utf-8") if isinstance(raw, str) else b""
            if not filename:
                subtype = part.get_content_subtype() or "bin"
                prefix = "inline-image" if disposition == "inline" else "attachment"
                filename = f"{prefix}.{subtype}"
            attachments.append(
                Attachment(
                    filename=_safe_filename(filename),
                    content=payload,
                    content_type=content_type or "application/octet-stream",
                )
            )
            continue

        if content_type == "text/plain":
            text = _decode_part(part).strip()
            if text:
                plain_parts.append(text)
        elif content_type == "text/html":
            text = _decode_part(part).strip()
            if text:
                html_parts.append(text)

    body = plain_parts[0] if plain_parts else ""
    if body and looks_like_html(body):
        body = html_to_text(body)
    elif not body and html_parts:
        body = html_to_text(html_parts[0])
    elif body:
        body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
        body = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", body)

    if not body:
        body = "[Письмо без текстового содержимого]"

    return MailContent(
        subject=subject,
        sender=sender,
        body=body,
        attachments=tuple(attachments),
        message_id=(msg.get("Message-ID", "") or "").strip(),
    )


def utf16_units(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def truncate_utf16(text: str, max_units: int) -> str:
    if max_units < 0:
        raise ValueError("max_units must not be negative")
    result: list[str] = []
    used = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > max_units:
            break
        result.append(char)
        used += width
    return "".join(result)


def split_text(text: str, max_units: int = BODY_CHUNK_UNITS) -> list[str]:
    """Split text in a strictly advancing, finite pass, preferring word breaks."""
    if max_units < 2:
        raise ValueError("max_units must be at least 2")
    if not text:
        return [""]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start
        used = 0
        last_break: int | None = None
        while end < len(text):
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if used + width > max_units:
                break
            used += width
            end += 1
            if text[end - 1] in " \n\t" and used >= max_units // 2:
                last_break = end

        if end == len(text):
            chunks.append(text[start:end])
            break
        if last_break is not None and last_break > start:
            end = last_break
        if end <= start:
            raise ValueError("The chunk limit is too small for this text.")
        chunks.append(text[start:end])
        start = end
    return chunks


def _telegram_prefix(subject: str, sender: str) -> str:
    safe_sender = html.escape(truncate_utf16(sender, 160), quote=True)
    safe_subject = html.escape(truncate_utf16(subject, 160), quote=True)
    return (
        "<b>Новое письмо</b>\n"
        f"<b>От:</b> {safe_sender}\n"
        f"<b>Тема:</b> {safe_subject}"
    )


def _validate_telegram_message(text: str) -> None:
    visible_text = html_to_text(text)
    if utf16_units(visible_text) > TELEGRAM_TEXT_LIMIT:
        raise ValueError("A Telegram message exceeds the Bot API text limit.")


def build_delivery_plan(
    subject: str,
    sender: str,
    body: str,
    attachments: Iterable[Attachment],
    max_text_messages: int = MAX_TEXT_MESSAGES,
) -> DeliveryPlan:
    """Create safe Telegram HTML messages or one complete text-file fallback."""
    if max_text_messages < 1:
        raise ValueError("max_text_messages must be at least 1")

    original_attachments = tuple(attachments)
    chunks = split_text(body)
    prefix = _telegram_prefix(subject, sender)

    if len(chunks) > max_text_messages:
        preview = truncate_utf16(body, LONG_BODY_PREVIEW_UNITS).rstrip()
        if preview and len(body) > len(preview):
            preview += "…"
        message = (
            f"{prefix}\n\n"
            "Письмо длинное: полный текст отправлен файлом <code>email_body.txt</code>.\n\n"
            f"<b>Начало письма:</b>\n{html.escape(preview, quote=False)}"
        )
        _validate_telegram_message(message)
        body_attachment = Attachment(
            filename="email_body.txt",
            content=body.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
        )
        return DeliveryPlan((message,), (body_attachment, *original_attachments))

    messages: list[str] = []
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        if index == 1:
            heading = prefix
            if total > 1:
                heading += f"\n<i>Часть {index}/{total}</i>"
        else:
            safe_subject = html.escape(truncate_utf16(subject, 160), quote=True)
            heading = f"<b>Продолжение письма {index}/{total}</b>\n<b>Тема:</b> {safe_subject}"
        message = f"{heading}\n\n{html.escape(chunk, quote=False)}"
        _validate_telegram_message(message)
        messages.append(message)

    return DeliveryPlan(tuple(messages), original_attachments)


def _retry_after(payload: dict) -> int:
    try:
        value = int(payload.get("parameters", {}).get("retry_after", 3))
    except (AttributeError, TypeError, ValueError):
        return 3
    return max(1, value)


class TelegramClient:
    """Small Bot API client that checks `ok` and observes group rate limits."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        send_interval: float = DEFAULT_SEND_INTERVAL,
        opener: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._chat_id = chat_id
        self._send_interval = send_interval
        self._opener = opener or urlopen
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        wait = self._send_interval - (self._clock() - self._last_request_at)
        if wait > 0:
            self._sleep(wait)

    def _post(
        self,
        method: str,
        data: dict,
        document: Attachment | None = None,
    ) -> dict:
        for attempt in range(MAX_TELEGRAM_ATTEMPTS):
            self._pace()
            if document is not None:
                body, content_type = _encode_multipart(data, document)
            else:
                body = urlencode(data).encode("utf-8")
                content_type = "application/x-www-form-urlencoded; charset=utf-8"
            request = Request(
                f"{self._base_url}/{method}",
                data=body,
                headers={"Content-Type": content_type},
                method="POST",
            )
            self._last_request_at = self._clock()
            try:
                response = self._opener(request, timeout=60 if document is not None else 30)
                status_code = getattr(response, "status", None)
                if status_code is None:
                    status_code = response.getcode()
                response_body = response.read()
                close = getattr(response, "close", None)
                if close:
                    close()
            except HTTPError as exc:
                status_code = exc.code
                try:
                    response_body = exc.read()
                finally:
                    exc.close()
            except (URLError, TimeoutError, OSError):
                # urllib exceptions can include the full URL, which contains the bot token.
                raise TelegramError("Не удалось связаться с Telegram API.") from None

            try:
                payload = json.loads(response_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, AttributeError):
                raise TelegramError("Telegram вернул ответ, который не удалось разобрать.") from None

            error_code = payload.get("error_code") if isinstance(payload, dict) else None
            if status_code == 429 or error_code == 429:
                retry_after = _retry_after(payload if isinstance(payload, dict) else {})
                if retry_after > MAX_RETRY_AFTER_SECONDS:
                    raise TelegramError(
                        f"Telegram попросил подождать {retry_after} секунд; письмо оставлено в почте."
                    )
                if attempt + 1 >= MAX_TELEGRAM_ATTEMPTS:
                    raise TelegramError("Telegram продолжает ограничивать частоту отправки.")
                self._sleep(retry_after)
                continue

            if not (200 <= status_code < 300) or not isinstance(payload, dict) or payload.get("ok") is not True:
                description = payload.get("description", "") if isinstance(payload, dict) else ""
                description = re.sub(r"[\r\n\x00-\x1f]+", " ", str(description))[:180]
                detail = description or f"HTTP {status_code}"
                raise TelegramError(f"Telegram отклонил отправку: {detail}")

            return payload.get("result", {})

        raise TelegramError("Telegram не принял отправку после ограниченного числа попыток.")

    def send_message(self, text: str) -> dict:
        return self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def send_document(self, attachment: Attachment) -> dict:
        if len(attachment.content) > MAX_TELEGRAM_FILE_BYTES:
            raise TelegramError(
                f"Вложение {attachment.filename!r} больше допустимого размера Telegram; письмо оставлено в почте."
            )
        return self._post(
            "sendDocument",
            {"chat_id": self._chat_id},
            document=attachment,
        )

def _encode_multipart(data: dict, document: Attachment) -> tuple[bytes, str]:
    boundary = f"----MailBotRIS252{secrets.token_hex(12)}"
    chunks: list[bytes] = []
    for name, value in data.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    filename = _safe_filename(document.filename).replace('"', "'")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                'Content-Disposition: form-data; name="document"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {document.content_type}\r\n\r\n".encode("ascii", "replace"),
            document.content,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _imap_quote(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_ok(status: object) -> bool:
    return str(status).upper() == "OK"


def ensure_folder(mail: imaplib.IMAP4_SSL, folder: str) -> None:
    status, data = mail.list('""', _imap_quote(folder))
    if _imap_ok(status) and any(item for item in (data or []) if item):
        return
    status, _ = mail.create(_imap_quote(folder))
    if _imap_ok(status):
        return
    # Another run/client may have created the folder after LIST.
    check_status, check_data = mail.list('""', _imap_quote(folder))
    if _imap_ok(check_status) and any(item for item in (check_data or []) if item):
        return
    raise ImapError(f"Не удалось создать почтовую папку {folder!r}.")


def supports_user_flags(mail: imaplib.IMAP4_SSL) -> bool:
    """Return True only when SELECT advertised permanent custom IMAP keywords."""
    try:
        _, data = mail.response("PERMANENTFLAGS")
    except (imaplib.IMAP4.error, AttributeError):
        return False
    raw = b" ".join(item for item in (data or []) if isinstance(item, bytes)).upper()
    return b"\\*" in raw


def get_capabilities(mail: imaplib.IMAP4_SSL) -> set[str]:
    try:
        status, data = mail.capability()
    except (imaplib.IMAP4.error, OSError):
        return set()
    if not _imap_ok(status):
        return set()
    raw = b" ".join(item for item in (data or []) if isinstance(item, bytes))
    return {part.decode("ascii", "ignore").upper() for part in raw.split()}


def fetch_message(mail: imaplib.IMAP4_SSL, uid: str) -> tuple[Message, set[str]]:
    status, data = mail.uid("FETCH", uid, "(UID FLAGS RFC822)")
    if not _imap_ok(status):
        raise ImapError(f"Не удалось прочитать письмо с UID {uid}.")

    metadata: list[bytes] = []
    raw_message: bytes | None = None
    for item in data or []:
        if isinstance(item, tuple):
            if item and isinstance(item[0], bytes):
                metadata.append(item[0])
            if len(item) > 1 and isinstance(item[1], bytes):
                raw_message = item[1]
        elif isinstance(item, bytes):
            metadata.append(item)

    if raw_message is None:
        raise ImapError(f"Сервер не вернул содержимое письма с UID {uid}.")

    meta = b" ".join(metadata)
    flag_match = re.search(rb"FLAGS\s+\(([^)]*)\)", meta, re.IGNORECASE)
    flags = {
        item.decode("ascii", "ignore").upper()
        for item in (flag_match.group(1).split() if flag_match else [])
    }
    return email.message_from_bytes(raw_message, policy=default_email_policy), flags


def add_uid_flag(mail: imaplib.IMAP4_SSL, uid: str, flag: str) -> None:
    status, _ = mail.uid("STORE", uid, "+FLAGS.SILENT", f"({flag})")
    if not _imap_ok(status):
        raise ImapError("Не удалось сохранить отметку обработки письма в IMAP.")


def _folder_contains_message_id(
    mail: imaplib.IMAP4_SSL,
    folder: str,
    message_id: str,
) -> bool:
    if not message_id:
        return False
    status, _ = mail.select(folder, readonly=True)
    if not _imap_ok(status):
        inbox_status, _ = mail.select("INBOX")
        if not _imap_ok(inbox_status):
            raise ImapError("Не удалось вернуться в INBOX после проверки архива.")
        return False
    try:
        status, data = mail.uid(
            "SEARCH", None, "HEADER", "Message-ID", _imap_quote(message_id)
        )
        if not _imap_ok(status):
            return False
        return bool(data and data[0] and data[0].strip())
    finally:
        inbox_status, _ = mail.select("INBOX")
        if not _imap_ok(inbox_status):
            raise ImapError("Не удалось вернуться в INBOX после проверки архива.")


def move_uid(
    mail: imaplib.IMAP4_SSL,
    uid: str,
    folder: str,
    capabilities: set[str],
    user_flags_supported: bool,
) -> bool:
    """Archive a message without expunging unrelated messages.

    Returns False only when the server can copy but has no safe UID-specific
    delete operation. In that case the archive copy is still made and the
    caller records a checkpoint where possible.
    """
    if "MOVE" in capabilities:
        status, _ = mail.uid("MOVE", uid, _imap_quote(folder))
        if not _imap_ok(status):
            raise ImapError(f"Не удалось переместить письмо в {folder!r}.")
        return True

    status, _ = mail.uid("COPY", uid, _imap_quote(folder))
    if not _imap_ok(status):
        raise ImapError(f"Не удалось скопировать письмо в {folder!r}; оригинал сохранён.")

    status, _ = mail.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
    if not _imap_ok(status):
        raise ImapError("Письмо скопировано, но не удалось отметить оригинал для удаления.")

    if "UIDPLUS" not in capabilities:
        # Do not use global EXPUNGE: it could remove other messages the user
        # marked for deletion. SEARCH UNDELETED will skip this archived copy.
        return False

    status, _ = mail.uid("EXPUNGE", uid)
    if not _imap_ok(status):
        raise ImapError("Письмо скопировано, но не удалось удалить оригинал из INBOX.")
    return True


def _sender_address(sender_header: str) -> str:
    _, address = parseaddr(sender_header)
    return address.casefold().strip()


def _message_marker(index: int) -> str:
    return f"BOT_SENT_TEXT_{index:04d}"


def _attachment_marker(index: int) -> str:
    return f"BOT_SENT_FILE_{index:04d}"


def _failed_attachment_marker(index: int) -> str:
    return f"BOT_FAILED_FILE_{index:04d}"


def _attachment_failure_reason(
    attachment: Attachment,
    skip_all_due_to_oversize: bool = False,
) -> str:
    if len(attachment.content) > MAX_TELEGRAM_FILE_BYTES:
        return "размер превышает лимит Telegram 50 МБ"
    if skip_all_due_to_oversize:
        return "не отправлено из-за другого вложения больше лимита 50 МБ"
    return "Telegram не подтвердил доставку файла"


def _attachment_warning_message(
    failures: list[tuple[Attachment, str]],
) -> str:
    lines = [
        "⚠️ <b>Не все вложения удалось отправить в Telegram</b>",
        "Откройте исходное письмо в Mail.ru, чтобы скачать эти файлы:",
    ]
    for attachment, reason in failures[:3]:
        filename = html.escape(
            truncate_utf16(attachment.filename, 120), quote=False
        )
        lines.append(f"• <code>{filename}</code> — {html.escape(reason, quote=False)}")
    if len(failures) > 3:
        lines.append(f"• Ещё {len(failures) - 3} вложений не доставлено.")
    message = "\n".join(lines)
    _validate_telegram_message(message)
    return message


def process_one(
    mail: imaplib.IMAP4_SSL,
    uid: str,
    settings: Settings,
    telegram: TelegramClient,
    capabilities: set[str],
    user_flags_supported: bool,
) -> str:
    msg, flags = fetch_message(mail, uid)
    content = extract_mail_content(msg)

    if FLAG_ARCHIVED in flags:
        return "already-archived"

    if content.message_id and _folder_contains_message_id(
        mail, settings.seen_folder, content.message_id
    ):
        if user_flags_supported and FLAG_COMPLETE in flags:
            add_uid_flag(mail, uid, FLAG_ARCHIVED)
        return "already-archived"

    if content.message_id and _folder_contains_message_id(
        mail, settings.ignored_folder, content.message_id
    ):
        if user_flags_supported:
            add_uid_flag(mail, uid, FLAG_IGNORED)
            add_uid_flag(mail, uid, FLAG_ARCHIVED)
        return "ignored"

    if FLAG_COMPLETE in flags:
        moved = move_uid(
            mail, uid, settings.seen_folder, capabilities, user_flags_supported
        )
        if not moved and user_flags_supported:
            add_uid_flag(mail, uid, FLAG_ARCHIVED)
        return "already-sent"

    sender_address = _sender_address(decode_mime_words(msg.get("From")))
    if sender_address and sender_address in settings.skip_senders:
        if FLAG_IGNORED not in flags and user_flags_supported:
            add_uid_flag(mail, uid, FLAG_IGNORED)
            flags.add(FLAG_IGNORED)
        moved = move_uid(
            mail, uid, settings.ignored_folder, capabilities, user_flags_supported
        )
        if not moved and user_flags_supported:
            add_uid_flag(mail, uid, FLAG_ARCHIVED)
        return "ignored"

    plan = build_delivery_plan(
        content.subject,
        content.sender,
        content.body,
        content.attachments,
        max_text_messages=settings.max_text_messages,
    )

    for index, text_message in enumerate(plan.messages, start=1):
        marker = _message_marker(index)
        if user_flags_supported and marker in flags:
            continue
        telegram.send_message(text_message)
        if user_flags_supported:
            add_uid_flag(mail, uid, marker)
            flags.add(marker)

    attachment_failures: list[tuple[Attachment, str]] = []
    skip_all_due_to_oversize = any(
        len(attachment.content) > MAX_TELEGRAM_FILE_BYTES
        for attachment in plan.attachments
    )
    for index, attachment in enumerate(plan.attachments, start=1):
        sent_marker = _attachment_marker(index)
        failed_marker = _failed_attachment_marker(index)
        if user_flags_supported and sent_marker in flags:
            continue
        if user_flags_supported and failed_marker in flags:
            attachment_failures.append(
                (
                    attachment,
                    _attachment_failure_reason(
                        attachment,
                        skip_all_due_to_oversize=skip_all_due_to_oversize,
                    ),
                )
            )
            continue

        failure_reason: str | None = None
        if skip_all_due_to_oversize:
            failure_reason = _attachment_failure_reason(
                attachment,
                skip_all_due_to_oversize=True,
            )
        else:
            try:
                telegram.send_document(attachment)
            except TelegramError:
                failure_reason = _attachment_failure_reason(attachment)

        if failure_reason:
            attachment_failures.append((attachment, failure_reason))
            LOGGER.warning(
                "Не удалось доставить вложение %r: %s.",
                attachment.filename,
                failure_reason,
            )
            if user_flags_supported:
                add_uid_flag(mail, uid, failed_marker)
                flags.add(failed_marker)
            continue

        if user_flags_supported:
            add_uid_flag(mail, uid, sent_marker)
            flags.add(sent_marker)

    if attachment_failures and FLAG_ATTACHMENT_WARNING not in flags:
        telegram.send_message(_attachment_warning_message(attachment_failures))
        if user_flags_supported:
            add_uid_flag(mail, uid, FLAG_ATTACHMENT_WARNING)
            flags.add(FLAG_ATTACHMENT_WARNING)

    if user_flags_supported:
        add_uid_flag(mail, uid, FLAG_COMPLETE)
        flags.add(FLAG_COMPLETE)

    moved = move_uid(
        mail, uid, settings.seen_folder, capabilities, user_flags_supported
    )
    if not moved:
        # The source remains in INBOX because this IMAP server does not offer
        # a safe UID-specific deletion operation. Message-ID search prevents
        # the archived copy from being sent again on the next run.
        if user_flags_supported:
            add_uid_flag(mail, uid, FLAG_ARCHIVED)
        LOGGER.warning(
            "Письмо UID %s сохранено в %s; оригинал оставлен в INBOX, чтобы не очищать другие удалённые письма.",
            uid,
            settings.seen_folder,
        )
        return (
            "sent-with-attachment-warning-but-left-in-inbox"
            if attachment_failures
            else "sent-but-left-in-inbox"
        )
    return "sent-with-attachment-warning" if attachment_failures else "sent"


def process_mail(settings: Settings | None = None) -> int:
    settings = settings or Settings.from_env()
    mail: imaplib.IMAP4_SSL | None = None
    telegram = TelegramClient(
        settings.telegram_token,
        settings.telegram_chat_id,
        send_interval=settings.send_interval,
    )
    failed = 0
    processed = 0

    try:
        mail = imaplib.IMAP4_SSL(settings.imap_server, timeout=settings.imap_timeout)
        status, _ = mail.login(settings.email_user, settings.email_password)
        if not _imap_ok(status):
            raise ImapError("Почтовый сервер отклонил вход.")
        status, _ = mail.select("INBOX")
        if not _imap_ok(status):
            raise ImapError("Не удалось открыть папку INBOX.")

        capabilities = get_capabilities(mail)
        user_flags_supported = supports_user_flags(mail)
        if not user_flags_supported:
            LOGGER.warning(
                "IMAP-сервер не объявил поддержку постоянных пользовательских флагов; при сетевом обрыве возможна повторная отправка последней части."
            )

        ensure_folder(mail, settings.seen_folder)
        ensure_folder(mail, settings.ignored_folder)

        status, data = mail.uid("SEARCH", None, "UNDELETED")
        if not _imap_ok(status):
            raise ImapError("Не удалось получить список писем из INBOX.")
        uids = (data[0] if data else b"").split()
        uids = uids[: settings.max_emails_per_run]

        LOGGER.info("Найдено писем для проверки: %d", len(uids))
        for uid_bytes in uids:
            uid = uid_bytes.decode("ascii", "ignore")
            if not uid:
                continue
            try:
                outcome = process_one(
                    mail,
                    uid,
                    settings,
                    telegram,
                    capabilities,
                    user_flags_supported,
                )
                processed += 1
                LOGGER.info("UID %s: %s", uid, outcome)
            except MailBotError as exc:
                failed += 1
                LOGGER.error("UID %s не обработан: %s", uid, exc)
                # A Telegram/IMAP outage should not trigger repeated sends to
                # the rest of the mailbox during this same workflow run.
                if isinstance(exc, (TelegramError, ImapError)):
                    break
            except Exception as exc:  # Keep secrets and full mail out of logs.
                failed += 1
                LOGGER.error("UID %s: внутренняя ошибка %s.", uid, type(exc).__name__)

        LOGGER.info("Проверено: %d; ошибок: %d", processed, failed)
        return 1 if failed else 0
    except (imaplib.IMAP4.error, OSError, URLError) as exc:
        LOGGER.error("Ошибка соединения: %s.", type(exc).__name__)
        return 1
    except MailBotError as exc:
        LOGGER.error("Бот остановлен: %s", exc)
        return 1
    finally:
        if mail is not None:
            try:
                mail.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return process_mail()
    except ConfigurationError as exc:
        LOGGER.error("Ошибка настройки: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
