#!/usr/bin/env python3
"""Tiny Telegram bank prototype. Python standard library only."""

import json
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

def load_local_env():
    # Tiny .env reader so the prototype needs no third-party package.
    try:
        with open(".env", encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except FileNotFoundError:
        pass


load_local_env()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR") or os.path.join(os.getcwd(), "data"))
CONFIGURED_DB_PATH = os.environ.get("BANK_DB_PATH", "bank.sqlite3").strip() or "bank.sqlite3"
if os.path.isabs(CONFIGURED_DB_PATH):
    requested_db_path = os.path.normpath(CONFIGURED_DB_PATH)
    app_dir = os.path.abspath(os.getcwd())
    if os.path.commonpath((DATA_DIR, requested_db_path)) == DATA_DIR and requested_db_path != DATA_DIR:
        DB_PATH = requested_db_path
        LEGACY_DB_PATH = os.path.abspath(os.path.relpath(DB_PATH, DATA_DIR))
    elif os.path.commonpath((app_dir, requested_db_path)) == app_dir:
        DB_PATH = os.path.join(DATA_DIR, os.path.relpath(requested_db_path, app_dir))
        LEGACY_DB_PATH = requested_db_path
    else:
        DB_PATH = requested_db_path
        LEGACY_DB_PATH = None
else:
    DB_PATH = os.path.join(DATA_DIR, CONFIGURED_DB_PATH)
    LEGACY_DB_PATH = os.path.abspath(CONFIGURED_DB_PATH)
STARTING_VAULT = os.environ.get("BANK_STARTING_VAULT_AR", "0")
if not TOKEN and __name__ == "__main__":
    raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment first.")

API = f"https://api.telegram.org/bot{TOKEN}/"
BANK_API_TOKEN = os.environ.get("BANK_API_TOKEN", "").strip()
BANK_API_PORT = (os.environ.get("BANK_API_PORT", "").strip()
                 or os.environ.get("PORT", "").strip())
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
MAX_AMOUNT = 2_147_483_647
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
RATE_LIMIT_PER_MINUTE = 30
RESET_CONFIRMATION = "Да, я знаю, что делаю"
REQUEST_TIMES = defaultdict(deque)
RATE_NOTICE_AT = {}
LAST_RATE_PRUNE = 0


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def prepare_database_location():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    target = os.path.abspath(DB_PATH)
    if not LEGACY_DB_PATH or LEGACY_DB_PATH == target:
        return
    if os.path.exists(target) or not os.path.isfile(LEGACY_DB_PATH):
        return

    # Move the old working-directory database and its SQLite WAL files together.
    moved_files = []
    try:
        for suffix in ("", "-wal", "-shm"):
            source_file = LEGACY_DB_PATH + suffix
            target_file = target + suffix
            if os.path.exists(source_file):
                os.replace(source_file, target_file)
                moved_files.append((source_file, target_file))
    except Exception:
        for source_file, target_file in reversed(moved_files):
            if os.path.exists(target_file):
                os.replace(target_file, source_file)
        raise
    print(f"Moved existing SQLite database to persistent data path: {target}")


def init_db():
    prepare_database_location()
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY,
            minecraft_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_users (
            telegram_id INTEGER PRIMARY KEY,
            player_id INTEGER UNIQUE REFERENCES players(id),
            created_at TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 1 CHECK(verified IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY,
            player_id INTEGER NOT NULL REFERENCES players(id),
            number TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS card_access (
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            granted_by INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(card_id, player_id)
        );
        CREATE TABLE IF NOT EXISTS withdrawal_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_player INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            requester_player INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            source_card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
            amount INTEGER NOT NULL CHECK(amount > 0),
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            closed_at TEXT,
            banker_telegram_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS roles (
            telegram_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            PRIMARY KEY (telegram_id, permission)
        );
        CREATE TABLE IF NOT EXISTS banker_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_player INTEGER NOT NULL REFERENCES players(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            from_player INTEGER REFERENCES players(id),
            to_player INTEGER REFERENCES players(id),
            from_card_id INTEGER REFERENCES cards(id),
            to_card_id INTEGER REFERENCES cards(id),
            amount INTEGER NOT NULL CHECK(amount > 0),
            actor_telegram_id INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vault_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount INTEGER NOT NULL,
            kind TEXT NOT NULL,
            actor_telegram_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL REFERENCES players(id),
            amount INTEGER NOT NULL CHECK(amount > 0),
            reason TEXT NOT NULL,
            payee_card_id INTEGER NOT NULL REFERENCES cards(id),
            issued_by_telegram_id INTEGER NOT NULL,
            issued_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unpaid' CHECK(status IN ('unpaid','paid')),
            paid_at TEXT,
            paid_by_telegram_id INTEGER,
            source_card_id INTEGER REFERENCES cards(id),
            ledger_id INTEGER REFERENCES ledger(id)
        );
        CREATE TABLE IF NOT EXISTS link_codes (
            code_hash TEXT PRIMARY KEY,
            player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            id TEXT PRIMARY KEY,
            secret_hash TEXT NOT NULL,
            player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            telegram_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied','expired')),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS fines_player_status_idx ON fines(player_id,status,id DESC);
        CREATE INDEX IF NOT EXISTS ledger_from_idx ON ledger(from_player, id DESC);
        CREATE INDEX IF NOT EXISTS ledger_to_idx ON ledger(to_player, id DESC);
        """)
        ledger_columns = {row["name"] for row in c.execute("PRAGMA table_info(ledger)").fetchall()}
        telegram_columns = {row["name"] for row in c.execute("PRAGMA table_info(telegram_users)").fetchall()}
        if "verified" not in telegram_columns:
            c.execute("ALTER TABLE telegram_users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1))")
        if "from_card_id" not in ledger_columns:
            c.execute("ALTER TABLE ledger ADD COLUMN from_card_id INTEGER REFERENCES cards(id)")
        if "to_card_id" not in ledger_columns:
            c.execute("ALTER TABLE ledger ADD COLUMN to_card_id INTEGER REFERENCES cards(id)")
        c.execute("CREATE INDEX IF NOT EXISTS ledger_from_card_idx ON ledger(from_card_id, id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS ledger_to_card_idx ON ledger(to_card_id, id DESC)")
        c.execute("""UPDATE ledger SET from_card_id=(SELECT MIN(id) FROM cards WHERE player_id=ledger.from_player)
                     WHERE from_player IS NOT NULL AND from_card_id IS NULL""")
        c.execute("""UPDATE ledger SET to_card_id=(SELECT MIN(id) FROM cards WHERE player_id=ledger.to_player)
                     WHERE to_player IS NOT NULL AND to_card_id IS NULL""")
        c.execute("""UPDATE withdrawal_requests SET source_card_id=(SELECT MIN(id) FROM cards WHERE player_id=withdrawal_requests.account_player)
                     WHERE source_card_id IS NULL""")
        # Legacy request-based withdrawals are retired; release any old reserved balances.
        c.execute("UPDATE withdrawal_requests SET status='rejected',closed_at=? WHERE status='pending'", (datetime.now(timezone.utc).isoformat(),))
        if STARTING_VAULT.isdigit():
            c.execute("INSERT OR IGNORE INTO bank_meta(key,value) VALUES('starting_vault_ar',?)", (STARTING_VAULT,))
        for value in os.environ.get("BANK_ADMIN_IDS", "").split(","):
            value = value.strip()
            if re.fullmatch(r"[0-9]{1,19}", value) and 0 < int(value) <= MAX_SQLITE_INTEGER:
                c.execute("INSERT OR IGNORE INTO roles(telegram_id,permission) VALUES(?, 'admin')", (int(value),))


def api(method, payload=None):
    data = urllib.parse.urlencode(payload or {}).encode()
    req = urllib.request.Request(API + method, data=data)
    with urllib.request.urlopen(req, timeout=40) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result["result"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def keyboard(rows):
    return json.dumps({"inline_keyboard": [[{"text": label, "callback_data": data}
                                              for label, data in row] for row in rows]})


def send(chat_id, text, markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if markup:
        payload["reply_markup"] = markup
    api("sendMessage", payload)


def answer_callback(callback_id, text=""):
    api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def has_permission(tid, permission):
    with db() as c:
        return c.execute("SELECT 1 FROM roles WHERE telegram_id=? AND permission IN (?, 'admin')",
                         (tid, permission)).fetchone() is not None


def valid_nickname(value):
    return bool(USERNAME_RE.fullmatch(value)) and not re.fullmatch(r"[0-9]{5}", value)


def valid_player_lookup(value):
    return bool(re.fullmatch(r"[0-9]{5}", value) or valid_nickname(value))


def allow_request(tid):
    global LAST_RATE_PRUNE
    current = time.monotonic()
    if current - LAST_RATE_PRUNE >= 60:
        for known_tid, history in list(REQUEST_TIMES.items()):
            while history and current - history[0] >= 60:
                history.popleft()
            if not history:
                REQUEST_TIMES.pop(known_tid, None)
                RATE_NOTICE_AT.pop(known_tid, None)
        LAST_RATE_PRUNE = current
    requests = REQUEST_TIMES[tid]
    while requests and current - requests[0] >= 60:
        requests.popleft()
    if len(requests) >= RATE_LIMIT_PER_MINUTE:
        return False
    requests.append(current)
    return True


def should_send_rate_notice(tid):
    current = time.monotonic()
    last_notice = RATE_NOTICE_AT.get(tid)
    if last_notice is None or current - last_notice >= 60:
        RATE_NOTICE_AT[tid] = current
        return True
    return False


def prompt_reset_confirmation(chat_id, tid):
    STATES[tid] = {"mode": "admin_reset", "step": "phrase", "created_at": time.monotonic()}
    send(chat_id,
         f"⚠️ Это удалит игроков, карты, балансы, историю и движения кассы.\n\nЧтобы подтвердить, отправь отдельным сообщением точно:\n{RESET_CONFIRMATION}",
         keyboard([[('Отмена', 'reset_cancel')]]))


def clear_test_data():
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM link_codes")
        c.execute("DELETE FROM fines")
        c.execute("DELETE FROM ledger")
        c.execute("DELETE FROM vault_events")
        c.execute("DELETE FROM withdrawal_requests")
        c.execute("DELETE FROM banker_calls")
        c.execute("DELETE FROM card_access")
        c.execute("DELETE FROM cards")
        c.execute("DELETE FROM telegram_users")
        c.execute("DELETE FROM players")


def user_player(tid):
    with db() as c:
        row = c.execute("SELECT p.* FROM telegram_users t JOIN players p ON p.id=t.player_id WHERE t.telegram_id=? AND t.verified=1",
                        (tid,)).fetchone()
    return row


def player_by_name_or_card(value):
    if not valid_player_lookup(value):
        return None
    with db() as c:
        return c.execute("""SELECT p.* FROM players p
            LEFT JOIN cards k ON k.player_id=p.id AND k.active=1
            WHERE p.minecraft_name=? COLLATE NOCASE OR UPPER(k.number)=UPPER(?) LIMIT 1""",
                         (value, value)).fetchone()


def player_by_name(value):
    if not valid_nickname(value):
        return None
    with db() as c:
        return c.execute("SELECT * FROM players WHERE minecraft_name=? COLLATE NOCASE", (value,)).fetchone()


def balance(card_id):
    with db() as c:
        row = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_card_id=? THEN amount ELSE 0 END),0)-
                                   COALESCE(SUM(CASE WHEN from_card_id=? THEN amount ELSE 0 END),0) AS value
                            FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction','casino_deposit','casino_withdraw','fine_payment')""",
                         (card_id, card_id)).fetchone()
    return int(row["value"])


def player_balance(player_id):
    with db() as c:
        card_ids = c.execute("SELECT id FROM cards WHERE player_id=? AND active=1", (player_id,)).fetchall()
    return sum(balance(row["id"]) for row in card_ids)


def pending_withdrawals(card_id, exclude_id=None):
    with db() as c:
        if exclude_id is None:
            row = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawal_requests WHERE source_card_id=? AND status='pending'", (card_id,)).fetchone()
        else:
            row = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawal_requests WHERE source_card_id=? AND status='pending' AND id<>?", (card_id, exclude_id)).fetchone()
    return int(row[0])


def available_balance(card_id):
    return balance(card_id) - pending_withdrawals(card_id)


def shared_cards(player_id):
    with db() as c:
        return c.execute("""SELECT k.id AS card_id,k.number,p.id AS owner_id,p.minecraft_name AS owner_name
            FROM card_access a JOIN cards k ON k.id=a.card_id JOIN players p ON p.id=k.player_id
            WHERE a.player_id=? AND k.active=1 ORDER BY p.minecraft_name COLLATE NOCASE,k.number""", (player_id,)).fetchall()


def card_telegram_ids(card_id):
    with db() as c:
        rows = c.execute("""SELECT DISTINCT t.telegram_id FROM telegram_users t
            WHERE t.verified=1 AND t.player_id IN (
                SELECT player_id FROM cards WHERE id=?
                UNION SELECT player_id FROM card_access WHERE card_id=?
            )""", (card_id, card_id)).fetchall()
    return [row["telegram_id"] for row in rows]


def notify_card_users(card_id, message, exclude_telegram_id=None):
    for telegram_id in card_telegram_ids(card_id):
        if telegram_id == exclude_telegram_id:
            continue
        try:
            send(telegram_id, message)
        except Exception as exc:
            print(f"Notification delivery failed for Telegram user {telegram_id}: {exc}")


def accessible_accounts(player_id):
    with db() as c:
        own_cards = c.execute("SELECT id,number FROM cards WHERE player_id=? AND active=1 ORDER BY id", (player_id,)).fetchall()
    accounts = [{"account_id": player_id, "card_id": card["id"], "card_number": card["number"], "owner_name": None}
                for card in own_cards]
    for row in shared_cards(player_id):
        accounts.append({"account_id": row["owner_id"], "card_id": row["card_id"],
                         "card_number": row["number"], "owner_name": row["owner_name"]})
    return accounts


def can_use_account(player_id, account_id, card_id):
    if card_id is None:
        return False
    with db() as c:
        own = c.execute("SELECT 1 FROM cards WHERE id=? AND player_id=? AND active=1", (card_id, player_id)).fetchone()
        if own and player_id == account_id:
            return True
        return c.execute("""SELECT 1 FROM card_access a JOIN cards k ON k.id=a.card_id
            WHERE a.player_id=? AND k.player_id=? AND k.id=? AND k.active=1""",
                         (player_id, account_id, card_id)).fetchone() is not None


def total_balances():
    with db() as c:
        row = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_card_id IS NOT NULL THEN amount ELSE 0 END),0)-
                                   COALESCE(SUM(CASE WHEN from_card_id IS NOT NULL THEN amount ELSE 0 END),0) AS value
                            FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction','casino_deposit','casino_withdraw','fine_payment')""").fetchone()
    return int(row["value"])


def vault_cash():
    with db() as c:
        opening = c.execute("SELECT value FROM bank_meta WHERE key='starting_vault_ar'").fetchone()
        movements = c.execute("SELECT COALESCE(SUM(amount),0) FROM vault_events").fetchone()[0]
    return int(opening["value"] if opening else 0) + int(movements)


def issue_card(player_id):
    import secrets
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM cards WHERE player_id=? AND active=1", (player_id,)).fetchone()[0] >= 5:
            return None
        while True:
            number = "".join(str(secrets.randbelow(10)) for _ in range(5))
            if not c.execute("SELECT 1 FROM cards WHERE number=?", (number,)).fetchone():
                break
        c.execute("INSERT INTO cards(player_id,number,created_at) VALUES(?,?,?)", (player_id, number, now()))
        return number


def issue_custom_card(player_id, number):
    if not re.fullmatch(r"[0-9]{5}", number):
        raise ValueError("Номер карты должен состоять ровно из пяти цифр.")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        active_count = c.execute("SELECT COUNT(*) FROM cards WHERE player_id=? AND active=1", (player_id,)).fetchone()[0]
        if active_count >= 5:
            raise ValueError("У игрока уже максимум — пять активных карт.")
        if c.execute("SELECT 1 FROM cards WHERE number=?", (number,)).fetchone():
            raise ValueError("Этот номер уже выдавался. Выбери другой пятизначный номер.")
        c.execute("INSERT INTO cards(player_id,number,created_at) VALUES(?,?,?)", (player_id, number, now()))
    return number


def add_role(tid, permission):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO roles(telegram_id,permission) VALUES(?,?)", (tid, permission))


def menu(tid, show_home=False):
    rows = [[("💳 Баланс и карты", "account"), ("↔️ Перевод", "transfer")],
            [("📜 История", "history"), ("📣 Вызвать банкира", "call_banker")]]
    if has_permission(tid, "banker"):
        rows.append([("🏦 Внести АР", "deposit"), ("💸 Снять АР", "withdraw")])
    if has_permission(tid, "view_ledger"):
        rows.append([("📊 Сводка и журнал банка", "bank_report")])
        rows.append([("👥 Игроки и карты", "players")])
    if has_permission(tid, "admin"):
        rows.append([("🛡 Управление банкирами", "manage_bankers")])
        rows.append([("💳 Выпустить карту игроку", "admin_issue_card")])
        rows.append([("⚖️ Выписать штраф", "admin_fine")])
        rows.append([("🧹 Сбросить тестовую базу", "reset_prompt")])
    if user_player(tid):
        rows.append([("⚖️ Мои штрафы", "my_fines")])
    if show_home:
        rows.append([("🏠 Главное меню", "home")])
    send_menu = keyboard(rows)
    return send_menu


def send_banker_management(chat_id, tid):
    with db() as c:
        players = c.execute("""WITH subjects AS (
                SELECT t.telegram_id,p.minecraft_name FROM players p JOIN telegram_users t ON t.player_id=p.id
                UNION SELECT r.telegram_id,NULL AS minecraft_name FROM roles r
                    WHERE NOT EXISTS(SELECT 1 FROM telegram_users t WHERE t.telegram_id=r.telegram_id)
            )
            SELECT s.telegram_id,s.minecraft_name,
                MAX(CASE WHEN r.permission='banker' THEN 1 ELSE 0 END) AS is_banker,
                MAX(CASE WHEN r.permission='admin' THEN 1 ELSE 0 END) AS is_admin
            FROM subjects s LEFT JOIN roles r ON r.telegram_id=s.telegram_id
            GROUP BY s.telegram_id,s.minecraft_name
            ORDER BY COALESCE(s.minecraft_name,CAST(s.telegram_id AS TEXT)) COLLATE NOCASE""").fetchall()
    rows = []
    for target in players:
        display_name = target["minecraft_name"] or f"Telegram {target['telegram_id']}"
        if target["is_admin"]:
            rows.append([(f"👑 {display_name} — администратор", "noop")])
        elif target["is_banker"]:
            rows.append([(f"✅ {display_name} — снять роль банкира", f"banker_toggle:{target['telegram_id']}")])
        else:
            rows.append([(f"➕ {display_name} — сделать банкиром", f"banker_toggle:{target['telegram_id']}")])
    rows.append([("🏠 Главное меню", "home")])
    if not players:
        send(chat_id, "Зарегистрированных игроков пока нет.", keyboard(rows))
    else:
        send(chat_id, "Управление ролью банкира. Администратор уже имеет все права.", keyboard(rows))


def call_bankers(chat_id, tid, player):
    with db() as c:
        bankers = c.execute("SELECT DISTINCT telegram_id FROM roles WHERE permission IN ('banker','admin')").fetchall()
    if not bankers:
        send(chat_id, "Пока ни одному Telegram аккаунту не выдана роль банкира.", menu(tid, show_home=True))
        return

    current = datetime.now(timezone.utc)
    cutoff = (current - timedelta(minutes=3)).isoformat(timespec="seconds")
    seconds_left = None
    call_id = None
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        last_call = c.execute("SELECT created_at FROM banker_calls ORDER BY id DESC LIMIT 1").fetchone()
        if last_call and last_call["created_at"] > cutoff:
            last_time = datetime.fromisoformat(last_call["created_at"])
            seconds_left = max(1, int((last_time + timedelta(minutes=3) - current).total_seconds()))
        else:
            c.execute("DELETE FROM banker_calls")
            cursor = c.execute("INSERT INTO banker_calls(requester_player,created_at) VALUES(?,?)",
                               (player["id"], current.isoformat(timespec="seconds")))
            call_id = cursor.lastrowid

    if seconds_left is not None:
        send(chat_id, f"Банкиров уже вызвали. Подожди ещё примерно {seconds_left} сек.", menu(tid, show_home=True))
        return

    delivered = 0
    for banker in bankers:
        try:
            send(banker["telegram_id"],
                 f"📣 Игрок {player['minecraft_name']} вызывает банкира в банк. Если можешь прийти — откликнись ему в игре или в Telegram.")
            delivered += 1
        except Exception as exc:
            print(f"Banker call delivery failed for Telegram user {banker['telegram_id']}: {exc}")

    if delivered:
        send(chat_id, "Вызов отправлен банкирам. Подожди, пока кто-нибудь откликнется.", menu(tid, show_home=True))
    else:
        with db() as c:
            c.execute("DELETE FROM banker_calls WHERE id=?", (call_id,))
        send(chat_id, "Не удалось доставить вызов. Попроси банкиров открыть бота и отправить /start.", menu(tid, show_home=True))


def home_markup():
    return keyboard([[('🏠 Главное меню', 'home')]])


def history_text(player_id, telegram_id=None):
    with db() as c:
        if telegram_id is None:
            rows = c.execute("""SELECT l.*, a.minecraft_name AS sender, b.minecraft_name AS receiver,
                (SELECT p.minecraft_name FROM telegram_users t JOIN players p ON p.id=t.player_id WHERE t.telegram_id=l.actor_telegram_id) AS actor_name
            FROM ledger l LEFT JOIN players a ON a.id=l.from_player LEFT JOIN players b ON b.id=l.to_player
            WHERE l.from_player=? OR l.to_player=? ORDER BY l.id DESC LIMIT 10""", (player_id, player_id)).fetchall()
        else:
            rows = c.execute("""SELECT l.*, a.minecraft_name AS sender, b.minecraft_name AS receiver,
                (SELECT p.minecraft_name FROM telegram_users t JOIN players p ON p.id=t.player_id WHERE t.telegram_id=l.actor_telegram_id) AS actor_name
            FROM ledger l LEFT JOIN players a ON a.id=l.from_player LEFT JOIN players b ON b.id=l.to_player
            WHERE l.from_player=? OR l.to_player=? OR l.actor_telegram_id=? ORDER BY l.id DESC LIMIT 10""",
                              (player_id, player_id, telegram_id)).fetchall()
    if not rows:
        return "Операций пока нет."
    labels = {"deposit": "Внесение", "withdraw": "Снятие", "transfer": "Перевод", "correction": "Исправление",
              "casino_bet": "Ставка казино", "casino_win": "Выигрыш казино",
              "casino_deposit": "Депозит в казино", "casino_withdraw": "Вывод из казино", "fine_payment": "Оплата штрафа"}
    result = []
    for r in rows:
        if r["kind"] == "deposit": detail = "внесение наличных АР"
        elif r["kind"] == "withdraw": detail = "снятие наличных АР"
        elif r["kind"] == "transfer": detail = f"{r['sender']} → {r['receiver']}"
        elif r["kind"] == "casino_deposit": detail = "перевод в кошелёк казино"
        elif r["kind"] == "casino_withdraw": detail = "вывод из кошелька казино"
        elif r["kind"] == "fine_payment": detail = "платёж в пользу получателя штрафа"
        else: detail = r["note"] or "исправление"
        suffix = f" ({r['note']})" if r["note"] else ""
        if r["kind"] in ("deposit", "withdraw"):
            detail += f" · банкир: {r['actor_name'] or 'Telegram ID ' + str(r['actor_telegram_id'])}"
        result.append(f"#{r['id']} · {labels.get(r['kind'], r['kind'])}: {detail}, {r['amount']} АР{suffix}")
    return "Последние операции:\n" + "\n".join(result)


def record(kind, sender, receiver, amount, actor, note="", from_card_id=None, to_card_id=None):
    if amount <= 0:
        raise ValueError("Сумма должна быть целым числом больше нуля.")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if kind in ("withdraw", "transfer", "fine_payment"):
            owner = c.execute("SELECT player_id FROM cards WHERE id=? AND active=1", (from_card_id,)).fetchone()
            if not owner or owner["player_id"] != sender:
                raise ValueError("Карта списания не найдена или выключена.")
            bal = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_card_id=? THEN amount ELSE 0 END),0)-
                                      COALESCE(SUM(CASE WHEN from_card_id=? THEN amount ELSE 0 END),0)
               FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction','casino_deposit','casino_withdraw','fine_payment')""",
                            (from_card_id, from_card_id)).fetchone()[0]
            held = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawal_requests WHERE source_card_id=? AND status='pending'", (from_card_id,)).fetchone()[0]
            available = int(bal) - int(held)
            if available < amount:
                raise ValueError(f"Недостаточно свободных средств на карте. Доступно: {available} АР.")
        if kind in ("deposit", "transfer", "fine_payment"):
            owner = c.execute("SELECT player_id FROM cards WHERE id=? AND active=1", (to_card_id,)).fetchone()
            if not owner or owner["player_id"] != receiver:
                raise ValueError("Карта зачисления не найдена или выключена.")
        if kind == "withdraw":
            opening = c.execute("SELECT value FROM bank_meta WHERE key='starting_vault_ar'").fetchone()
            cash_moves = c.execute("SELECT COALESCE(SUM(amount),0) FROM vault_events").fetchone()[0]
            available_cash = int(opening["value"] if opening else 0) + int(cash_moves)
            if available_cash < amount:
                raise ValueError(f"В хранилище недостаточно АР. Доступно: {available_cash} АР.")
        c.execute("""INSERT INTO ledger(kind,from_player,to_player,from_card_id,to_card_id,amount,actor_telegram_id,note,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (kind, sender, receiver, from_card_id, to_card_id, amount, actor, note, now()))
        if kind == "deposit":
            c.execute("INSERT INTO vault_events(amount,kind,actor_telegram_id,created_at) VALUES(?,?,?,?)", (amount, kind, actor, now()))
        elif kind == "withdraw":
            c.execute("INSERT INTO vault_events(amount,kind,actor_telegram_id,created_at) VALUES(?,?,?,?)", (-amount, kind, actor, now()))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def sync_player(name):
    """Create a bank account shell for a server-registered name, without issuing a card."""
    if not valid_nickname(name):
        raise ValueError("Некорректный Minecraft ник.")
    with db() as c:
        c.execute("INSERT OR IGNORE INTO players(minecraft_name,created_at) VALUES(?,?)", (name, now()))
        row = c.execute("SELECT id,minecraft_name FROM players WHERE minecraft_name=? COLLATE NOCASE", (name,)).fetchone()
    return dict(row)


def issue_link_code(name):
    player = sync_player(name)
    created = now()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM telegram_users WHERE player_id=? AND verified=1", (player["id"],)).fetchone():
            raise ValueError("Этот Minecraft ник уже привязан к Telegram.")
        c.execute("DELETE FROM link_codes WHERE expires_at<=? OR player_id=?", (created, player["id"]))
        for _ in range(10):
            code = f"{secrets.randbelow(100_000_000):08d}"
            digest = hashlib.sha256(code.encode("ascii")).hexdigest()
            try:
                c.execute("INSERT INTO link_codes(code_hash,player_id,created_at,expires_at) VALUES(?,?,?,?)",
                          (digest, player["id"], created, expires))
                return {"code": code, "expires_in": 60, "player": player["minecraft_name"]}
            except sqlite3.IntegrityError:
                continue
    raise ValueError("Не удалось создать код. Повтори команду.")


def complete_link(telegram_id, code):
    if not re.fullmatch(r"[0-9]{8}", code):
        raise ValueError("Код должен состоять из восьми цифр.")
    digest = hashlib.sha256(code.encode("ascii")).hexdigest()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("""SELECT lc.player_id,p.minecraft_name FROM link_codes lc
            JOIN players p ON p.id=lc.player_id WHERE lc.code_hash=? AND lc.expires_at>?""",
                        (digest, now())).fetchone()
        if not row:
            c.execute("DELETE FROM link_codes WHERE code_hash=?", (digest,))
            raise ValueError("Код неверный или истёк. Запроси новый через /bank link в игре.")
        current = c.execute("SELECT player_id,verified FROM telegram_users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if current and (current["verified"] or current["player_id"] != row["player_id"]):
            raise ValueError("Этот Telegram уже связан с банковским аккаунтом.")
        old = c.execute("SELECT telegram_id,verified FROM telegram_users WHERE player_id=?", (row["player_id"],)).fetchone()
        if old and old["verified"]:
            c.execute("DELETE FROM link_codes WHERE player_id=?", (row["player_id"],))
            raise ValueError("Этот Minecraft ник уже привязан к Telegram.")
        if old and old["telegram_id"] != telegram_id:
            c.execute("DELETE FROM telegram_users WHERE telegram_id=?", (old["telegram_id"],))
            c.execute("DELETE FROM roles WHERE telegram_id=?", (old["telegram_id"],))
        if current:
            c.execute("UPDATE telegram_users SET verified=1,created_at=? WHERE telegram_id=?", (now(), telegram_id))
        else:
            c.execute("INSERT INTO telegram_users(telegram_id,player_id,created_at,verified) VALUES(?,?,?,1)",
                      (telegram_id, row["player_id"], now()))
        c.execute("DELETE FROM link_codes WHERE player_id=?", (row["player_id"],))
        return row["minecraft_name"]


def create_login_attempt(name):
    if not valid_nickname(name):
        raise ValueError("Некорректный Minecraft ник.")
    created_dt = datetime.now(timezone.utc)
    created = created_dt.isoformat(timespec="seconds")
    expires = (created_dt + timedelta(minutes=2)).isoformat(timespec="seconds")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        player = c.execute("SELECT id,minecraft_name FROM players WHERE minecraft_name=? COLLATE NOCASE", (name,)).fetchone()
        if not player:
            raise LookupError("Для этого ника нет банковского профиля.")
        linked = c.execute("SELECT telegram_id FROM telegram_users WHERE player_id=? AND verified=1", (player["id"],)).fetchone()
        if not linked:
            raise LookupError("Telegram ещё не привязан к этому Minecraft нику.")
        # Replace older requests for this player so a stale Telegram button cannot authorize another join.
        c.execute("UPDATE login_attempts SET status='expired',resolved_at=? WHERE player_id=? AND status='pending'", (created, player["id"]))
        attempt_id = secrets.token_hex(6)
        secret = secrets.token_hex(8)
        c.execute("INSERT INTO login_attempts(id,secret_hash,player_id,telegram_id,status,created_at,expires_at) VALUES(?,?,?,?, 'pending',?,?)",
                  (attempt_id, hashlib.sha256(secret.encode("ascii")).hexdigest(), player["id"], linked["telegram_id"], created, expires))
        return {"id": attempt_id, "secret": secret, "player": player["minecraft_name"], "telegram_id": linked["telegram_id"], "expires_in": 120}


def resolve_login_attempt(attempt_id, telegram_id, approve):
    if not re.fullmatch(r"[0-9a-f]{12}", attempt_id):
        raise ValueError("Некорректный запрос входа.")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM login_attempts WHERE id=?", (attempt_id,)).fetchone()
        if not row or row["telegram_id"] != telegram_id:
            raise ValueError("Этот запрос входа не найден.")
        if row["status"] != "pending":
            return row["status"]
        if row["expires_at"] <= now():
            c.execute("UPDATE login_attempts SET status='expired',resolved_at=? WHERE id=? AND status='pending'", (now(), attempt_id))
            return "expired"
        status = "approved" if approve else "denied"
        c.execute("UPDATE login_attempts SET status=?,resolved_at=? WHERE id=? AND status='pending'", (status, now(), attempt_id))
        return status


def poll_login_attempt(attempt_id, secret):
    if not re.fullmatch(r"[0-9a-f]{12}", attempt_id) or not re.fullmatch(r"[0-9a-f]{16}", secret):
        raise ValueError("Некорректный запрос входа.")
    digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
    with db() as c:
        row = c.execute("SELECT secret_hash,status,expires_at FROM login_attempts WHERE id=?", (attempt_id,)).fetchone()
        if not row or not hmac.compare_digest(row["secret_hash"], digest):
            raise LookupError("Запрос входа не найден.")
        if row["status"] == "pending" and row["expires_at"] <= now():
            c.execute("UPDATE login_attempts SET status='expired',resolved_at=? WHERE id=? AND status='pending'", (now(), attempt_id))
            return "expired"
        return row["status"]


def unpaid_fines(player_id):
    with db() as c:
        return c.execute("""SELECT f.*,k.number AS payee_card_number,p.minecraft_name AS payee_name,
            (SELECT q.minecraft_name FROM telegram_users t JOIN players q ON q.id=t.player_id
                WHERE t.telegram_id=f.issued_by_telegram_id) AS issuer_name
            FROM fines f JOIN cards k ON k.id=f.payee_card_id JOIN players p ON p.id=k.player_id
            WHERE f.player_id=? AND f.status='unpaid' ORDER BY f.id""", (player_id,)).fetchall()


def issue_fine(player_id, amount, reason, payee_card_id, issuer_tid):
    if not (1 <= amount <= MAX_AMOUNT):
        raise ValueError("Некорректная сумма штрафа.")
    reason = reason.strip()
    if not reason or len(reason) > 180:
        raise ValueError("Причина штрафа должна содержать от 1 до 180 символов.")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        player = c.execute("SELECT id FROM players WHERE id=?", (player_id,)).fetchone()
        payee = c.execute("SELECT id,player_id FROM cards WHERE id=? AND active=1", (payee_card_id,)).fetchone()
        if not player or not payee:
            raise ValueError("Игрок или карта получателя не найдены.")
        if payee["player_id"] == player_id:
            raise ValueError("Нельзя выписать штраф в пользу карты самого оштрафованного игрока.")
        cur = c.execute("""INSERT INTO fines(player_id,amount,reason,payee_card_id,issued_by_telegram_id,issued_at)
            VALUES(?,?,?,?,?,?)""", (player_id, amount, reason, payee_card_id, issuer_tid, now()))
        return cur.lastrowid


def fine_list_text(player_id):
    rows = unpaid_fines(player_id)
    if not rows:
        return "Неоплаченных штрафов нет."
    return "⚖️ Неоплаченные штрафы:\n" + "\n".join(
        f"#{row['id']} · {row['amount']} АР · {row['reason']}\nКому: {row['payee_name']}, карта {row['payee_card_number']}\nВыписал: {row['issuer_name'] or 'Telegram ID ' + str(row['issued_by_telegram_id'])}"
        for row in rows)


def pay_fine(fine_id, player_id, source_card_id, payer_tid):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        fine = c.execute("SELECT * FROM fines WHERE id=? AND player_id=? AND status='unpaid'", (fine_id, player_id)).fetchone()
        if not fine:
            raise ValueError("Штраф уже оплачен или не найден.")
        source = c.execute("SELECT id,player_id FROM cards WHERE id=? AND active=1", (source_card_id,)).fetchone()
        beneficiary = c.execute("SELECT id,player_id,active FROM cards WHERE id=?", (fine['payee_card_id'],)).fetchone()
        if not source or source['player_id'] != player_id:
            raise ValueError("Списать штраф можно только со своей активной карты.")
        if not beneficiary or not beneficiary['active']:
            raise ValueError("Карта получателя штрафа отключена; обратись к администратору.")
        balance_row = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_card_id=? THEN amount ELSE 0 END),0)-
            COALESCE(SUM(CASE WHEN from_card_id=? THEN amount ELSE 0 END),0)
            FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction','casino_deposit','casino_withdraw','fine_payment')""",
            (source_card_id, source_card_id)).fetchone()
        held = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawal_requests WHERE source_card_id=? AND status='pending'", (source_card_id,)).fetchone()[0]
        available = int(balance_row[0]) - int(held)
        if available < fine['amount']:
            raise ValueError(f"На карте недостаточно средств. Доступно: {available} АР.")
        note = f"Штраф #{fine_id}: {fine['reason']}"
        c.execute("""INSERT INTO ledger(kind,from_player,to_player,from_card_id,to_card_id,amount,actor_telegram_id,note,created_at)
            VALUES('fine_payment',?,?,?,?,?,?,?,?)""",
            (player_id, beneficiary['player_id'], source_card_id, beneficiary['id'], fine['amount'], payer_tid, note, now()))
        ledger_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("UPDATE fines SET status='paid',paid_at=?,paid_by_telegram_id=?,source_card_id=?,ledger_id=? WHERE id=? AND status='unpaid'",
                  (now(), payer_tid, source_card_id, ledger_id, fine_id))
        return ledger_id, int(fine['amount']), int(beneficiary['id'])


def create_withdrawal_request(account_id, requester_id, source_card_id, amount):
    if amount <= 0:
        raise ValueError("Сумма должна быть целым числом больше нуля.")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        card = c.execute("SELECT player_id FROM cards WHERE id=? AND active=1", (source_card_id,)).fetchone()
        if not card or card["player_id"] != account_id:
            raise ValueError("Карта снятия не найдена или выключена.")
        bal = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_card_id=? THEN amount ELSE 0 END),0)-
                                  COALESCE(SUM(CASE WHEN from_card_id=? THEN amount ELSE 0 END),0)
                           FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction','casino_deposit','casino_withdraw','fine_payment')""",
                        (source_card_id, source_card_id)).fetchone()[0]
        held = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawal_requests WHERE source_card_id=? AND status='pending'", (source_card_id,)).fetchone()[0]
        available = int(bal) - int(held)
        if available < amount:
            raise ValueError(f"Недостаточно свободных средств на карте. Доступно: {available} АР.")
        cur = c.execute("""INSERT INTO withdrawal_requests(account_player,requester_player,source_card_id,amount,created_at)
                          VALUES(?,?,?,?,?)""", (account_id, requester_id, source_card_id, amount, now()))
        return cur.lastrowid


def fulfill_withdrawal_request(request_id, banker_telegram_id):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        request = c.execute("""SELECT r.*,a.minecraft_name AS account_name,q.minecraft_name AS requester_name
            FROM withdrawal_requests r JOIN players a ON a.id=r.account_player
            JOIN players q ON q.id=r.requester_player WHERE r.id=? AND r.status='pending'""", (request_id,)).fetchone()
        if not request:
            raise ValueError("Заявка уже обработана или не найдена.")
        account_id, amount = request["account_player"], request["amount"]
        card_id = request["source_card_id"]
        bal = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_card_id=? THEN amount ELSE 0 END),0)-
                                  COALESCE(SUM(CASE WHEN from_card_id=? THEN amount ELSE 0 END),0)
                           FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction','casino_deposit','casino_withdraw','fine_payment')""",
                        (card_id, card_id)).fetchone()[0]
        held = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawal_requests WHERE source_card_id=? AND status='pending' AND id<>?", (card_id, request_id)).fetchone()[0]
        if int(bal) - int(held) < amount:
            raise ValueError("Свободного баланса уже недостаточно для этой заявки.")
        opening = c.execute("SELECT value FROM bank_meta WHERE key='starting_vault_ar'").fetchone()
        cash_moves = c.execute("SELECT COALESCE(SUM(amount),0) FROM vault_events").fetchone()[0]
        cash = int(opening["value"] if opening else 0) + int(cash_moves)
        if cash < amount:
            raise ValueError(f"В хранилище недостаточно АР. Доступно: {cash} АР.")
        c.execute("""INSERT INTO ledger(kind,from_player,to_player,from_card_id,to_card_id,amount,actor_telegram_id,note,created_at)
                     VALUES('withdraw',?,NULL,?,NULL,?,?,?,?)""",
                  (account_id, card_id, amount, banker_telegram_id,
                   f"Выдано по заявке #{request_id} игроку {request['requester_name']}", now()))
        c.execute("INSERT INTO vault_events(amount,kind,actor_telegram_id,created_at) VALUES(?,?,?,?)",
                  (-amount, "withdraw", banker_telegram_id, now()))
        c.execute("UPDATE withdrawal_requests SET status='completed',closed_at=?,banker_telegram_id=? WHERE id=?",
                  (now(), banker_telegram_id, request_id))
        return dict(request)


def reject_withdrawal_request(request_id, banker_telegram_id):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        request = c.execute("""SELECT r.*,a.minecraft_name AS account_name,q.minecraft_name AS requester_name
            FROM withdrawal_requests r JOIN players a ON a.id=r.account_player
            JOIN players q ON q.id=r.requester_player WHERE r.id=? AND r.status='pending'""", (request_id,)).fetchone()
        if not request:
            raise ValueError("Заявка уже обработана или не найдена.")
        c.execute("UPDATE withdrawal_requests SET status='rejected',closed_at=?,banker_telegram_id=? WHERE id=?",
                  (now(), banker_telegram_id, request_id))
        return dict(request)


def begin_state(tid, mode):
    STATES[tid] = {"mode": mode, "step": "target" if mode in ("transfer", "deposit", "withdraw") else "amount"}


def parse_amount(value):
    if not re.fullmatch(r"[0-9]{1,10}", value):
        raise ValueError("Введи целое положительное число АР не длиннее 10 цифр.")
    amount = int(value)
    if amount <= 0 or amount > MAX_AMOUNT:
        raise ValueError(f"Сумма должна быть от 1 до {MAX_AMOUNT} АР.")
    return amount


STATES = {}


def start_user_operation(chat_id, tid, player, mode):
    accounts = accessible_accounts(player["id"])
    if not accounts:
        send(chat_id, "Нет активных карт. Сначала выпусти карту в разделе «Баланс и карты».", menu(tid, show_home=True))
        return
    if len(accounts) > 1:
        STATES[tid] = {"mode": mode, "step": "source"}
        rows = []
        for account in accounts:
            if account["owner_name"] is None:
                label = f"Моя карта {account['card_number']} · {available_balance(account['card_id'])} АР"
            else:
                label = f"{account['card_number']} · {account['owner_name']}"
            rows.append([(label, f"useaccount:{mode}:{account['account_id']}:{account['card_id'] or 0}")])
        rows.append([("🏠 Главное меню", "home")])
        send(chat_id, "Выбери счёт, с которого выполнить операцию:", keyboard(rows))
        return

    source = accounts[0]
    STATES[tid] = {"mode": mode, "step": "target" if mode == "transfer" else "amount",
                   "source_player": source["account_id"], "source_card_id": source["card_id"],
                   "source_card_number": source["card_number"]}
    if mode == "transfer":
        send(chat_id, "Введи Minecraft ник получателя или номер его карты. /cancel — отмена.", home_markup())
    else:
        send(chat_id, "Введи целую сумму АР для заявки на снятие. Банкир подтвердит её при выдаче наличных.", home_markup())


def on_message(message):
    if message.get("chat", {}).get("type") != "private":
        return
    chat_id = message["chat"]["id"]
    tid = message["from"]["id"]
    text = (message.get("text") or "").strip()
    if len(text) > 256:
        if STATES.get(tid, {}).get("mode") == "admin_reset":
            STATES.pop(tid, None)
        send(chat_id, "Сообщение слишком длинное. Максимум — 256 символов.")
        return
    player = user_player(tid)

    if text.startswith("/start"):
        STATES.pop(tid, None)
        if player:
            send(chat_id, f"Йоу, {player['minecraft_name']}! Это банк сервера.\nВыбери действие:", menu(tid))
        else:
            send(chat_id, "Йоу! Для привязки открой Minecraft, войди через AuthMe и выполни /bank link. Введи сюда выданный код — он действует одну минуту.")
            STATES[tid] = {"mode": "link", "step": "code"}
        return

    if text.startswith("/cancel"):
        STATES.pop(tid, None)
        send(chat_id, "Действие отменено.", menu(tid) if player else None)
        return

    if text and text.split(maxsplit=1)[0].split("@", 1)[0] == "/link":
        parts = text.split()
        if len(parts) == 1:
            STATES[tid] = {"mode": "link", "step": "code"}
            send(chat_id, "Введи одноразовый восьмизначный код из Minecraft. Он действует одну минуту.")
            return
        if len(parts) != 2:
            send(chat_id, "Формат: /link КОД")
            return
        try:
            linked_name = complete_link(tid, parts[1])
        except ValueError as exc:
            send(chat_id, str(exc))
            return
        STATES.pop(tid, None)
        send(chat_id, f"Готово! Minecraft ник {linked_name} привязан к этому Telegram и банковскому профилю. Карта выпускается отдельно.", menu(tid))
        return

    if text.startswith("/") and tid in STATES:
        STATES.pop(tid, None)

    if text.startswith("/setrole"):
        if not has_permission(tid, "admin"):
            send(chat_id, "Нет доступа.")
            return
        parts = text.split()
        if (len(parts) != 3 or not re.fullmatch(r"[0-9]{1,19}", parts[1])
                or int(parts[1]) <= 0 or int(parts[1]) > MAX_SQLITE_INTEGER
                or parts[2] not in ("banker", "view_ledger", "admin")):
            send(chat_id, "Формат: /setrole TELEGRAM_ID banker|view_ledger|admin")
            return
        add_role(int(parts[1]), parts[2])
        send(chat_id, f"Роль {parts[2]} выдана Telegram ID {parts[1]}.")
        return

    if text.startswith("/help"):
        send(chat_id, "Команды: /start — меню, /cancel — отменить ввод.\nАдмин: /setrole TELEGRAM_ID banker|view_ledger|admin")
        return

    if text.startswith("/ledger"):
        if not has_permission(tid, "view_ledger"):
            send(chat_id, "Нет доступа.")
            return
        send(chat_id, global_history_text(), menu(tid, show_home=True))
        return

    if text.startswith("/reset_test_data"):
        if not has_permission(tid, "admin"):
            send(chat_id, "Нет доступа.")
            return
        prompt_reset_confirmation(chat_id, tid)
        return

    state = STATES.get(tid)
    if not state:
        send(chat_id, "Нажми /start, чтобы открыть меню.")
        return
    mode, step = state["mode"], state["step"]

    try:
        if mode in ("deposit", "withdraw") and not (has_permission(tid, "banker") or has_permission(tid, "admin")):
            STATES.pop(tid, None)
            send(chat_id, "Роль банкира больше не активна.", menu(tid))
            return

        if mode == "admin_reset" and step == "phrase":
            if not has_permission(tid, "admin"):
                STATES.pop(tid, None)
                send(chat_id, "Нет доступа администратора.")
                return
            if time.monotonic() - state.get("created_at", 0) > 120:
                STATES.pop(tid, None)
                send(chat_id, "Подтверждение сброса истекло. База не удалена.", menu(tid))
                return
            if text != RESET_CONFIRMATION:
                STATES.pop(tid, None)
                send(chat_id, "Фраза не совпала. База не удалена.", menu(tid))
                return
            clear_test_data()
            STATES.clear()
            send(chat_id, "Тестовая база очищена: аккаунты, карты, балансы и история удалены. Роли сохранены.", menu(tid))
            return

        if mode == "admin_issue_card":
            if not has_permission(tid, "admin"):
                STATES.pop(tid, None)
                send(chat_id, "Нет доступа администратора.", menu(tid))
                return
            if step == "target":
                if not valid_nickname(text):
                    raise ValueError("Введи точный Minecraft ник игрока. Ники из пяти цифр запрещены.")
                target = player_by_name(text)
                if not target:
                    raise ValueError("Этот ник ещё не зарегистрирован в банке.")
                with db() as c:
                    active_count = c.execute("SELECT COUNT(*) FROM cards WHERE player_id=? AND active=1", (target["id"],)).fetchone()[0]
                if active_count >= 5:
                    raise ValueError("У игрока уже максимум — пять активных карт.")
                state.update(step="number", target_player_id=target["id"], target_name=target["minecraft_name"])
                send(chat_id, f"Игрок: {target['minecraft_name']}. Введи желаемый номер карты из пяти цифр.", home_markup())
                return
            if step == "number":
                number = issue_custom_card(state["target_player_id"], text)
                target_name = state["target_name"]
                target_player_id = state["target_player_id"]
                STATES.pop(tid, None)
                send(chat_id, f"Карта {number} выпущена игроку {target_name}.", menu(tid, show_home=True))
                with db() as c:
                    target_user = c.execute("SELECT telegram_id FROM telegram_users WHERE player_id=? AND verified=1", (target_player_id,)).fetchone()
                if target_user:
                    try:
                        send(target_user["telegram_id"], f"Администратор выпустил тебе карту {number}.")
                    except Exception as exc:
                        print(f"Custom card notification failed for Telegram user {target_user['telegram_id']}: {exc}")
                return

        if mode == "admin_fine":
            if not has_permission(tid, "admin"):
                STATES.pop(tid, None)
                send(chat_id, "Нет доступа администратора.", menu(tid))
                return
            if step == "target":
                target = player_by_name(text)
                if not target:
                    raise ValueError("Игрок не найден. Сначала зарегистрируй его в банке или синхронизируй через сервер.")
                state.update(step="amount", fine_player_id=target["id"], fine_player_name=target["minecraft_name"])
                send(chat_id, f"Игрок: {target['minecraft_name']}. Введи сумму штрафа в целых АР.", home_markup())
                return
            if step == "amount":
                amount = parse_amount(text)
                state.update(step="reason", fine_amount=amount)
                send(chat_id, "Введи текст причины штрафа (до 180 символов).", home_markup())
                return
            if step == "reason":
                reason = text.strip()
                if not reason or len(reason) > 180:
                    raise ValueError("Причина штрафа должна содержать от 1 до 180 символов.")
                with db() as c:
                    payees = c.execute("""SELECT k.id,k.number,p.minecraft_name FROM cards k
                        JOIN players p ON p.id=k.player_id WHERE k.active=1 ORDER BY p.minecraft_name COLLATE NOCASE,k.number""").fetchall()
                if not payees:
                    raise ValueError("Нет активных карт для получателя платежа. Сначала выпусти карту правительства или ведомства.")
                state.update(step="payee", fine_reason=reason)
                rows = [[(f"{r['minecraft_name']} · карта {r['number']}", f"finepayee:{r['id']}")] for r in payees]
                rows.append([("🏠 Главное меню", "home")])
                send(chat_id, "Выбери, в пользу кого будет оплачен штраф:", keyboard(rows))
                return

        if mode == "link" and step == "code":
            linked_name = complete_link(tid, text)
            STATES.pop(tid, None)
            send(chat_id, f"Готово! Minecraft ник {linked_name} привязан к этому Telegram и банковскому профилю. Карта выпускается отдельно.", menu(tid))
            return

        if mode == "share" and step == "recipient":
            recipient = player_by_name(text)
            if not recipient:
                raise ValueError("Игрок не найден. Он должен сначала зарегистрироваться в боте.")
            if recipient["id"] == player["id"]:
                raise ValueError("Нельзя делиться картой с самим собой.")
            with db() as c:
                card = c.execute("SELECT id,number FROM cards WHERE id=? AND player_id=? AND active=1",
                                  (state["card_id"], player["id"])).fetchone()
                if not card:
                    raise ValueError("Твоя карта не найдена или уже отключена.")
                c.execute("INSERT OR IGNORE INTO card_access(card_id,player_id,granted_by,created_at) VALUES(?,?,?,?)",
                          (card["id"], recipient["id"], player["id"], now()))
            STATES.pop(tid, None)
            send(chat_id, f"Доступ к карте {card['number']} выдан игроку {recipient['minecraft_name']}. Он сможет переводить и запрашивать снятие только с баланса этой карты.", menu(tid, show_home=True))
            return

        if mode == "transfer" and step == "target":
            target = player_by_name_or_card(text)
            if not target:
                raise ValueError("Не нашёл игрока или карту.")
            if re.fullmatch(r"\d{5}", text):
                with db() as c:
                    card = c.execute("SELECT id,number FROM cards WHERE player_id=? AND number=? AND active=1", (target["id"], text)).fetchone()
                if not card:
                    raise ValueError("Карта не найдена или выключена.")
                if card["id"] == state.get("source_card_id"):
                    raise ValueError("Нельзя переводить с карты на неё же.")
                state.update(target=target["id"], target_name=target["minecraft_name"], target_card=text,
                             target_card_id=card["id"], step="amount")
                send(chat_id, f"Перевод на карту {text} игрока {target['minecraft_name']}. Введи целую сумму АР.", home_markup())
                return
            with db() as c:
                cards = c.execute("SELECT id,number FROM cards WHERE player_id=? AND active=1 ORDER BY id", (target["id"],)).fetchall()
            if not cards:
                raise ValueError("У игрока пока нет активной карты.")
            cards = [card for card in cards if card["id"] != state.get("source_card_id")]
            if not cards:
                raise ValueError("У игрока нет другой карты для перевода.")
            state.update(target=target["id"], target_name=target["minecraft_name"], step="pick_card")
            rows = [[(card["number"], f"pickcard:{card['id']}")] for card in cards]
            rows.append([("🏠 Главное меню", "home")])
            api("sendMessage", {"chat_id": chat_id, "text": f"Выбери карту игрока {target['minecraft_name']}:", "reply_markup": keyboard(rows)})
            return

        if mode in ("deposit", "withdraw") and step == "target":
            if not (has_permission(tid, "banker") or has_permission(tid, "admin")):
                raise ValueError("Нет доступа.")
            target = player_by_name_or_card(text)
            if not target:
                raise ValueError("Игрок или карта не найдены.")
            own_player = user_player(tid)
            if not has_permission(tid, "admin") and (not own_player or own_player["id"] == target["id"]):
                raise ValueError("Банкир может вносить или снимать АР на счетах других игроков, но не на своём. Администратор может работать со всеми счетами.")
            with db() as c:
                if re.fullmatch(r"\d{5}", text):
                    cards = c.execute("SELECT id,number FROM cards WHERE player_id=? AND number=? AND active=1", (target["id"], text)).fetchall()
                else:
                    cards = c.execute("SELECT id,number FROM cards WHERE player_id=? AND active=1 ORDER BY id", (target["id"],)).fetchall()
            if not cards:
                raise ValueError("У игрока нет активных карт.")
            if len(cards) == 1:
                state.update(target=target["id"], target_name=target["minecraft_name"], target_card_id=cards[0]["id"], target_card_number=cards[0]["number"], step="amount")
                send(chat_id, f"Карта {cards[0]['number']} игрока {target['minecraft_name']}. Введи целую сумму АР.", home_markup())
            else:
                state.update(target=target["id"], target_name=target["minecraft_name"], step="select_bank_card")
                rows = [[(f"{card['number']} · баланс {balance(card['id'])} АР", f"bankcard:{mode}:{card['id']}")] for card in cards]
                rows.append([("🏠 Главное меню", "home")])
                send(chat_id, f"Выбери карту игрока {target['minecraft_name']}:", keyboard(rows))
            return

        if step == "amount":
            amount = parse_amount(text)
            if mode == "transfer":
                source_player_id = state.get("source_player", player["id"])
                if not can_use_account(player["id"], source_player_id, state.get("source_card_id")):
                    raise ValueError("Доступ к выбранной карте закрыт.")
                source_note = f"Доступ к карте {state['source_card_number']}; действовал {player['minecraft_name']}" if source_player_id != player["id"] else ""
                card_note = f"На карту {state['target_card']}" if state.get("target_card") else "Получатель выбран по нику"
                note = f"{card_note}; {source_note}" if source_note else card_note
                op = record("transfer", source_player_id, state["target"], amount, tid, note,
                            from_card_id=state["source_card_id"], to_card_id=state["target_card_id"])
                send(chat_id, f"Готово: переведено {amount} АР на карту {state['target_card']} игрока {state['target_name']}. Операция #{op}.", menu(tid, show_home=True))
                notify_card_users(state["target_card_id"],
                                  f"🔔 На карту {state['target_card']} поступило {amount} АР от игрока {player['minecraft_name']}.\nОперация #{op}. Баланс карты: {balance(state['target_card_id'])} АР.",
                                  exclude_telegram_id=tid)
            elif mode == "deposit":
                op = record("deposit", None, state["target"], amount, tid, "Наличные АР сданы в кассу банка",
                            to_card_id=state["target_card_id"])
                send(chat_id, f"Зачислено {amount} АР на карту {state['target_card_number']} игрока {state['target_name']}. Операция #{op}.", menu(tid, show_home=True))
                notify_card_users(state["target_card_id"],
                                  f"На карту {state['target_card_number']} внесено {amount} АР. Баланс карты: {balance(state['target_card_id'])} АР. Операция #{op}.")
            elif mode == "withdraw":
                op = record("withdraw", state["target"], None, amount, tid, "Наличные АР выданы из хранилища банка",
                            from_card_id=state["target_card_id"])
                send(chat_id, f"Снято {amount} АР с карты {state['target_card_number']} игрока {state['target_name']}. Операция #{op}.", menu(tid, show_home=True))
            STATES.pop(tid, None)
            return
    except (ValueError, sqlite3.IntegrityError) as e:
        send(chat_id, str(e) + "\nПопробуй ещё раз или отправь /cancel.", home_markup())


def on_callback(query):
    tid = query["from"]["id"]
    chat_id = query["message"]["chat"]["id"]
    data = query.get("data", "")
    login_match = re.fullmatch(r"login:(approve|deny):([0-9a-f]{12})", data)
    if login_match:
        answer_callback(query["id"])
        try:
            status = resolve_login_attempt(login_match.group(2), tid, login_match.group(1) == "approve")
            if status == "approved":
                send(chat_id, "✅ Вход в Minecraft разрешён. Если это были не вы, отклоните вход и смените пароль AuthMe.")
            elif status == "denied":
                send(chat_id, "⛔ Вход отклонён. Сервер не пустит игрока.")
            elif status == "expired":
                send(chat_id, "⌛ Запрос входа уже истёк. Игроку нужно подключиться снова.")
            else:
                send(chat_id, "Этот запрос входа уже обработан.")
        except ValueError as exc:
            send(chat_id, str(exc))
        return
    player = user_player(tid)
    answer_callback(query["id"])
    if data == "account" and player:
        with db() as c:
            cards = c.execute("SELECT id,number FROM cards WHERE player_id=? AND active=1 ORDER BY id", (player["id"],)).fetchall()
            shares = c.execute("""SELECT a.card_id,k.number,p.id AS player_id,p.minecraft_name
                FROM card_access a JOIN cards k ON k.id=a.card_id JOIN players p ON p.id=a.player_id
                WHERE k.player_id=? AND k.active=1 ORDER BY k.number,p.minecraft_name""", (player["id"],)).fetchall()
        shares_by_card = {}
        for share in shares:
            shares_by_card.setdefault(share["card_id"], []).append(share)
        card_lines = "\n".join(
            f"• {card['number']} — {balance(card['id'])} АР (доступно {available_balance(card['id'])} АР)" +
            (" — доступ: " + ", ".join(s["minecraft_name"] for s in shares_by_card.get(card["id"], []))
             if shares_by_card.get(card["id"]) else "")
            for card in cards) or "Карт нет"
        received_cards = shared_cards(player["id"])
        if received_cards:
            card_lines += "\n\nДоступные тебе карты:\n" + "\n".join(
                f"• {row['number']} — {row['owner_name']}: {balance(row['card_id'])} АР (доступно {available_balance(row['card_id'])} АР)"
                for row in received_cards)
        rows = [[("➕ Выпустить карту", "newcard")]] if len(cards) < 5 else []
        if cards:
            rows.append([("🔗 Поделиться картой", "share")])
            for card in cards:
                rows.append([(f"🗑 Удалить {card['number']}", f"deletecard:{card['id']}")])
        for share in shares:
            rows.append([(f"✖ {share['number']} → {share['minecraft_name']}", f"revoke:{share['card_id']}:{share['player_id']}")])
        rows.append([("🏠 Главное меню", "home")])
        send(chat_id, f"Игрок: {player['minecraft_name']}\nВсего на своих картах: {player_balance(player['id'])} АР\nКарты:\n{card_lines}", keyboard(rows))
    elif data == "newcard" and player:
        card = issue_card(player["id"])
        send(chat_id, f"Выпущена новая карта: {card}" if card else "У тебя уже максимум — пять карт.", menu(tid, show_home=True))
    elif data.startswith("deletecard:") and player:
        try:
            card_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        with db() as c:
            card = c.execute("SELECT id,number FROM cards WHERE id=? AND player_id=? AND active=1", (card_id, player["id"])).fetchone()
        if not card:
            send(chat_id, "Карта не найдена или уже удалена.", menu(tid, show_home=True))
            return
        card_balance = balance(card_id)
        if card_balance != 0:
            send(chat_id, f"На карте {card_balance} АР. Сначала переведи остаток на другую карту, потом удаляй.", menu(tid, show_home=True))
            return
        with db() as c:
            pending_fines = c.execute("SELECT COUNT(*) FROM fines WHERE payee_card_id=? AND status='unpaid'", (card_id,)).fetchone()[0]
        if pending_fines:
            send(chat_id, "Эта карта указана получателем неоплаченных штрафов. Сначала переназначь их через администратора.", menu(tid, show_home=True))
            return
        if pending_withdrawals(card_id) > 0:
            send(chat_id, "По этой карте есть ожидающая заявка на снятие. Дождись её обработки.", menu(tid, show_home=True))
            return
        with db() as c:
            c.execute("UPDATE cards SET active=0 WHERE id=?", (card_id,))
            c.execute("DELETE FROM card_access WHERE card_id=?", (card_id,))
        send(chat_id, f"Карта {card['number']} удалена.", menu(tid, show_home=True))
    elif data == "share" and player:
        with db() as c:
            cards = c.execute("SELECT id,number FROM cards WHERE player_id=? AND active=1 ORDER BY id", (player["id"],)).fetchall()
        if not cards:
            send(chat_id, "У тебя пока нет активных карт.", menu(tid, show_home=True))
            return
        STATES[tid] = {"mode": "share", "step": "select_card"}
        rows = [[(card["number"], f"sharecard:{card['id']}")] for card in cards]
        rows.append([("🏠 Главное меню", "home")])
        send(chat_id, "Выбери карту, к которой хочешь открыть доступ:", keyboard(rows))
    elif data.startswith("sharecard:") and player:
        state = STATES.get(tid)
        if not state or state.get("mode") != "share" or state.get("step") != "select_card":
            return
        card_id = int(data.split(":", 1)[1])
        with db() as c:
            card = c.execute("SELECT id,number FROM cards WHERE id=? AND player_id=? AND active=1", (card_id, player["id"])).fetchone()
        if not card:
            STATES.pop(tid, None)
            send(chat_id, "Карта не найдена.", menu(tid, show_home=True))
            return
        state.update(step="recipient", card_id=card_id)
        send(chat_id, f"Введи Minecraft ник игрока, которому открыть доступ к карте {card['number']}.", home_markup())
    elif data.startswith("revoke:") and player:
        try:
            _, card_id, recipient_id = data.split(":", 2)
            card_id, recipient_id = int(card_id), int(recipient_id)
        except ValueError:
            return
        with db() as c:
            owned_share = c.execute("""SELECT 1 FROM card_access a JOIN cards k ON k.id=a.card_id
                WHERE a.card_id=? AND a.player_id=? AND a.granted_by=? AND k.player_id=?""",
                                     (card_id, recipient_id, player["id"], player["id"])).fetchone()
            if not owned_share:
                send(chat_id, "Не нашёл такую открытую тобой карту.", menu(tid, show_home=True))
                return
            c.execute("UPDATE withdrawal_requests SET status='rejected',closed_at=? WHERE source_card_id=? AND requester_player=? AND status='pending'",
                      (now(), card_id, recipient_id))
            c.execute("DELETE FROM card_access WHERE card_id=? AND player_id=?", (card_id, recipient_id))
        send(chat_id, "Доступ к карте закрыт.", menu(tid, show_home=True))
    elif data in ("home", "back"):
        STATES.pop(tid, None)
        send(chat_id, "Главное меню:", menu(tid))
    elif data == "manage_bankers" and has_permission(tid, "admin"):
        send_banker_management(chat_id, tid)
    elif data == "noop" and has_permission(tid, "admin"):
        send(chat_id, "Администратор уже имеет все права.", menu(tid, show_home=True))
    elif data == "admin_issue_card" and has_permission(tid, "admin"):
        STATES[tid] = {"mode": "admin_issue_card", "step": "target"}
        send(chat_id, "Введи Minecraft ник зарегистрированного игрока, которому выпустить карту. /cancel — отмена.", home_markup())
    elif data == "admin_fine" and has_permission(tid, "admin"):
        STATES[tid] = {"mode": "admin_fine", "step": "target"}
        send(chat_id, "Введи Minecraft ник игрока, которому выписывается штраф. /cancel — отмена.", home_markup())
    elif data == "my_fines" and player:
        fines = unpaid_fines(player["id"])
        rows = [[(f"💳 Оплатить штраф #{fine['id']} · {fine['amount']} АР", f"finepay:{fine['id']}")] for fine in fines]
        rows.append([("🏠 Главное меню", "home")])
        send(chat_id, fine_list_text(player["id"]), keyboard(rows))
    elif data.startswith("finepay:") and player:
        try:
            fine_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        fine = next((item for item in unpaid_fines(player["id"]) if item["id"] == fine_id), None)
        if not fine:
            send(chat_id, "Штраф уже оплачен или не найден.", menu(tid, show_home=True))
            return
        with db() as c:
            cards = c.execute("SELECT id,number FROM cards WHERE player_id=? AND active=1 ORDER BY id", (player["id"],)).fetchall()
        if not cards:
            send(chat_id, "У тебя пока нет активной карты. Выпусти её в разделе «Баланс и карты», затем оплати штраф.", menu(tid, show_home=True))
            return
        rows = [[(f"Карта {card['number']} · {balance(card['id'])} АР", f"finecard:{fine_id}:{card['id']}")] for card in cards]
        rows.append([("🏠 Главное меню", "home")])
        send(chat_id, f"Штраф #{fine_id}: {fine['amount']} АР. Выбери карту для оплаты:", keyboard(rows))
    elif data.startswith("finecard:") and player:
        try:
            _, raw_fine_id, raw_card_id = data.split(":", 2)
            fine_id, card_id = int(raw_fine_id), int(raw_card_id)
        except ValueError:
            return
        try:
            ledger_id, amount, beneficiary_card_id = pay_fine(fine_id, player["id"], card_id, tid)
        except ValueError as exc:
            send(chat_id, str(exc), menu(tid, show_home=True))
            return
        send(chat_id, f"Штраф #{fine_id} оплачен: списано {amount} АР. Операция #{ledger_id}.", menu(tid, show_home=True))
        notify_card_users(beneficiary_card_id, f"Получена оплата штрафа #{fine_id}: {amount} АР. Операция #{ledger_id}.")
    elif data.startswith("finepayee:") and has_permission(tid, "admin"):
        state = STATES.get(tid)
        if not state or state.get("mode") != "admin_fine" or state.get("step") != "payee":
            return
        try:
            payee_card_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        try:
            fine_id = issue_fine(state["fine_player_id"], state["fine_amount"], state["fine_reason"], payee_card_id, tid)
        except ValueError as exc:
            STATES.pop(tid, None)
            send(chat_id, str(exc), menu(tid, show_home=True))
            return
        target_name = state["fine_player_name"]
        target_id = state["fine_player_id"]
        STATES.pop(tid, None)
        send(chat_id, f"Штраф #{fine_id} выписан игроку {target_name}: {state['fine_amount']} АР. Он сможет оплатить его после выпуска карты.", menu(tid, show_home=True))
        with db() as c:
            target_tg = c.execute("SELECT telegram_id FROM telegram_users WHERE player_id=? AND verified=1", (target_id,)).fetchone()
        if target_tg:
            try:
                send(target_tg["telegram_id"], f"⚖️ Тебе выписан штраф #{fine_id}: {state['fine_amount']} АР. Причина: {state['fine_reason']}\nОткрой «Мои штрафы» в банковском боте для оплаты.", menu(target_tg["telegram_id"], show_home=True))
            except Exception as exc:
                print(f"Fine notification failed for Telegram user {target_tg['telegram_id']}: {exc}")
    elif data.startswith("banker_toggle:") and has_permission(tid, "admin"):
        raw_player_id = data.split(":", 1)[1]
        if (not re.fullmatch(r"[0-9]{1,19}", raw_player_id)
                or int(raw_player_id) <= 0 or int(raw_player_id) > MAX_SQLITE_INTEGER):
            send(chat_id, "Некорректный игрок.", menu(tid, show_home=True))
            return
        target_telegram_id = int(raw_player_id)
        with db() as c:
            target = c.execute("""SELECT
                    (EXISTS(SELECT 1 FROM telegram_users WHERE telegram_id=?) OR
                     EXISTS(SELECT 1 FROM roles WHERE telegram_id=?)) AS known_user,
                    EXISTS(SELECT 1 FROM roles WHERE telegram_id=? AND permission='admin') AS is_admin,
                    EXISTS(SELECT 1 FROM roles WHERE telegram_id=? AND permission='banker') AS is_banker""",
                               (target_telegram_id, target_telegram_id, target_telegram_id, target_telegram_id)).fetchone()
            if not target["known_user"]:
                send(chat_id, "Аккаунт уже не зарегистрирован и не имеет ролей.", menu(tid, show_home=True))
                return
            if target["is_admin"]:
                send(chat_id, "Администратор уже имеет все права; отдельное переключение роли банкира не требуется.", menu(tid, show_home=True))
                return
            if target["is_banker"]:
                c.execute("DELETE FROM roles WHERE telegram_id=? AND permission='banker'", (target_telegram_id,))
            else:
                c.execute("INSERT OR IGNORE INTO roles(telegram_id,permission) VALUES(?,'banker')", (target_telegram_id,))
        send_banker_management(chat_id, tid)
    elif data == "reset_prompt" and has_permission(tid, "admin"):
        prompt_reset_confirmation(chat_id, tid)
    elif data == "reset_cancel" and has_permission(tid, "admin"):
        STATES.pop(tid, None)
        send(chat_id, "Сброс отменён.", menu(tid))
    elif data.startswith("pickcard:") and player:
        state = STATES.get(tid)
        if not state or state.get("mode") != "transfer" or state.get("step") != "pick_card":
            return
        card_id = int(data.split(":", 1)[1])
        with db() as c:
            card = c.execute("SELECT number FROM cards WHERE id=? AND player_id=? AND active=1", (card_id, state["target"])).fetchone()
        if not card:
            STATES.pop(tid, None)
            send(chat_id, "Эта карта больше недоступна. Начни перевод заново.", menu(tid, show_home=True))
            return
        state.update(step="amount", target_card=card["number"], target_card_id=card_id)
        send(chat_id, f"Перевод на карту {card['number']} игрока {state['target_name']}. Введи целую сумму АР.", home_markup())
    elif data == "transfer" and player:
        start_user_operation(chat_id, tid, player, "transfer")
    elif data.startswith("useaccount:") and player:
        try:
            _, mode, account_id, card_id = data.split(":", 3)
            account_id, card_id = int(account_id), int(card_id)
            card_id = card_id or None
        except ValueError:
            return
        if mode != "transfer" or not can_use_account(player["id"], account_id, card_id):
            send(chat_id, "У тебя больше нет доступа к этому счёту.", menu(tid))
            return
        state = {"mode": mode, "source_player": account_id, "source_card_id": card_id}
        if card_id:
            with db() as c:
                row = c.execute("SELECT number FROM cards WHERE id=?", (card_id,)).fetchone()
            state["source_card_number"] = row["number"] if row else ""
        state["step"] = "target"
        STATES[tid] = state
        send(chat_id, "Введи Minecraft ник получателя или номер его карты.", home_markup())
    elif data == "history" and player:
        send(chat_id, history_text(player["id"], tid), menu(tid, show_home=True))
    elif data == "call_banker" and player:
        call_bankers(chat_id, tid, player)
    elif data in ("deposit", "withdraw") and (has_permission(tid, "banker") or has_permission(tid, "admin")):
        if not has_permission(tid, "admin") and not user_player(tid):
            send(chat_id, "Чтобы банкир не мог провести операцию на собственном счёте, сначала привяжи свой профиль.", menu(tid, show_home=True))
            return
        begin_state(tid, data)
        prompt = "Введи Minecraft ник игрока или номер карты. Администратор может выбрать любой счёт." if has_permission(tid, "admin") else "Введи Minecraft ник игрока или номер карты. Банкир может работать со счетами других игроков, кроме собственного."
        send(chat_id, prompt, home_markup())
    elif data.startswith("bankcard:") and (has_permission(tid, "banker") or has_permission(tid, "admin")):
        state = STATES.get(tid)
        if not state or state.get("step") != "select_bank_card":
            return
        _, mode, raw_card_id = data.split(":", 2)
        card_id = int(raw_card_id)
        if mode != state.get("mode"):
            return
        own_player = user_player(tid)
        if not has_permission(tid, "admin") and (not own_player or own_player["id"] == state.get("target")):
            send(chat_id, "Банкир может вносить или снимать АР на счетах других игроков, но не на своём.", menu(tid, show_home=True))
            STATES.pop(tid, None)
            return
        with db() as c:
            card = c.execute("SELECT id,number,player_id FROM cards WHERE id=? AND player_id=? AND active=1",
                              (card_id, state["target"])).fetchone()
        if not card:
            send(chat_id, "Карта больше не активна.", menu(tid, show_home=True))
            STATES.pop(tid, None)
            return
        state.update(step="amount", target_card_id=card_id, target_card_number=card["number"])
        send(chat_id, f"Карта {card['number']} игрока {state['target_name']}. Введи целую сумму АР.", home_markup())
    elif data == "bank_report" and has_permission(tid, "view_ledger"):
        report = (f"📊 Сводка банка\nИгроков: {player_count()}\nАР на картах: {total_balances()}\n"
                  f"АР в хранилище: {vault_cash()}\n\n{global_history_text()}")
        chunks = split_message(report)
        for index, chunk in enumerate(chunks):
            markup = menu(tid, show_home=True) if index == len(chunks) - 1 else None
            send(chat_id, chunk, markup)
    elif data == "players" and has_permission(tid, "view_ledger"):
        report = players_report_text()
        chunks = split_message(report)
        for index, chunk in enumerate(chunks):
            markup = menu(tid, show_home=True) if index == len(chunks) - 1 else None
            send(chat_id, chunk, markup)


def player_count():
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM players").fetchone()[0]


def global_history_text():
    with db() as c:
        rows = c.execute("""SELECT l.*, a.minecraft_name AS sender, b.minecraft_name AS receiver,
                (SELECT p.minecraft_name FROM telegram_users t JOIN players p ON p.id=t.player_id WHERE t.telegram_id=l.actor_telegram_id) AS actor_name
            FROM ledger l LEFT JOIN players a ON a.id=l.from_player LEFT JOIN players b ON b.id=l.to_player
            ORDER BY l.id DESC LIMIT 20""").fetchall()
    if not rows:
        return "В журнале пока нет операций."
    lines = []
    for r in rows:
        display_kind = {"fine_payment": "Оплата штрафа", "deposit": "Внесение", "withdraw": "Снятие", "transfer": "Перевод"}.get(r["kind"], r["kind"])
        line = f"#{r['id']} {display_kind} · {r['sender'] or 'касса'} → {r['receiver'] or 'касса'} · {r['amount']} АР"
        if r["kind"] in ("deposit", "withdraw"):
            line += f" · банкир: {r['actor_name'] or 'Telegram ID ' + str(r['actor_telegram_id'])}"
        if r["note"]:
            line += f" · {r['note']}"
        lines.append(line)
    return "Последние операции банка:\n" + "\n".join(lines)


def players_report_text():
    with db() as c:
        players = c.execute("SELECT id,minecraft_name FROM players ORDER BY minecraft_name COLLATE NOCASE").fetchall()
        all_cards = c.execute("SELECT id,player_id,number FROM cards WHERE active=1 ORDER BY player_id,id").fetchall()
    cards_by_player = {}
    for card in all_cards:
        cards_by_player.setdefault(card["player_id"], []).append(card)

    lines = [
        "📊 Реестр банка",
        f"Игроков: {len(players)}",
        f"Активных карт: {len(all_cards)}",
        f"АР на картах: {total_balances()}",
        f"АР в хранилище: {vault_cash()}",
        "",
    ]
    if not players:
        lines.append("Зарегистрированных игроков пока нет.")
    else:
        for player in players:
            player_cards = cards_by_player.get(player["id"], [])
            card_text = ", ".join(f"{card['number']}: {balance(card['id'])} АР" for card in player_cards) or "нет активных карт"
            lines.append(f"{player['minecraft_name']} — всего {player_balance(player['id'])} АР\nКарты: {card_text}")
    return "\n".join(lines)


def split_message(text, limit=3600):
    chunks = []
    current = ""
    for part in text.split("\n"):
        candidate = f"{current}\n{part}" if current else part
        if len(candidate) > limit and current:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def start_bank_api():
    if not BANK_API_TOKEN:
        return None
    if len(BANK_API_TOKEN) < 32:
        raise SystemExit("BANK_API_TOKEN must be at least 32 characters.")
    if not BANK_API_PORT:
        raise SystemExit("Set BANK_API_PORT or use the PORT environment variable provided by the host.")
    if not re.fullmatch(r"[0-9]{1,5}", BANK_API_PORT) or not 1 <= int(BANK_API_PORT) <= 65535:
        raise SystemExit("BANK_API_PORT must be between 1 and 65535.")

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/v1/health":
                self.send_json(200, {"ok": True, "service": "minecraft-ar-bank"})
                return
            parsed = urllib.parse.urlsplit(self.path)
            match = re.fullmatch(r"/api/v1/login-attempts/([0-9a-f]{12})", parsed.path)
            if not match:
                self.send_json(404, {"error": "not_found"})
                return
            supplied = self.headers.get("Authorization", "")
            expected = "Bearer " + BANK_API_TOKEN
            if not hmac.compare_digest(supplied, expected):
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                secret = urllib.parse.parse_qs(parsed.query, strict_parsing=True).get("secret", [""])[0]
            except ValueError:
                self.send_json(400, {"error": "invalid_request"})
                return
            try:
                status = poll_login_attempt(match.group(1), secret)
            except ValueError as exc:
                self.send_json(400, {"error": "invalid_request", "message": str(exc)})
            except LookupError as exc:
                self.send_json(404, {"error": "not_found", "message": str(exc)})
            else:
                self.send_json(200, {"ok": True, "status": status})

        def do_POST(self):
            if self.path not in ("/api/v1/players/sync", "/api/v1/link-codes", "/api/v1/login-attempts"):
                self.send_json(404, {"error": "not_found"})
                return
            supplied = self.headers.get("Authorization", "")
            expected = "Bearer " + BANK_API_TOKEN
            if not hmac.compare_digest(supplied, expected):
                self.send_json(401, {"error": "unauthorized"})
                return
            raw_length = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,6}", raw_length) or int(raw_length) > 65536:
                self.send_json(413, {"error": "request_too_large"})
                return
            try:
                payload = json.loads(self.rfile.read(int(raw_length)).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                if self.path == "/api/v1/players/sync":
                    names = payload.get("players")
                    if not isinstance(names, list) or len(names) > 1000:
                        raise ValueError("players must be an array of at most 1000 names")
                    normalized, seen = [], set()
                    for name in names:
                        if not isinstance(name, str) or not valid_nickname(name):
                            raise ValueError("invalid Minecraft nickname in players")
                        if name.casefold() not in seen:
                            normalized.append(name)
                            seen.add(name.casefold())
                    synced = [sync_player(name) for name in normalized]
                    result = {"ok": True, "synced": len(synced), "players": synced}
                else:
                    name = payload.get("player")
                    if not isinstance(name, str):
                        raise ValueError("player must be a Minecraft nickname")
                    if self.path == "/api/v1/link-codes":
                        result = {"ok": True, **issue_link_code(name)}
                    else:
                        attempt = create_login_attempt(name)
                        try:
                            send(attempt["telegram_id"],
                                 f"🔐 Запрос входа в Minecraft от игрока {attempt['player']}.\n\nЕсли это вы — разрешите вход. Если нет — отклоните. Запрос истечёт через 2 минуты.",
                                 keyboard([[('✅ Разрешить', f"login:approve:{attempt['id']}"),
                                           ('⛔ Отклонить', f"login:deny:{attempt['id']}")]]))
                        except Exception:
                            with db() as c:
                                c.execute("UPDATE login_attempts SET status='expired',resolved_at=? WHERE id=? AND status='pending'", (now(), attempt["id"]))
                            raise RuntimeError("Не удалось отправить запрос в Telegram.")
                        result = {"ok": True, "id": attempt["id"], "secret": attempt["secret"],
                                  "expires_in": attempt["expires_in"], "player": attempt["player"]}
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                status = 409 if "already linked" in str(exc).lower() or "уже привязан" in str(exc).lower() else 400
                self.send_json(status, {"error": "invalid_request", "message": str(exc)[:160]})
                return
            except LookupError as exc:
                self.send_json(404, {"error": "not_linked", "message": str(exc)[:160]})
                return
            except RuntimeError as exc:
                self.send_json(503, {"error": "telegram_unavailable", "message": str(exc)[:160]})
                return
            self.send_json(200, result)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", int(BANK_API_PORT)), Handler)
    thread = threading.Thread(target=server.serve_forever, name="bank-api", daemon=True)
    thread.start()
    print(f"Bank API listening on 0.0.0.0:{BANK_API_PORT}")
    return server


def main():
    init_db()
    print(f"Telegram bank prototype is polling. SQLite database: {os.path.abspath(DB_PATH)}")
    start_bank_api()
    offset = 0
    while True:
        try:
            updates = api("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": json.dumps(["message", "callback_query"])})
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    inbound = update.get("message") or update.get("callback_query")
                    if ("message" in update
                            and update["message"].get("chat", {}).get("type") != "private"):
                        continue
                    telegram_user = inbound.get("from", {}) if inbound else {}
                    telegram_id = telegram_user.get("id")
                    if telegram_id is not None and not allow_request(telegram_id):
                        if "callback_query" in update:
                            answer_callback(update["callback_query"]["id"], "Лимит: не более 30 действий в минуту.")
                        elif should_send_rate_notice(telegram_id):
                            send(update["message"]["chat"]["id"], "Лимит: не более 30 действий в минуту. Попробуй чуть позже.")
                        continue
                    if "message" in update:
                        on_message(update["message"])
                    elif "callback_query" in update:
                        on_callback(update["callback_query"])
                except Exception as exc:
                    print(f"Update handler error: {exc}")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Polling error: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
