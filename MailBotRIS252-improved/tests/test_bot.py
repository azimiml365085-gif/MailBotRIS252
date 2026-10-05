import unittest
import json
from email.message import EmailMessage
from unittest.mock import patch

from bot import (
    Attachment,
    ImapError,
    TelegramClient,
    TelegramError,
    Settings,
    build_delivery_plan,
    extract_mail_content,
    html_to_text,
    move_uid,
    process_one,
    split_text,
    utf16_units,
)


class HtmlAndMimeTests(unittest.TestCase):
    def test_raw_html_in_plain_part_becomes_readable_text(self):
        msg = EmailMessage()
        msg["From"] = "Учебный офис <office@example.org>"
        msg["Subject"] = "=?utf-8?b?0KHQvtC70YzQvdC+0L3QvtCy?="
        msg["Message-ID"] = "<raw-html@example.org>"
        msg.set_content(
            '<div><p><b>Уважаемые студенты!</b></p>'
            '<p>Важная информация <a href="https://example.org/info">по ссылке</a>.</p>'
            '<ul><li>Первый пункт</li><li>Второй пункт</li></ul>'
            '<div style="display: none">скрытый текст</div>'
            '<style>.secret { display:none }</style></div>'
        )

        content = extract_mail_content(msg)

        self.assertIn("Уважаемые студенты!", content.body)
        self.assertIn("Важная информация", content.body)
        self.assertIn("https://example.org/info", content.body)
        self.assertIn("• Первый пункт", content.body)
        self.assertNotIn("<div", content.body)
        self.assertNotIn("display:none", content.body)
        self.assertNotIn("скрытый текст", content.body)

    def test_html_renderer_ignores_unsafe_link_schemes_and_styles(self):
        rendered = html_to_text(
            '<div>Текст <a href="javascript:alert(1)">ссылка</a></div>'
            '<style>body { color: red }</style><script>ignore()</script>'
        )
        self.assertIn("Текст ссылка", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("color: red", rendered)
        self.assertNotIn("ignore()", rendered)

    def test_plain_part_is_preferred_over_html_alternative(self):
        msg = EmailMessage()
        msg["From"] = "Sender <sender@example.org>"
        msg["Subject"] = "Test"
        msg.set_content("Обычный текст письма")
        msg.add_alternative("<p>HTML-версия</p>", subtype="html")

        content = extract_mail_content(msg)

        self.assertEqual(content.body, "Обычный текст письма")
        self.assertNotIn("HTML-версия", content.body)

    def test_attachment_filename_and_bytes_are_preserved(self):
        msg = EmailMessage()
        msg["From"] = "sender@example.org"
        msg["Subject"] = "File"
        msg.set_content("Вложение")
        msg.add_attachment(
            b"sample bytes",
            maintype="application",
            subtype="pdf",
            filename="report.pdf",
        )

        content = extract_mail_content(msg)

        self.assertEqual(len(content.attachments), 1)
        self.assertEqual(content.attachments[0].filename, "report.pdf")
        self.assertEqual(content.attachments[0].content, b"sample bytes")


class LongTextTests(unittest.TestCase):
    def test_split_is_finite_and_preserves_every_character(self):
        source = ("я🙂" * 120_000) + "конец"
        chunks = split_text(source, max_units=3000)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), source)
        self.assertTrue(all(utf16_units(part) <= 3000 for part in chunks))

    def test_long_body_is_sent_once_as_a_complete_text_file(self):
        source = ("Абзац со ссылкой https://example.org. " * 1500).strip()
        plan = build_delivery_plan(
            "Длинное письмо",
            "office@example.org",
            source,
            (),
            max_text_messages=4,
        )
        self.assertEqual(len(plan.messages), 1)
        self.assertEqual(plan.attachments[0].filename, "email_body.txt")
        self.assertEqual(plan.attachments[0].content.decode("utf-8"), source)
        self.assertIn("полный текст отправлен файлом", plan.messages[0])

    def test_text_messages_stay_within_safe_budget(self):
        source = "слово " * 1800
        plan = build_delivery_plan("Тема", "Имя <a@example.org>", source, ())
        self.assertLessEqual(len(plan.messages), 8)
        for message in plan.messages:
            # Markup is ignored here; the body itself is bounded below the API limit.
            self.assertLess(utf16_units(message), 4096)


class TelegramAndImapSafetyTests(unittest.TestCase):
    def test_telegram_ok_false_is_treated_as_failure(self):
        class Response:
            status = 400

            @staticmethod
            def read():
                return json.dumps(
                    {"ok": False, "description": "Bad Request: chat not found"}
                ).encode("utf-8")

        class Opener:
            @staticmethod
            def __call__(*args, **kwargs):
                return Response()

        client = TelegramClient(
            "secret-token", "-100123", send_interval=0, opener=Opener(), sleep=lambda _: None
        )
        with self.assertRaisesRegex(TelegramError, "chat not found"):
            client.send_message("hello")

    def test_429_uses_retry_after_and_retries_only_a_bounded_number(self):
        class Response:
            def __init__(self, status_code, payload):
                self.status = status_code
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        class Opener:
            def __init__(self):
                self.responses = [
                    Response(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 2}}),
                    Response(200, {"ok": True, "result": {"message_id": 42}}),
                ]
                self.calls = 0

            def __call__(self, *args, **kwargs):
                self.calls += 1
                return self.responses.pop(0)

        delays = []
        opener = Opener()
        client = TelegramClient(
            "secret-token",
            "-100123",
            send_interval=0,
            opener=opener,
            sleep=delays.append,
        )

        result = client.send_message("hello")

        self.assertEqual(result["message_id"], 42)
        self.assertEqual(opener.calls, 2)
        self.assertIn(2, delays)

    def test_failed_copy_never_marks_source_for_deletion(self):
        class FakeMail:
            def __init__(self):
                self.calls = []

            def uid(self, command, *args):
                self.calls.append((command, args))
                return "NO", [b"copy failed"]

        mail = FakeMail()
        with self.assertRaisesRegex(ImapError, "оригинал сохранён"):
            move_uid(mail, "123", "SeenByBot", {"UIDPLUS"}, True)
        self.assertEqual([command for command, _ in mail.calls], ["COPY"])

    def test_without_uidplus_it_marks_only_this_uid_and_never_global_expunge(self):
        class FakeMail:
            def __init__(self):
                self.calls = []

            def uid(self, command, *args):
                self.calls.append((command, args))
                return "OK", [b"ok"]

        mail = FakeMail()
        moved = move_uid(mail, "123", "SeenByBot", set(), False)
        self.assertFalse(moved)
        self.assertEqual([command for command, _ in mail.calls], ["COPY", "STORE"])
        self.assertIn("(\\Deleted)", mail.calls[1][1])

    def test_telegram_failure_does_not_archive_or_delete_source(self):
        class FakeMail:
            def __init__(self):
                self.calls = []

            def select(self, *args, **kwargs):
                self.calls.append(("SELECT", args))
                return "OK", [b"1"]

            def uid(self, command, *args):
                self.calls.append((command, args))
                if command == "SEARCH":
                    return "OK", [b""]
                self.fail(f"Unexpected IMAP command: {command}")

            def fail(self, message):
                raise AssertionError(message)

        class FailingTelegram:
            @staticmethod
            def send_message(_text):
                raise TelegramError("Telegram отклонил отправку.")

        msg = EmailMessage()
        msg["From"] = "sender@example.org"
        msg["Subject"] = "Test"
        msg.set_content("Hello")
        settings = Settings(
            imap_server="imap.example.org",
            email_user="user@example.org",
            email_password="placeholder",
            telegram_token="placeholder",
            telegram_chat_id="-100123",
            skip_senders=(),
        )
        mail = FakeMail()

        with patch("bot.fetch_message", return_value=(msg, set())):
            with self.assertRaises(TelegramError):
                process_one(mail, "123", settings, FailingTelegram(), set(), False)

        self.assertNotIn("COPY", [command for command, _ in mail.calls])
        self.assertNotIn("MOVE", [command for command, _ in mail.calls])
        self.assertNotIn("STORE", [command for command, _ in mail.calls])


if __name__ == "__main__":
    unittest.main()
