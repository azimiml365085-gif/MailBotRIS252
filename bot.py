import imaplib
import email
import requests
import html
import time
import traceback
import os
from io import BytesIO
from email.header import decode_header
from email.utils import parseaddr
from bs4 import BeautifulSoup

# ===== НАСТРОЙКИ (из переменных окружения) =====
IMAP_SERVER = "imap.mail.ru"

EMAIL_ACCOUNT = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SKIP_SENDER = "security@id.mail.ru"
SEEN_FOLDER = "SeenByBot"
# ===============================================

def decode_mime_words(s):
    decoded = ""
    for word, enc in decode_header(s or ""):
        if isinstance(word, bytes):
            decoded += word.decode(enc or "utf-8", errors="ignore")
        else:
            decoded += word
    return decoded.strip()


def clean_text(text):
    return html.escape(text or "")


def html_to_text(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text(separator="\n")


def send_text_to_telegram(subject, sender, body):
    text = (
        f"<b>Новое письмо</b>\n"
        f"<b>От:</b> {clean_text(sender)}\n"
        f"<b>Тема:</b> {clean_text(subject)}\n\n"
        f"<blockquote>{clean_text(body)}</blockquote>"
    )

    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30
    )


def send_file_to_telegram(filename, file_bytes):
    # Создаем объект BytesIO из байтов файла
    file_io = BytesIO(file_bytes)
    file_io.name = filename  # Даем имя файлу

    # Отправляем файл как документ
    files = {"document": (filename, file_io, "application/octet-stream")}
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data={"chat_id": CHAT_ID},
        files=files,
        timeout=60
    )


def extract_sender(msg):
    raw_from = msg.get("From", "")
    name, email_addr = parseaddr(raw_from)
    name = decode_mime_words(name) if name else email_addr
    return f"{name} <{email_addr}>"


def process_mail():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("INBOX")

    status, messages = mail.search(None, "ALL")
    mail_ids = messages[0].split()

    for mail_id in mail_ids:
        status, msg_data = mail.fetch(mail_id, "(RFC822)")
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_words(msg.get("Subject", "Без темы"))
        sender = extract_sender(msg)

        if SKIP_SENDER.lower() in sender.lower():
            continue

        body = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition") or "")

                if content_type == "text/plain" and "attachment" not in content_disposition:
                    charset = part.get_content_charset() or "utf-8"
                    body += part.get_payload(decode=True).decode(charset, errors="ignore")

                elif content_type == "text/html" and not body:
                    charset = part.get_content_charset() or "utf-8"
                    html_body = part.get_payload(decode=True).decode(charset, errors="ignore")
                    body = html_to_text(html_body)

                elif "attachment" in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        filename = decode_mime_words(filename)
                        attachments.append((filename, part.get_payload(decode=True)))
        else:
            charset = msg.get_content_charset() or "utf-8"
            body = msg.get_payload(decode=True).decode(charset, errors="ignore")

        if not body.strip():
            body = "[Письмо без текстового содержимого]"

        send_text_to_telegram(subject, sender, body)

        for filename, file_bytes in attachments:
            send_file_to_telegram(filename, file_bytes)

        mail.copy(mail_id, SEEN_FOLDER)
        mail.store(mail_id, "+FLAGS", "\\Deleted")

    mail.expunge()
    mail.logout()


if __name__ == "__main__":
    print("Приступаю к работе, босс!")
    try:
        process_mail()
        print("✅ Работа успешно завершена!")
    except Exception as e:
        print("❌ Ошибка:", e)
        traceback.print_exc()
        exit(1)
