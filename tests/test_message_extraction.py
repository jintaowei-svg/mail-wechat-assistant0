"""Offline MIME parsing regressions using fictional messages only."""
from email.message import EmailMessage
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailcore import extract_message, heuristic_assess


class MessageExtractionTests(unittest.TestCase):
    def test_attached_eml_does_not_replace_html_body_or_raise_priority(self):
        mail = EmailMessage()
        mail["Subject"] = "普通通知"
        mail.set_content("<p>这是当前邮件正文</p>", subtype="html")
        attached = EmailMessage()
        attached.set_content("付款失败，请立即处理附件中的旧订单")
        mail.add_attachment(attached, filename="old-message.eml")

        result = extract_message(mail.as_bytes())

        self.assertEqual(result.body, "这是当前邮件正文")
        self.assertEqual(heuristic_assess(result, {}).priority, "ignore")

    def test_attached_multipart_subtree_is_skipped(self):
        mail = EmailMessage()
        mail.set_content("当前正文")
        attached = EmailMessage()
        attached.set_content("附件中的纯文本")
        attached.add_alternative("<p>附件中的 HTML</p>", subtype="html")
        attached.add_header("Content-Disposition", "attachment", filename="bundle.mime")
        mail.make_mixed()
        mail.attach(attached)

        self.assertEqual(extract_message(mail.as_bytes()).body, "当前正文")

    def test_attachment_only_message_has_no_body(self):
        attached = EmailMessage()
        attached.set_content("这段文字只存在于附件")
        mail = EmailMessage()
        mail.add_attachment(attached, filename="message.eml")

        self.assertEqual(extract_message(mail.as_bytes()).body, "")

    def test_nested_body_alternatives_and_inline_text_are_preserved(self):
        mail = EmailMessage()
        mail.set_content("正文纯文本")
        mail.add_alternative("<p>正文 HTML 副本</p>", subtype="html")
        mail.add_attachment("附加纯文本", subtype="plain", filename="notes.txt")
        inline = EmailMessage()
        inline.set_content("内联补充")
        inline.add_header("Content-Disposition", "inline")
        mail.attach(inline)

        self.assertEqual(extract_message(mail.as_bytes()).body, "正文纯文本 内联补充")


if __name__ == "__main__":
    unittest.main()
