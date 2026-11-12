import sys
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailcore import MailMessage, extract_message, heuristic_assess, redact, render_alerts
from main import db_init, _update_cursor, _cursor, scan_account, flush_pushes
from unittest.mock import patch
from tempfile import TemporaryDirectory
import sqlite3
import os


class ClassificationTests(unittest.TestCase):
    def test_urgent(self):
        msg = MailMessage("a", "a@b.com", "付款失败，请立即处理", "订单无法完成", None)
        self.assertEqual(heuristic_assess(msg, {}).priority, "urgent")

    def test_vip(self):
        msg = MailMessage("boss", "boss@abc.com", "你好", "这是常规内容", None)
        self.assertEqual(heuristic_assess(msg, {"vip_senders": ["boss@abc.com"]}).priority, "important")

    def test_ad_ignore(self):
        msg = MailMessage("sales", "news@a.com", "限时优惠促销", "折扣快报", None)
        self.assertEqual(heuristic_assess(msg, {}).priority, "ignore")

    def test_otp_redacted(self):
        self.assertNotIn("123456", redact("验证码：123456"))
        self.assertNotIn("123456", redact("Your OTP: 123456"))

    def test_raw_message_parse(self):
        e = EmailMessage()
        e["From"] = "Person <person@example.com>"
        e["Subject"] = "请确认合同"
        e["Date"] = "Wed, 11 Nov 2026 10:20:00 +0800"
        e.set_content("请在明天上午 10 点前回复")
        m = extract_message(e.as_bytes())
        self.assertEqual(m.sender_email, "person@example.com")
        self.assertIn("明天", m.body)
        self.assertIsNotNone(m.received_at)

    def test_render(self):
        title, body = render_alerts([{
            "account": "work", "sender": "Tom", "subject": "合同审批",
            "summary": "请审核合同", "action": "回复", "deadline": "明天"
        }], digest=False)
        self.assertIn("紧急邮件", title)
        self.assertIn("明天", body)

    def test_cursor_persists(self):
        con = sqlite3.connect(":memory:")
        # use an actual file-free database with the same schema
        con.executescript("CREATE TABLE cursors(account TEXT, folder TEXT, uidvalidity TEXT, last_uid INTEGER, PRIMARY KEY(account,folder));")
        _update_cursor(con, "QQ", "INBOX", "1234", 42)
        self.assertEqual(_cursor(con, "QQ", "INBOX"), ("1234", 42))
        _update_cursor(con, "QQ", "INBOX", "1234", 43)
        self.assertEqual(_cursor(con, "QQ", "INBOX"), ("1234", 43))
        con.close()


class FakeIMAP:
    mailbox = {}
    instances = []

    def __init__(self, host, port, timeout=None, ssl_context=None):
        self.readonly_used = False
        self.instances.append(self)

    def login(self, username, password):
        assert username and password
        return "OK", []

    def select(self, folder, readonly=False):
        self.readonly_used = readonly
        return "OK", [str(len(self.mailbox)).encode()]

    def status(self, folder, query):
        return "OK", [b'INBOX (UIDVALIDITY 777)']

    def uid(self, action, *args):
        if action == "SEARCH":
            if args[1] == "ALL":
                uids = list(self.mailbox)
            else:
                assert args[1] == "UID"
                start = int(args[2].split(":")[0])
                uids = [uid for uid in self.mailbox if uid >= start]
                # Simulate the IMAP quirk when UID start is past max available.
                if not uids and self.mailbox:
                    uids = [max(self.mailbox)]
            return "OK", [b" ".join(str(i).encode() for i in sorted(uids))]
        if action == "FETCH":
            return "OK", [(b"1 (BODY[] {123}", self.mailbox[int(args[0])]), b")"]
        raise RuntimeError("unsupported mock operation")

    def logout(self):
        pass


def sample(subject, body, date="Wed, 11 Nov 2026 10:20:00 +0800"):
    mail = EmailMessage()
    mail["From"] = "Team <team@example.org>"
    mail["Subject"] = subject
    mail["Date"] = date
    mail.set_content(body)
    return mail.as_bytes()


class ScanAndDeliveryTests(unittest.TestCase):
    def test_invalid_ai_response_does_not_block_later_mail(self):
        current_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        FakeIMAP.mailbox = {
            12: sample("请确认合同", "请回复第一份合同", current_date),
            18: sample("发票请确认", "请审核第二份发票", current_date),
        }
        FakeIMAP.instances = []
        responses = [
            {"choices": [{"message": {"content": "null"}}]},
            {"choices": [{"message": {"content":
                '{"priority":"important","summary":"第二封 AI 摘要","action":"请审核"}'}}]},
        ]
        env = {"TEST_MAIL_PASS": "fake-secret", "AI_MODEL": "fictional-model",
               "AI_BASE_URL": "https://ai.example.invalid/v1", "AI_API_KEY": "fake-key"}
        with TemporaryDirectory() as tmp, patch("main.imaplib.IMAP4_SSL", FakeIMAP), \
                patch.dict(os.environ, env), \
                patch("mailcore._post_json", side_effect=responses) as post:
            con = db_init(Path(tmp) / "state.db")
            try:
                cfg = {"name": "personal", "host": "imap.example.org",
                       "username": "me@example.org", "password_env": "TEST_MAIL_PASS"}
                self.assertEqual(scan_account(con, cfg, {}), (2, 2))
                self.assertEqual(_cursor(con, "personal", "INBOX"), ("777", 18))
                rows = con.execute("SELECT summary FROM messages ORDER BY uid").fetchall()
                self.assertEqual([row["summary"] for row in rows],
                                 ["请回复第一份合同", "第二封 AI 摘要"])
                self.assertEqual(scan_account(con, cfg, {}), (0, 0))
                self.assertEqual(post.call_count, 2)
                self.assertTrue(FakeIMAP.instances[-1].readonly_used)
            finally:
                con.close()

    def test_scan_dedup_increment_and_readonly(self):
        now = datetime.now(timezone.utc)
        current_date = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
        FakeIMAP.mailbox = {
            12: sample("发票请确认", "请确认发票信息", current_date),
            14: sample("优惠券促销", "退订此营销活动", current_date),
            18: sample("付款失败", "请立即处理订单", current_date),
        }
        FakeIMAP.instances = []
        with TemporaryDirectory() as tmp, patch("main.imaplib.IMAP4_SSL", FakeIMAP), patch.dict(os.environ, {"TEST_MAIL_PASS": "test-secret", "AI_MODEL": "", "AI_BASE_URL": ""}):
            con = db_init(Path(tmp) / "state.db")
            cfg = {"name":"personal", "host":"imap.example.org", "username":"me@example.org", "password_env":"TEST_MAIL_PASS"}
            got = scan_account(con, cfg, {}, bootstrap_hours=24)
            self.assertEqual(got, (3, 2))
            self.assertTrue(FakeIMAP.instances[-1].readonly_used)
            self.assertEqual(_cursor(con, "personal", "INBOX"), ("777", 18))
            self.assertEqual(scan_account(con, cfg, {}), (0, 0))
            FakeIMAP.mailbox[19] = sample("客户合同", "请回复合同修改意见", current_date)
            self.assertEqual(scan_account(con, cfg, {}), (1, 1))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
            con.close()

    def test_digest_and_urgent_batch(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"DAILY_PUSH_LIMIT": "5", "DIGEST_TIME": "19:00"}):
            con = db_init(Path(tmp) / "state.db")
            for i, priority in enumerate(("urgent", "important"), 1):
                con.execute("INSERT INTO messages(account,folder,uidvalidity,uid,sender,subject,priority,summary,action,deadline,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("demo", "INBOX", "123", i, "Alice", "合同", priority, "请审核", "请回复", "明天", "pending", "2026-11-11"))
            con.commit()
            with patch("main._now_local", return_value=datetime(2026, 11, 11, 19, 30, tzinfo=timezone.utc)), patch("main.push_message") as push:
                flush_pushes(con)
                self.assertEqual(push.call_count, 2)
                flush_pushes(con)
                self.assertEqual(push.call_count, 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages WHERE status='sent'").fetchone()[0], 2)
            con.close()


if __name__ == "__main__":
    unittest.main()
