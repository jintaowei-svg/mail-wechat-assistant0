"""No real credentials or network calls. Covers three-provider onboarding."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import providers
import setup
from mailcore import MailMessage, heuristic_assess


class XJTUProviderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "enabled": True, "provider": "xjtu_student", "name": "学校邮箱",
            "host": "stu.xjtu.edu.cn", "port": 993,
            "username": "student@stu.xjtu.edu.cn",
            "password_env": "XJTU_MAIL_APP_PASSWORD", "folder": "INBOX",
        }

    def test_official_host_and_credential_shape(self):
        self.assertEqual(providers.provider_for(self.cfg), "xjtu_student")
        self.assertEqual(providers.provider_for({"host": "stu.xjtu.edu.cn"}), "xjtu_student")
        self.assertEqual(providers.validate_account(self.cfg), [])
        other = dict(self.cfg, host="malicious.example.org")
        self.assertTrue(providers.validate_account(other))
        other = dict(self.cfg, port=143)
        self.assertTrue(providers.validate_account(other))
        other = dict(self.cfg, username="student@xjtu.edu.cn")
        self.assertTrue(providers.validate_account(other))

    def test_school_uses_client_specific_password_not_163_id(self):
        class FakeClient:
            calls = []
            def login(self, user, password):
                self.calls.append((user, password))
                return "OK", []
        client = FakeClient()
        with patch.dict(os.environ, {"XJTU_MAIL_APP_PASSWORD": "A1b2C3d4E5f6G7h8"}):
            providers.imap_login(client, self.cfg)
        self.assertEqual(client.calls, [("student@stu.xjtu.edu.cn", "A1b2C3d4E5f6G7h8")])

    def test_school_login_error_is_helpful_without_exposing_secrets(self):
        hint = providers.auth_hint(providers.imaplib.IMAP4.error("private string"), self.cfg)
        self.assertIn("客户端专用密码", hint)
        self.assertNotIn("private string", hint)

    def test_per_account_rules_preserve_global_and_are_scoped(self):
        global_rules = {"important_keywords": ["合同"], "urgent_keywords": ["付款失败"]}
        school = dict(self.cfg, rules={"important_keywords": ["奖学金", "教务处"],
                                       "urgent_keywords": ["选课截止"]})
        combined = main.effective_rules(global_rules, school)
        self.assertIn("奖学金", combined["important_keywords"])
        self.assertIn("合同", combined["important_keywords"])
        self.assertEqual(global_rules, {"important_keywords": ["合同"], "urgent_keywords": ["付款失败"]})
        mail = MailMessage("教务", "notice@stu.xjtu.edu.cn", "奖学金结果", "请尽快查看", None)
        self.assertEqual(heuristic_assess(mail, combined).priority, "important")
        self.assertEqual(heuristic_assess(mail, global_rules).priority, "ignore")
        urgent = MailMessage("教务", "notice@stu.xjtu.edu.cn", "选课截止", "请处理", None)
        self.assertEqual(heuristic_assess(urgent, combined).priority, "urgent")

    def test_netease_special_id_kept_for_163(self):
        class FakeClient:
            def __init__(self):
                self.login_calls = []
                self.id_calls = []
            def login(self, user, secret):
                self.login_calls.append((user, secret))
                return "OK", []
            def _simple_command(self, action, value):
                self.id_calls.append((action, value))
                return "OK", []
        account = {"provider": "163", "username": "name@163.com",
                   "password_env": "NETEASE_MAIL_AUTH_CODE"}
        client = FakeClient()
        with patch.dict(os.environ, {"NETEASE_MAIL_AUTH_CODE": "fake-nonsecret"}):
            providers.imap_login(client, account)
        self.assertEqual(client.login_calls, [("name@163.com", "fake-nonsecret")])
        self.assertEqual(client.id_calls[0][0], "ID")
        self.assertIn("MailWechatAssistant", client.id_calls[0][1])

    def test_three_mailbox_first_run_setup(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.example.json").write_text(
                (Path(setup.__file__).parent / "config.example.json").read_text(encoding="utf-8"),
                encoding="utf-8")
            accounts = iter(["123456@qq.com", "person@163.com", "student@stu.xjtu.edu.cn"])
            credentials = iter(["fake-qq-secret", "fake-163-secret", "fake-school-secret", "SCTdemoSendKey"])
            with patch.object(setup, "ROOT", root), patch.object(setup, "ask_email", side_effect=lambda *args: next(accounts)), patch.object(setup, "ask_secret", side_effect=lambda *args: next(credentials)), patch.object(setup, "getpass", side_effect=lambda *args: next(credentials)):
                setup.init_all()
            content = (root / "config.json").read_text(encoding="utf-8")
            config = json.loads(content)
            self.assertEqual({a["provider"] for a in config["accounts"] if a["enabled"]},
                             {"qq", "163", "xjtu_student"})
            self.assertIn("XJTU_MAIL_APP_PASSWORD=fake-school-secret", (root / ".env").read_text(encoding="utf-8"))
            self.assertNotIn("fake-school-secret", content)
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)

    def test_incremental_setup_preserves_qq_and_163(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = json.loads((Path(setup.__file__).parent / "config.example.json").read_text(encoding="utf-8"))
            original = {"accounts": template["accounts"][:2], "rules": {"vip_senders": ["friend@example.org"]}}
            original["accounts"][0].update({"enabled": True, "username": "123456@qq.com"})
            original["accounts"][1].update({"enabled": True, "username": "mine@163.com"})
            (root / "config.example.json").write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
            (root / "config.json").write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            (root / ".env").write_text("QQ_MAIL_AUTH_CODE=qq-secret\nNETEASE_MAIL_AUTH_CODE=163-secret\n", encoding="utf-8")
            with patch.object(setup, "ROOT", root), patch.object(setup, "ask_email", return_value="student@stu.xjtu.edu.cn"), patch.object(setup, "ask_secret", return_value="school-client-secret"):
                setup.add_xjtu()
            updated = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["accounts"][:2], original["accounts"])
            self.assertEqual(updated["rules"], original["rules"])
            self.assertEqual(updated["accounts"][2]["host"], "stu.xjtu.edu.cn")
            self.assertTrue(updated["accounts"][2]["enabled"])
            env = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("QQ_MAIL_AUTH_CODE=qq-secret", env)
            self.assertIn("NETEASE_MAIL_AUTH_CODE=163-secret", env)
            self.assertIn("XJTU_MAIL_APP_PASSWORD=school-client-secret", env)
            if os.name == "posix":
                self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
            # Subsequent runs update the school account, without duplicate school records.
            with patch.object(setup, "ROOT", root), patch.object(setup, "ask_email", return_value="new@stu.xjtu.edu.cn"), patch.object(setup, "ask_secret", return_value="new-school-secret"):
                setup.add_xjtu()
            updated = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(len(updated["accounts"]), 3)
            self.assertEqual(updated["accounts"][2]["username"], "new@stu.xjtu.edu.cn")
            self.assertEqual((root / ".env").read_text(encoding="utf-8").count("XJTU_MAIL_APP_PASSWORD="), 1)


if __name__ == "__main__":
    unittest.main()
