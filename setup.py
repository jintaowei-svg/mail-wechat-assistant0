"""Interactive setup: QQ, 163 and XJTU school IMAP credentials (local only)."""
from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import re
import stat
import tempfile

ROOT = Path(__file__).resolve().parent


def ask_email(label: str, suffix: str) -> str:
    while True:
        address = input(f"{label}地址（例如 user{suffix}；留空跳过）：").strip()
        if not address:
            return ""
        if re.fullmatch(r"[^\s@]+@[^\s@]+", address) and address.lower().endswith(suffix):
            return address
        print(f"请填写完整的 {suffix} 邮箱地址。")


def ask_secret(label: str) -> str:
    while True:
        value = getpass(f"{label}专用授权码（输入时不显示）：").strip()
        if value and not any(c.isspace() for c in value) and "\n" not in value:
            return value
        print("凭据不能为空或含空格；如显示为分组形式，请去掉组间空格。")


def secure_write(path: Path, content: str) -> None:
    """Private atomic replacement; never follow a symlink."""
    if path.is_symlink():
        raise RuntimeError(f"拒绝读写符号链接：{path.name}")
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def env_content(data: dict[str, str]) -> str:
    for key, value in data.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key) or "\n" in value or "\r" in value:
            raise ValueError("发现非法环境变量名或包含换行的凭据")
    return "# 本文件包含私密凭据，请勿上传/转发/截图\n" + "\n".join(
        f"{k}={v}" for k, v in data.items()) + "\n"


def update_env_secret(path: Path, key: str, value: str) -> None:
    if path.is_symlink():
        raise RuntimeError("拒绝读取 .env 符号链接")
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    lines = original.splitlines()
    found = False
    output = []
    for line in lines:
        if re.match(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=", line):
            if not found:
                output.append(f"{key}={value}")
                found = True
            # Avoid duplicate values for a single environment variable.
        else:
            output.append(line)
    if not found:
        output.append(f"{key}={value}")
    secure_write(path, "\n".join(output) + "\n")


def init_all() -> None:
    print("\nQQ + 163 + 西安交大学生邮箱初始化（密码仅保存在你的本机 .env）\n")
    print("QQ/163 请准备邮箱授权码；西交学生邮箱请在安全设置中生成 16 位客户端专用密码。")
    qq = ask_email("QQ", "@qq.com")
    qq_secret = ask_secret("QQ 邮箱") if qq else ""
    netease = ask_email("163", "@163.com")
    netease_secret = ask_secret("163 邮箱") if netease else ""
    xjtu = ask_email("西安交大学生邮箱", "@stu.xjtu.edu.cn")
    xjtu_secret = ask_secret("西交学生邮箱") if xjtu else ""
    if not (qq or netease or xjtu):
        raise ValueError("至少填写一个邮箱")
    sendkey = getpass("Server酱 Turbo SendKey（SCT 开头；留空稍后配置）：").strip()
    if sendkey and (not sendkey.startswith("SCT") or any(c.isspace() for c in sendkey)):
        raise ValueError("SendKey 格式不正确；Turbo 版应以 SCT 开头")

    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    by_provider = {account["provider"]: account for account in cfg["accounts"]}
    values = {"qq": qq, "163": netease, "xjtu_student": xjtu}
    for provider, addr in values.items():
        by_provider[provider]["enabled"] = bool(addr)
        if addr:
            by_provider[provider]["username"] = addr
    env = {
        "NOTIFIER": "serverchan",
        "SERVERCHAN_SENDKEY": sendkey,
        "QQ_MAIL_AUTH_CODE": qq_secret,
        "NETEASE_MAIL_AUTH_CODE": netease_secret,
        "XJTU_MAIL_APP_PASSWORD": xjtu_secret,
        "TIMEZONE": "Asia/Shanghai",
        "POLL_SECONDS": "300",
        "DIGEST_TIME": "19:00",
        "AUTH_RETRY_SECONDS": "3600",
        "DAILY_PUSH_LIMIT": "5",
        "FIRST_RUN_HOURS": "24",
        "MAX_SCAN_PER_ACCOUNT": "150",
        "MAX_MAIL_BYTES": "262144",
        "DB_PATH": "data/mail_assistant.sqlite3",
    }
    secure_write(ROOT / ".env", env_content(env))
    secure_write(ROOT / "config.json", json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    print("\n已创建 .env / config.json。请运行 python main.py --doctor → --check-auth → --test-push。")


def add_xjtu() -> None:
    """Preserve existing QQ/163 settings when adding school email later."""
    config_path = ROOT / "config.json"
    env_path = ROOT / ".env"
    if not config_path.is_file() or not env_path.is_file():
        raise ValueError("增量模式需要现有的 config.json 和 .env；首次配置请直接运行 python setup.py")
    if config_path.is_symlink() or env_path.is_symlink():
        raise RuntimeError("拒绝读取符号链接配置")
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    template = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    default = next(a for a in template["accounts"] if a["provider"] == "xjtu_student")
    xjtu = ask_email("西安交大学生邮箱", "@stu.xjtu.edu.cn")
    if not xjtu:
        print("未提供学校邮箱，原配置保持不变。")
        return
    secret = ask_secret("西交学生邮箱")
    matches = [a for a in cfg.get("accounts", []) if a.get("provider") == "xjtu_student" or
               a.get("host") in ("stu.xjtu.edu.cn", "imap.stu.xjtu.edu.cn")]
    if len(matches) > 1:
        raise ValueError("config.json 中发现多个学校邮箱，请手动检查后再运行")
    if matches:
        account = matches[0]
        account.update({"enabled": True, "provider": "xjtu_student", "username": xjtu,
                        "password_env": "XJTU_MAIL_APP_PASSWORD"})
        account.setdefault("rules", default["rules"])
    else:
        account = default.copy()
        account.update({"enabled": True, "username": xjtu})
        cfg.setdefault("accounts", []).append(account)
    # Credentials first: if interrupted before config write the previous mailboxes still work.
    update_env_secret(env_path, "XJTU_MAIL_APP_PASSWORD", secret)
    secure_write(config_path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    print("已加入西安交大学生邮箱，QQ/163 的账号和已有规则保持不变。")
    print("下一步运行：python main.py --doctor 与 python main.py --check-auth")


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ + 163 + 西安交大学生邮箱微信助手初始化")
    parser.add_argument("--force", action="store_true", help="首次初始化时允许覆盖现有 .env/config.json；谨慎使用")
    parser.add_argument("--add-xjtu", action="store_true", help="在现有 QQ/163 设置上增加学生邮箱，不覆盖其他账号")
    args = parser.parse_args()
    if args.add_xjtu:
        if args.force:
            parser.error("--force 与 --add-xjtu 不可同时使用")
        add_xjtu()
        return
    if not args.force and any((ROOT / file).exists() for file in (".env", "config.json")):
        parser.error("发现已有 .env/config.json；若只加入学校邮箱，请用 --add-xjtu；谨慎使用 --force 全量重置")
    init_all()


if __name__ == "__main__":
    main()
