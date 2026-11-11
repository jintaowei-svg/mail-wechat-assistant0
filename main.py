"""Multi-mailbox important-email assistant. Python 3.11+; standard library only."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import imaplib
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import time
from zoneinfo import ZoneInfo

from mailcore import (Assessment, MailMessage, ai_assess, extract_message,
                      heuristic_assess, push_message, read_env, render_alerts)
from providers import auth_hint, imap_login, provider_for, secret_exists, validate_account

ROOT = Path(__file__).resolve().parent


def db_init(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        # The DB contains short mail summaries and senders; limit directory access.
        path.parent.chmod(0o700)
    con = sqlite3.connect(path)
    if os.name == "posix":
        path.chmod(0o600)
    con.row_factory = sqlite3.Row
    con.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS cursors (
      account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
      last_uid INTEGER NOT NULL, PRIMARY KEY(account, folder)
    );
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL, uid INTEGER NOT NULL,
      sender TEXT NOT NULL, subject TEXT NOT NULL, priority TEXT NOT NULL,
      summary TEXT NOT NULL, action TEXT NOT NULL, deadline TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
      UNIQUE(account, folder, uidvalidity, uid)
    );
    CREATE TABLE IF NOT EXISTS pushes (
      id INTEGER PRIMARY KEY AUTOINCREMENT, local_day TEXT NOT NULL,
      kind TEXT NOT NULL, sent_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS digest_days (
      local_day TEXT PRIMARY KEY, checked_at TEXT NOT NULL
    );
    """)
    return con


def _cursor(con: sqlite3.Connection, account: str, folder: str) -> tuple[str, int] | None:
    row = con.execute("SELECT uidvalidity,last_uid FROM cursors WHERE account=? AND folder=?", (account, folder)).fetchone()
    return (row[0], int(row[1])) if row else None


def _update_cursor(con: sqlite3.Connection, account: str, folder: str, uidvalidity: str, uid: int) -> None:
    con.execute("""INSERT INTO cursors(account,folder,uidvalidity,last_uid) VALUES(?,?,?,?)
      ON CONFLICT(account,folder) DO UPDATE SET uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid""",
      (account, folder, uidvalidity, uid))


def _uidvalidity(client: imaplib.IMAP4_SSL, folder: str) -> str:
    typ, data = client.status(folder, "(UIDVALIDITY)")
    if typ != "OK" or not data:
        raise RuntimeError("无法获取 IMAP UIDVALIDITY")
    match = re.search(rb"\bUIDVALIDITY\s+(\d+)", data[0], re.I)
    if not match:
        raise RuntimeError("邮件服务器未返回 UIDVALIDITY")
    return match.group(1).decode("ascii")


def _fetch_raw(client: imaplib.IMAP4_SSL, uid: int) -> bytes:
    # Limit transferred data, especially for emails with huge attachments.
    # In oversized MIME emails only the first portion is classified.
    max_bytes = max(16_384, min(int(os.getenv("MAX_MAIL_BYTES", "262144")), 2_097_152))
    typ, data = client.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{max_bytes}>)")
    if typ != "OK" or not data:
        raise RuntimeError(f"无法读取 UID={uid}")
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError(f"无法解析 UID={uid} 的邮件内容")


def _open_mailbox(cfg: dict) -> imaplib.IMAP4_SSL:
    """Verify TLS hostname/certificate (imaplib default does not guarantee this)."""
    client = imaplib.IMAP4_SSL(
        cfg["host"], int(cfg.get("port", 993)),
        ssl_context=ssl.create_default_context(), timeout=25,
    )
    try:
        imap_login(client, cfg)
        folder = cfg.get("folder", "INBOX")
        typ, response = client.select(folder, readonly=True)
        if typ != "OK":
            if provider_for(cfg) == "163" and b"unsafe login" in b" ".join(
                    x.lower() for x in response if isinstance(x, bytes)):
                raise imaplib.IMAP4.error("Unsafe Login")
            raise RuntimeError("无法以只读方式打开收件箱，请检查 IMAP 文件夹权限")
        return client
    except Exception:
        try:
            client.logout()
        except Exception:
            pass
        raise


def check_account_auth(cfg: dict) -> str:
    """Connect/authenticate/SELECT STATUS only: no mail bodies, no DB writes."""
    client = _open_mailbox(cfg)
    try:
        return _uidvalidity(client, cfg.get("folder", "INBOX"))
    finally:
        try:
            client.logout()
        except Exception:
            pass


def effective_rules(global_rules: dict, account: dict) -> dict:
    """Append per-account keywords/VIPs without erasing global matching rules.

    A university mailbox can flag exams/registration/scholarships while QQ and
    NetEase personal accounts keep their existing noise filters.
    """
    merged = dict(global_rules)
    overrides = account.get("rules") or {}
    for field in ("urgent_keywords", "important_keywords", "ignore_keywords",
                  "vip_senders", "vip_domains"):
        if field in overrides:
            from mailcore import URGENT_DEFAULT, IMPORTANT_DEFAULT, IGNORE_DEFAULT
            defaults = {"urgent_keywords": URGENT_DEFAULT,
                        "important_keywords": IMPORTANT_DEFAULT,
                        "ignore_keywords": IGNORE_DEFAULT}
            base = merged.get(field, defaults.get(field, ()))
            merged[field] = list(dict.fromkeys([*base, *overrides[field]]))
    return merged


def scan_account(con: sqlite3.Connection, cfg: dict, rules: dict, *, dry_run: bool = False,
                 max_scan: int = 150, bootstrap_hours: int = 24) -> tuple[int, int]:
    name = cfg["name"]
    folder = cfg.get("folder", "INBOX")
    scanned = queued = 0
    client = _open_mailbox(cfg)
    try:
        validity = _uidvalidity(client, folder)
        old = _cursor(con, name, folder)
        first = not old or old[0] != validity
        last_uid = 0 if first else old[1]
        if first:
            print(f"[{name}] 首次扫描/UIDVALIDITY 已变化：仅提醒最近 {bootstrap_hours} 小时的邮件")
            typ, data = client.uid("SEARCH", None, "ALL")
        else:
            typ, data = client.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
        if typ != "OK":
            raise RuntimeError("IMAP UID 搜索失败")
        candidates = [int(x) for x in (data[0] or b"").split()]
        candidates = sorted(uid for uid in candidates if uid > last_uid)
        if first:
            candidates = candidates[-max_scan:]
        else:
            candidates = candidates[:max_scan]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=bootstrap_hours)
        for uid in candidates:
            # On any failed fetch/classification, do not advance the cursor: next run retries.
            msg = extract_message(_fetch_raw(client, uid))
            scanned += 1
            should_process = not first or msg.received_at is None or msg.received_at.astimezone(timezone.utc) >= cutoff
            assessed: Assessment | None = None
            if should_process:
                assessed = heuristic_assess(msg, effective_rules(rules, cfg))
                if os.getenv("AI_BASE_URL", "") and os.getenv("AI_MODEL", ""):
                    assessed = ai_assess(msg, assessed)
            if dry_run:
                print(f"  UID {uid} | {(assessed.priority if assessed else 'old'):<9} | {msg.subject[:75]}")
            else:
                # Cursor update and optional queue insert are atomic.
                with con:
                    if assessed and assessed.priority in ("urgent", "important"):
                        con.execute("""INSERT OR IGNORE INTO messages
                          (account,folder,uidvalidity,uid,sender,subject,priority,summary,action,deadline,status,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (name, folder, validity, uid, msg.sender, msg.subject,
                           assessed.priority, assessed.summary, assessed.action,
                           assessed.deadline, "pending", datetime.now(timezone.utc).isoformat()))
                        queued += 1
                    _update_cursor(con, name, folder, validity, uid)
        if first and not candidates and not dry_run:
            # Empty mailbox: initialize the cursor with the current UIDVALIDITY.
            with con:
                _update_cursor(con, name, folder, validity, 0)
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return scanned, queued


def _now_local() -> datetime:
    zone = os.getenv("TIMEZONE", "Asia/Shanghai")
    return datetime.now(ZoneInfo(zone))


def _pending(con: sqlite3.Connection, priority: str, limit: int) -> list[dict]:
    rows = con.execute("""SELECT id,account,sender,subject,priority,summary,action,deadline
      FROM messages WHERE status='pending' AND priority=? ORDER BY id LIMIT ?""",
      (priority, limit)).fetchall()
    return [dict(x) for x in rows]


def _push_count(con: sqlite3.Connection, local_day: str) -> int:
    return int(con.execute("SELECT COUNT(*) FROM pushes WHERE local_day=?", (local_day,)).fetchone()[0])


def _mark_pushed(con: sqlite3.Connection, rows: list[dict], day: str, kind: str) -> None:
    ids = [int(row["id"]) for row in rows]
    with con:
        con.executemany("UPDATE messages SET status='sent' WHERE id=?", [(i,) for i in ids])
        con.execute("INSERT INTO pushes(local_day, kind, sent_at) VALUES(?,?,?)",
                    (day, kind, datetime.now(timezone.utc).isoformat()))


def _fit_notification(rows: list[dict], *, digest: bool) -> list[dict]:
    """Fit a notification to a channel's practical Markdown payload size."""
    byte_limit = 3500 if os.getenv("NOTIFIER", "serverchan") == "wecom" else 12000
    picked: list[dict] = []
    for item in rows:
        proposed = picked + [item]
        title, body = render_alerts(proposed, digest=digest)
        if len(f"{title}\n{body}".encode("utf-8")) > byte_limit and picked:
            break
        picked = proposed
    return picked


def flush_pushes(con: sqlite3.Connection) -> None:
    now = _now_local()
    day = now.date().isoformat()
    quota = int(os.getenv("DAILY_PUSH_LIMIT", "5"))
    digest_time = os.getenv("DIGEST_TIME", "19:00")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", digest_time):
        raise ValueError("DIGEST_TIME 应为 HH:MM 格式")
    # Set aside one push each day for the important-email digest.
    urgent_limit = max(0, quota - 1)
    while _push_count(con, day) < urgent_limit:
        rows = _fit_notification(_pending(con, "urgent", 8), digest=False)
        if not rows:
            break
        title, body = render_alerts(rows, digest=False)
        try:
            push_message(title, body)
            _mark_pushed(con, rows, day, "urgent")
            print(f"[推送] 已发送紧急提醒（{len(rows)} 封）")
        except Exception as exc:
            print(f"[推送] 紧急提醒失败，将下次重试：{type(exc).__name__}: {exc}")
            break

    if now.strftime("%H:%M") < digest_time:
        return
    if con.execute("SELECT 1 FROM digest_days WHERE local_day=?", (day,)).fetchone():
        return
    rows = _fit_notification(_pending(con, "important", 20), digest=True)
    if not rows:
        with con:
            con.execute("INSERT OR IGNORE INTO digest_days(local_day,checked_at) VALUES(?,?)",
                        (day, datetime.now(timezone.utc).isoformat()))
        print("[日报] 暂无待汇总的重要邮件")
        return
    if _push_count(con, day) >= quota:
        print("[日报] 今日通知额度已用完，待下次尝试")
        return
    try:
        title, body = render_alerts(rows, digest=True)
        push_message(title, body)
        with con:
            _mark_pushed(con, rows, day, "digest")
            con.execute("INSERT OR IGNORE INTO digest_days(local_day,checked_at) VALUES(?,?)",
                        (day, datetime.now(timezone.utc).isoformat()))
        print(f"[日报] 已发送重要邮件摘要（{len(rows)} 封）")
    except Exception as exc:
        print(f"[日报] 推送失败，将下次重试：{type(exc).__name__}: {exc}")


def _db_path() -> Path:
    path = Path(os.getenv("DB_PATH", str(ROOT / "data" / "mail_assistant.sqlite3")))
    return path if path.is_absolute() else ROOT / path


def _heartbeat(path: Path, ok: int, failed: int, total: int) -> None:
    """Atomic heartbeat for Docker healthcheck (does not include credentials)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok, "failed": failed, "total": total,
    }
    target = path.parent / "heartbeat.json"
    temp = path.parent / "heartbeat.json.tmp"
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if os.name == "posix":
        temp.chmod(0o600)
    temp.replace(target)


def get_health(path: Path) -> tuple[bool, str]:
    file = path.parent / "heartbeat.json"
    if not file.is_file():
        return False, "还没有检查记录（监控尚未完成第一轮）"
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(value["checked_at"])
        if checked.tzinfo is None:
            return False, "检查记录时间格式错误"
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        max_age = max(300, int(os.getenv("POLL_SECONDS", "300")) * 3)
        if age > max_age:
            return False, f"最近检查记录已过期（{int(age)}秒）"
        if value["total"] < 1 or value["failed"] or value["ok"] != value["total"]:
            return False, f"最近一轮 {value['ok']}/{value['total']} 个邮箱成功"
        return True, f"最近一轮 {value['ok']}/{value['total']} 个邮箱连接成功"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "检查记录不可用"


def doctor(conf: dict, *, check_notifier: bool = True) -> bool:
    """Offline configuration diagnostics. Never echo the credentials."""
    good = True
    accounts = [a for a in conf.get("accounts", []) if a.get("enabled", True)]
    if not accounts:
        print("[配置] 没有启用的邮箱。请运行 python setup.py")
        good = False
    for account in accounts:
        name = account.get("name", "未命名邮箱")
        problems = validate_account(account)
        if not secret_exists(account):
            problems.append(f"找不到有效的授权码环境变量 {account.get('password_env', '<未配置>')}")
        if problems:
            good = False
            for problem in problems:
                print(f"[{name}] 配置问题：{problem}")
        else:
            print(f"[{name}] 配置就绪：{account['host']}:993 / SSL/TLS / 客户端凭据已设置")
            if provider_for(account) == "163":
                print(f"[{name}] 自动启用网易专用 IMAP ID 握手")
            if provider_for(account) == "xjtu_student":
                print(f"[{name}] 西交学生邮箱须使用网页版生成的客户端专用密码，不能用网页登录密码")
    if check_notifier:
        channel = os.getenv("NOTIFIER", "serverchan")
        if channel == "serverchan":
            key = os.getenv("SERVERCHAN_SENDKEY", "").strip()
            if key.startswith("SCT") and not any(x in key for x in ("替换", "YOUR", "示例")):
                print("[微信] Server酱 Turbo SendKey 已配置（未执行实际推送）")
            else:
                print("[微信] Server酱 Turbo SendKey 缺失或仍为示例值")
                good = False
        elif channel == "wecom":
            if os.getenv("WECOM_WEBHOOK", "").startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="):
                print("[微信] 企业微信群机器人 Webhook 已配置")
            else:
                print("[微信] 企业微信群机器人 Webhook 缺失")
                good = False
        else:
            print("[微信] NOTIFIER 必须为 serverchan 或 wecom")
            good = False
    return good


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"找不到配置 {path}，请复制 config.example.json 为 config.json")
    conf = json.loads(path.read_text(encoding="utf-8"))
    accounts = [a for a in conf.get("accounts", []) if a.get("enabled", True)]
    names = [a.get("name", "") for a in accounts]
    if len(names) != len(set(names)) or "" in names:
        raise ValueError("邮箱账户 name 必须非空且互不重复")
    for account in accounts:
        problems = validate_account(account)
        if problems:
            raise ValueError(f"邮箱 {account.get('name', '<未命名>')} 配置有误：{'；'.join(problems)}")
    return conf


def demo() -> None:
    rules = {"vip_senders": ["ceo@example.com"]}
    examples = [
        MailMessage("Alice <ceo@example.com>", "ceo@example.com", "明天确认合同", "请在明天下午 5 点前审核合同付款条款", None),
        MailMessage("Promotion <sale@example.org>", "sale@example.org", "限时优惠 8 折", "大促活动，点击订阅营销信息", None),
        MailMessage("Billing <billing@example.com>", "billing@example.com", "付款失败，请立即处理", "请检查付款方式以避免服务暂停", None),
    ]
    for msg in examples:
        result = heuristic_assess(msg, rules)
        print(f"[{result.priority:9}] {msg.subject} | {result.summary} | 截止:{result.deadline or '无'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="多邮箱重要邮件微信通知助手")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="检查一次邮箱，并按时间推送")
    mode.add_argument("--loop", action="store_true", help="循环运行，适合 Docker / 服务器")
    mode.add_argument("--dry-run", action="store_true", help="检查邮箱但不记游标、不微信推送")
    mode.add_argument("--test-push", action="store_true", help="向微信发送一条测试消息")
    mode.add_argument("--demo", action="store_true", help="用模拟邮件测试分类，不连接邮箱")
    mode.add_argument("--doctor", action="store_true", help="离线检查配置，不连接邮箱、不推送")
    mode.add_argument("--check-auth", action="store_true", help="验证 QQ、163、西交学生邮箱客户端授权，不读取邮件正文")
    mode.add_argument("--status", action="store_true", help="查看最近一次运行状态与待发通知计数")
    mode.add_argument("--healthcheck", action="store_true", help="供 Docker 健康检查使用")
    args = parser.parse_args()
    read_env(ROOT / ".env")
    if args.demo:
        demo()
        return
    if args.test_push:
        push_message("邮件助手连接成功", "微信提醒渠道测试成功。以后重要邮件可以通过这个渠道提醒你。")
        print("测试推送成功")
        return
    db_path = _db_path()
    if args.healthcheck:
        ok, hint = get_health(db_path)
        print(hint)
        if not ok:
            sys.exit(1)
        return
    if args.status:
        ok, hint = get_health(db_path)
        print(("[健康] " if ok else "[注意] ") + hint)
        if db_path.exists():
            with closing(sqlite3.connect(db_path)) as con:
                for priority, count in con.execute(
                    "SELECT priority,COUNT(*) FROM messages WHERE status='pending' GROUP BY priority"):
                    print(f"[待推送] {priority}: {count} 封")
        return

    config = load_config(ROOT / "config.json")
    if args.doctor:
        if not doctor(config):
            sys.exit(2)
        return
    if args.check_auth:
        if not doctor(config, check_notifier=False):
            sys.exit(2)
        success = fail = 0
        for account in config.get("accounts", []):
            if not account.get("enabled", True):
                continue
            try:
                check_account_auth(account)
                success += 1
                print(f"[{account['name']}] IMAP 连接、授权码、只读收件箱验证成功")
            except Exception as exc:
                fail += 1
                print(f"[{account['name']}] 验证失败：{auth_hint(exc, account)}")
        print(f"[验证] 成功 {success}，失败 {fail}；没有读取邮件正文或发送微信")
        if fail:
            sys.exit(2)
        return

    if not doctor(config):
        print("请先完成配置检查，避免循环重复错误登录。")
        sys.exit(2)
    retry_after: dict[str, float] = {}
    with closing(db_init(db_path)) as con:
        def once() -> tuple[int, int, int]:
            ok = failed = 0
            accounts = [a for a in config.get("accounts", []) if a.get("enabled", True)]
            for account in accounts:
                name = account["name"]
                if args.loop and time.monotonic() < retry_after.get(name, 0.0):
                    # Invalid authentication may trigger provider account security locks.
                    failed += 1
                    continue
                try:
                    scanned, queued = scan_account(
                        con, account, config.get("rules", {}), dry_run=args.dry_run,
                        max_scan=int(os.getenv("MAX_SCAN_PER_ACCOUNT", "150")),
                        bootstrap_hours=int(os.getenv("FIRST_RUN_HOURS", "24")),
                    )
                    ok += 1
                    retry_after.pop(name, None)
                    print(f"[{name}] 扫描 {scanned} 封；新增待提醒 {queued} 封", flush=True)
                except Exception as exc:
                    failed += 1
                    print(f"[{name}] 本轮读取失败：{auth_hint(exc, account)}", flush=True)
                    if isinstance(exc, imaplib.IMAP4.error) or "unsafe login" in str(exc).lower():
                        retry_after[name] = time.monotonic() + max(
                            600, int(os.getenv("AUTH_RETRY_SECONDS", "3600")))
                        print(f"[{name}] 为保护邮箱安全，将延迟重试认证", flush=True)
            if not args.dry_run:
                try:
                    flush_pushes(con)
                finally:
                    _heartbeat(db_path, ok, failed, len(accounts))
            return ok, failed, len(accounts)

        if args.loop:
            interval = max(60, int(os.getenv("POLL_SECONDS", "300")))
            print(f"[启动] 每 {interval} 秒检查一次；本地时区 {os.getenv('TIMEZONE', 'Asia/Shanghai')}", flush=True)
            while True:
                try:
                    once()
                except Exception as exc:
                    # Keep the process running, but never expose raw server errors.
                    print(f"[循环] 本轮运行异常：{type(exc).__name__}", flush=True)
                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    break
        else:
            ok, failed, _ = once()
            if failed or not ok:
                sys.exit(2)


if __name__ == "__main__":
    main()
