"""Provider-specific IMAP configuration for QQ, 163 and XJTU Coremail.

Only read-only IMAP is used; no SMTP, no account-password reset and no
web-browser/single-sign-on automation. Credentials stay in environment vars.
"""
from __future__ import annotations

import imaplib
import os
import re
import socket
import ssl

PROFILES = {
    "qq": {
        "host": "imap.qq.com", "hosts": ("imap.qq.com",),
        "port": 993, "password_env": "QQ_MAIL_AUTH_CODE",
        "credential_type": "QQ 邮箱 IMAP 授权码",
    },
    "163": {
        "host": "imap.163.com", "hosts": ("imap.163.com",),
        "port": 993, "password_env": "NETEASE_MAIL_AUTH_CODE",
        "credential_type": "网易 163 邮箱客户端授权码",
    },
    "xjtu_student": {
        # University IT docs specify stu.xjtu.edu.cn for IMAP over SSL:993.
        # A newer teaching slide also lists imap.stu.xjtu.edu.cn.
        "host": "stu.xjtu.edu.cn",
        "hosts": ("stu.xjtu.edu.cn", "imap.stu.xjtu.edu.cn"),
        "port": 993, "password_env": "XJTU_MAIL_APP_PASSWORD",
        "credential_type": "西安交大学生邮箱 16 位客户端专用密码",
    },
}

IMAP_ID = '("name" "MailWechatAssistant" "version" "3.0" "vendor" "SelfHosted")'


def provider_for(account: dict) -> str:
    """Infer known providers from host for backward-compatible v1 configs."""
    provider = str(account.get("provider", "")).strip().lower()
    if provider:
        return provider
    host = str(account.get("host", "")).strip().lower()
    return next((key for key, spec in PROFILES.items() if host in spec["hosts"]), "custom")


def validate_account(account: dict) -> list[str]:
    errors: list[str] = []
    provider = provider_for(account)
    name = str(account.get("name", "")).strip()
    host = str(account.get("host", "")).strip().lower()
    username = str(account.get("username", "")).strip()
    secret_var = str(account.get("password_env", "")).strip()
    try:
        port = int(account.get("port", 993))
    except (TypeError, ValueError):
        port = 0
    if not name:
        errors.append("name 不可为空")
    if provider not in (*PROFILES.keys(), "custom"):
        errors.append("provider 必须为 qq、163、xjtu_student 或 custom")
    if provider in PROFILES:
        expected = PROFILES[provider]
        if host not in expected["hosts"]:
            errors.append(f"{provider} 的 IMAP 服务器应为 {expected['host']}")
        if port != 993:
            errors.append("QQ/163/西交学生邮箱仅允许 SSL/TLS 端口 993")
    else:
        if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
            errors.append("请填写有效的 IMAP 主机名")
        if not 1 <= port <= 65535:
            errors.append("port 必须在 1 到 65535 之间")
    if not username or not re.fullmatch(r"[^\s@]+@[^\s@]+", username):
        errors.append("username 必须是完整邮箱地址")
    if provider == "163" and not username.lower().endswith("@163.com"):
        errors.append("163 个人邮箱请使用 xxx@163.com（企业邮箱需单独配置）")
    if provider == "xjtu_student" and not username.lower().endswith("@stu.xjtu.edu.cn"):
        errors.append("西交学生邮箱请使用网页版显示的完整 xxx@stu.xjtu.edu.cn 地址；特殊别名请咨询网信中心")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", secret_var):
        errors.append("password_env 必须是环境变量名")
    if not account.get("folder", "INBOX"):
        errors.append("folder 不可为空")
    return errors


def secret_exists(account: dict) -> bool:
    key = account.get("password_env", "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", str(key)):
        return False
    value = os.getenv(key, "").strip()
    return bool(value) and not any(marker in value.lower() for marker in
                                   ("替换", "在此输入", "your_", "example", "placeholder", "填入"))


def send_netease_imap_id(client: imaplib.IMAP4_SSL) -> None:
    """Send RFC 2971 IMAP ID as required by some NetEase 163 accounts.

    Python's stdlib imaplib exposes no public ID method. Only the command-state
    registration and helper call below use a private API. The ID describes
    this actual self-hosted app; it does not impersonate another mail client.
    """
    imaplib.Commands.setdefault("ID", ("AUTH",))
    typ, _ = client._simple_command("ID", IMAP_ID)
    if typ != "OK":
        raise RuntimeError("163 IMAP ID 握手被拒绝，请检查客户端安全设置")


def imap_login(client: imaplib.IMAP4_SSL, account: dict) -> None:
    var = account["password_env"]
    if not secret_exists(account):
        raise ValueError(f"缺少有效的邮箱客户端凭据：请设置 {var}")
    secret = os.environ[var].strip()
    client.login(account["username"].strip(), secret)
    if provider_for(account) == "163":
        send_netease_imap_id(client)


def auth_hint(exc: Exception, account: dict) -> str:
    """Actionable diagnostic without printing server responses or secrets."""
    provider = provider_for(account)
    msg = str(exc).lower()
    if "unsafe login" in msg:
        if provider == "163":
            return "网易 163 拦截 Unsafe Login：确认授权码、客户端安全登录及 IMAP ID 握手；勿频繁尝试。"
        return "邮箱拦截了客户端登录：检查专用密码、IMAP 服务及账号安全策略。"
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return "TLS 证书验证失败：检查系统时间、服务器地址和 CA 证书，不要关闭证书校验。"
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, OSError)):
        return "无法连接 IMAP：检查 DNS、外网访问、993 端口和校园网/防火墙限制。"
    if isinstance(exc, imaplib.IMAP4.error) or any(
            x in msg for x in ("auth", "login", "credential", "password")):
        if provider == "xjtu_student":
            return "学校邮箱认证失败：需在 stu.xjtu.edu.cn 的『设置→安全设置→客户端安全登录』生成客户端专用密码；不要用网页登录密码或 NetID 密码。"
        return "邮箱认证失败：检查完整地址、IMAP 服务与邮箱授权码；不要使用网页登录密码。"
    if "uidvalidity" in msg or "status" in msg or "select" in msg:
        return "已连接但无法读取收件箱：检查 IMAP 是否开放及 INBOX 文件夹权限。"
    return f"邮箱检查失败（{type(exc).__name__}）；请检查配置、网络或学校安全策略。"
