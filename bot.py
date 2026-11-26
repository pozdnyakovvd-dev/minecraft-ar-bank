#!/usr/bin/env python3
"""Tiny Telegram bank prototype. Python standard library only."""

import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

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
DB_PATH = os.environ.get("BANK_DB_PATH", "bank.sqlite3")
STARTING_VAULT = os.environ.get("BANK_STARTING_VAULT_AR", "0")
if not TOKEN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment first.")

API = f"https://api.telegram.org/bot{TOKEN}/"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


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


def init_db():
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
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY,
            player_id INTEGER NOT NULL REFERENCES players(id),
            number TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS roles (
            telegram_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            PRIMARY KEY (telegram_id, permission)
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            from_player INTEGER REFERENCES players(id),
            to_player INTEGER REFERENCES players(id),
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
        CREATE INDEX IF NOT EXISTS ledger_from_idx ON ledger(from_player, id DESC);
        CREATE INDEX IF NOT EXISTS ledger_to_idx ON ledger(to_player, id DESC);
        """)
        if STARTING_VAULT.isdigit():
            c.execute("INSERT OR IGNORE INTO bank_meta(key,value) VALUES('starting_vault_ar',?)", (STARTING_VAULT,))
        for value in os.environ.get("BANK_ADMIN_IDS", "").split(","):
            value = value.strip()
            if value.isdigit():
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


def user_player(tid):
    with db() as c:
        row = c.execute("SELECT p.* FROM telegram_users t JOIN players p ON p.id=t.player_id WHERE t.telegram_id=?",
                        (tid,)).fetchone()
    return row


def player_by_name_or_card(value):
    with db() as c:
        return c.execute("""SELECT p.* FROM players p
            LEFT JOIN cards k ON k.player_id=p.id AND k.active=1
            WHERE p.minecraft_name=? COLLATE NOCASE OR UPPER(k.number)=UPPER(?) LIMIT 1""",
                         (value, value)).fetchone()


def balance(player_id):
    with db() as c:
        row = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_player=? THEN amount ELSE 0 END),0)-
                                   COALESCE(SUM(CASE WHEN from_player=? THEN amount ELSE 0 END),0) AS value
                            FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction')""",
                         (player_id, player_id)).fetchone()
    return int(row["value"])


def total_balances():
    with db() as c:
        row = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_player IS NOT NULL THEN amount ELSE 0 END),0)-
                                   COALESCE(SUM(CASE WHEN from_player IS NOT NULL THEN amount ELSE 0 END),0) AS value
                            FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction')""").fetchone()
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


def register(tid, name):
    if not USERNAME_RE.fullmatch(name):
        raise ValueError("Ник должен быть от 3 до 16 символов: латинские буквы, цифры или _.")
    with db() as c:
        if c.execute("SELECT 1 FROM telegram_users WHERE telegram_id=?", (tid,)).fetchone():
            raise ValueError("Этот Telegram уже привязан к банковскому аккаунту.")
        if c.execute("SELECT 1 FROM players WHERE minecraft_name=? COLLATE NOCASE", (name,)).fetchone():
            raise ValueError("Этот игровой ник уже зарегистрирован в банке.")
        cur = c.execute("INSERT INTO players(minecraft_name,created_at) VALUES(?,?)", (name, now()))
        pid = cur.lastrowid
        c.execute("INSERT INTO telegram_users(telegram_id,player_id,created_at) VALUES(?,?,?)", (tid, pid, now()))
    return pid


def add_role(tid, permission):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO roles(telegram_id,permission) VALUES(?,?)", (tid, permission))


def menu(tid, show_home=False):
    rows = [[("💳 Баланс и карты", "account"), ("↔️ Перевод", "transfer")],
            [("📜 История", "history")]]
    if has_permission(tid, "banker"):
        rows.append([("🏦 Внести AR", "deposit"), ("💸 Снять AR", "withdraw")])
    if has_permission(tid, "view_ledger"):
        rows.append([("📊 Сводка банка", "summary"), ("📚 Журнал банка", "ledger")])
    if has_permission(tid, "admin"):
        rows.append([("🧹 Сбросить тестовую базу", "reset_prompt")])
    if show_home:
        rows.append([("🏠 Главное меню", "home")])
    send_menu = keyboard(rows)
    return send_menu


def home_markup():
    return keyboard([[('🏠 Главное меню', 'home')]])


def history_text(player_id):
    with db() as c:
        rows = c.execute("""SELECT l.*, a.minecraft_name AS sender, b.minecraft_name AS receiver
            FROM ledger l LEFT JOIN players a ON a.id=l.from_player LEFT JOIN players b ON b.id=l.to_player
            WHERE l.from_player=? OR l.to_player=? ORDER BY l.id DESC LIMIT 10""", (player_id, player_id)).fetchall()
    if not rows:
        return "Операций пока нет."
    labels = {"deposit": "Внесение", "withdraw": "Снятие", "transfer": "Перевод", "correction": "Исправление"}
    result = []
    for r in rows:
        if r["kind"] == "deposit": detail = "внесение наличных AR"
        elif r["kind"] == "withdraw": detail = "снятие наличных AR"
        elif r["kind"] == "transfer": detail = f"{r['sender']} → {r['receiver']}"
        else: detail = r["note"] or "исправление"
        suffix = f" ({r['note']})" if r["note"] else ""
        result.append(f"#{r['id']} · {labels.get(r['kind'], r['kind'])}: {detail}, {r['amount']} AR{suffix}")
    return "Последние операции:\n" + "\n".join(result)


def record(kind, sender, receiver, amount, actor, note=""):
    if amount <= 0:
        raise ValueError("Сумма должна быть целым числом больше нуля.")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        # SQLite serializes writes; this balance check and insertion are one transaction.
        if kind in ("withdraw", "transfer"):
            bal = c.execute("""SELECT COALESCE(SUM(CASE WHEN to_player=? THEN amount ELSE 0 END),0)-
                                      COALESCE(SUM(CASE WHEN from_player=? THEN amount ELSE 0 END),0)
                               FROM ledger WHERE kind IN ('deposit','withdraw','transfer','correction')""",
                            (sender, sender)).fetchone()[0]
            if bal < amount:
                raise ValueError(f"Недостаточно средств. Баланс: {bal} AR.")
        if kind == "withdraw":
            opening = c.execute("SELECT value FROM bank_meta WHERE key='starting_vault_ar'").fetchone()
            cash_moves = c.execute("SELECT COALESCE(SUM(amount),0) FROM vault_events").fetchone()[0]
            available_cash = int(opening["value"] if opening else 0) + int(cash_moves)
            if available_cash < amount:
                raise ValueError(f"В хранилище недостаточно AR. Доступно: {available_cash} AR.")
        c.execute("INSERT INTO ledger(kind,from_player,to_player,amount,actor_telegram_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                  (kind, sender, receiver, amount, actor, note, now()))
        if kind == "deposit":
            c.execute("INSERT INTO vault_events(amount,kind,actor_telegram_id,created_at) VALUES(?,?,?,?)", (amount, kind, actor, now()))
        elif kind == "withdraw":
            c.execute("INSERT INTO vault_events(amount,kind,actor_telegram_id,created_at) VALUES(?,?,?,?)", (-amount, kind, actor, now()))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def begin_state(tid, mode):
    STATES[tid] = {"mode": mode, "step": "target" if mode in ("transfer", "deposit", "withdraw") else "amount"}


def parse_amount(value):
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("Введи положительное целое число AR.")
    return int(value)


STATES = {}


def on_message(message):
    if message.get("chat", {}).get("type") != "private":
        return
    chat_id = message["chat"]["id"]
    tid = message["from"]["id"]
    text = (message.get("text") or "").strip()
    player = user_player(tid)

    if text.startswith("/start"):
        if player:
            send(chat_id, f"Йоу, {player['minecraft_name']}! Это банк сервера.\nВыбери действие:", menu(tid))
        else:
            send(chat_id, "Йоу! Для тестовой версии введи свой Minecraft ник.\n\n⚠️ Сейчас привязка по нику временная и не подтверждает владение пиратским аккаунтом. Перед настоящими переводами добавим одноразовый код из игры через Paper-плагин.")
            STATES[tid] = {"mode": "register", "step": "name"}
        return

    if text.startswith("/cancel"):
        STATES.pop(tid, None)
        send(chat_id, "Действие отменено.", menu(tid) if player else None)
        return

    if text.startswith("/setrole"):
        if not has_permission(tid, "admin"):
            send(chat_id, "Нет доступа.")
            return
        parts = text.split()
        if len(parts) != 3 or parts[2] not in ("banker", "view_ledger", "admin"):
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
        send(chat_id, "Сброс удалит всех тестовых игроков, карты, балансы, историю и движения хранилища. Роли доступа и начальный запас хранилища сохранятся.",
             keyboard([[('⚠️ Подтвердить сброс', 'reset_confirm')], [('Отмена', 'reset_cancel')]]))
        return

    state = STATES.get(tid)
    if not state:
        send(chat_id, "Нажми /start, чтобы открыть меню.")
        return
    mode, step = state["mode"], state["step"]

    try:
        if mode == "register" and step == "name":
            pid = register(tid, text)
            card = issue_card(pid)
            STATES.pop(tid, None)
            send(chat_id, f"Аккаунт {text} создан. Твоя первая карта: {card}\n\nДля тестовой версии привязка по нику не защищена от чужой регистрации. Пока не выдавай на такие аккаунты реальные ценности.", menu(tid))
            return

        if mode == "transfer" and step == "target":
            target = player_by_name_or_card(text)
            if not target or target["id"] == player["id"]:
                raise ValueError("Не нашёл другого игрока по нику или номеру карты.")
            if re.fullmatch(r"\d{5}", text):
                state.update(target=target["id"], target_name=target["minecraft_name"], step="amount")
                send(chat_id, f"Перевод на карту {text} игрока {target['minecraft_name']}. Введи целую сумму AR.", home_markup())
                return
            with db() as c:
                cards = c.execute("SELECT id,number FROM cards WHERE player_id=? AND active=1 ORDER BY id", (target["id"],)).fetchall()
            if not cards:
                raise ValueError("У игрока пока нет активной карты.")
            state.update(target=target["id"], target_name=target["minecraft_name"], step="pick_card")
            rows = [[(card["number"], f"pickcard:{card['id']}")] for card in cards]
            rows.append([("🏠 Главное меню", "home")])
            api("sendMessage", {"chat_id": chat_id, "text": f"Выбери карту игрока {target['minecraft_name']}:", "reply_markup": keyboard(rows)})
            return

        if mode in ("deposit", "withdraw") and step == "target":
            if not has_permission(tid, "banker"):
                raise ValueError("Нет доступа банкира.")
            target = player_by_name_or_card(text)
            if not target:
                raise ValueError("Игрок или карта не найдены.")
            state.update(target=target["id"], target_name=target["minecraft_name"], step="amount")
            send(chat_id, f"Игрок: {target['minecraft_name']}. Введи целую сумму AR.", home_markup())
            return

        if step == "amount":
            amount = parse_amount(text)
            if mode == "transfer":
                op = record("transfer", player["id"], state["target"], amount, tid,
                            f"На карту {state.get('target_card', 'выбрана по нику')}")
                send(chat_id, f"Готово: переведено {amount} AR игроку {state['target_name']}. Операция #{op}.", menu(tid, show_home=True))
            elif mode == "deposit":
                op = record("deposit", None, state["target"], amount, tid, "Наличные AR сданы в кассу банка")
                send(chat_id, f"Зачислено {amount} AR игроку {state['target_name']}. Операция #{op}.", menu(tid, show_home=True))
                with db() as c:
                    recipient = c.execute("SELECT telegram_id FROM telegram_users WHERE player_id=?", (state["target"],)).fetchone()
                if recipient:
                    send(recipient["telegram_id"], f"На твой счёт внесено {amount} AR. Операция #{op}.")
            elif mode == "withdraw":
                op = record("withdraw", state["target"], None, amount, tid, "Наличные AR выданы из хранилища банка")
                send(chat_id, f"Снято {amount} AR со счёта {state['target_name']}. Операция #{op}.", menu(tid, show_home=True))
            STATES.pop(tid, None)
            return
    except (ValueError, sqlite3.IntegrityError) as e:
        send(chat_id, str(e) + "\nПопробуй ещё раз или отправь /cancel.", home_markup())


def on_callback(query):
    tid = query["from"]["id"]
    chat_id = query["message"]["chat"]["id"]
    data = query.get("data", "")
    player = user_player(tid)
    answer_callback(query["id"])
    if data == "account" and player:
        with db() as c:
            cards = c.execute("SELECT number FROM cards WHERE player_id=? AND active=1 ORDER BY id", (player["id"],)).fetchall()
        card_lines = "\n".join("• " + r["number"] for r in cards) or "Карт нет"
        rows = [[("➕ Выпустить карту", "newcard")]] if len(cards) < 5 else []
        rows.append([("🏠 Главное меню", "home")])
        send(chat_id, f"Игрок: {player['minecraft_name']}\nБаланс: {balance(player['id'])} AR\nКарты:\n{card_lines}", keyboard(rows))
    elif data == "newcard" and player:
        card = issue_card(player["id"])
        send(chat_id, f"Выпущена новая карта: {card}" if card else "У тебя уже максимум — пять карт.", menu(tid, show_home=True))
    elif data in ("home", "back"):
        STATES.pop(tid, None)
        send(chat_id, "Главное меню:", menu(tid))
    elif data == "reset_prompt" and has_permission(tid, "admin"):
        send(chat_id, "Сброс удалит всех тестовых игроков, карты, балансы, историю и движения хранилища. Роли доступа и начальный запас хранилища сохранятся.",
             keyboard([[('⚠️ Подтвердить сброс', 'reset_confirm')], [('Отмена', 'reset_cancel')]]))
    elif data == "reset_cancel" and has_permission(tid, "admin"):
        send(chat_id, "Сброс отменён.", menu(tid))
    elif data == "reset_confirm" and has_permission(tid, "admin"):
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM ledger")
            c.execute("DELETE FROM vault_events")
            c.execute("DELETE FROM cards")
            c.execute("DELETE FROM telegram_users")
            c.execute("DELETE FROM players")
        STATES.clear()
        send(chat_id, "Тестовая база очищена: аккаунты, карты, балансы и история удалены. Роли сохранены.", menu(tid))
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
        state.update(step="amount", target_card=card["number"])
        send(chat_id, f"Перевод на карту {card['number']} игрока {state['target_name']}. Введи целую сумму AR.", home_markup())
    elif data == "transfer" and player:
        begin_state(tid, "transfer")
        send(chat_id, "Введи Minecraft ник получателя или номер его карты. /cancel — отмена.", home_markup())
    elif data == "history" and player:
        send(chat_id, history_text(player["id"]), menu(tid, show_home=True))
    elif data in ("deposit", "withdraw") and has_permission(tid, "banker"):
        begin_state(tid, data)
        send(chat_id, "Введи Minecraft ник игрока или номер карты. /cancel — отмена.", home_markup())
    elif data == "summary" and has_permission(tid, "view_ledger"):
        send(chat_id, f"Игроков: {player_count()}\nAR на счетах: {total_balances()}\nAR в хранилище: {vault_cash()}\nВсе операции хранятся в журнале.", menu(tid, show_home=True))
    elif data == "ledger" and has_permission(tid, "view_ledger"):
        send(chat_id, global_history_text(), menu(tid, show_home=True))


def player_count():
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM players").fetchone()[0]


def global_history_text():
    with db() as c:
        rows = c.execute("""SELECT l.*, a.minecraft_name AS sender, b.minecraft_name AS receiver
            FROM ledger l LEFT JOIN players a ON a.id=l.from_player LEFT JOIN players b ON b.id=l.to_player
            ORDER BY l.id DESC LIMIT 20""").fetchall()
    if not rows:
        return "В журнале пока нет операций."
    lines = [f"#{r['id']} {r['kind']} · {r['sender'] or 'касса'} → {r['receiver'] or 'касса'} · {r['amount']} AR" + (f" · {r['note']}" if r['note'] else "") for r in rows]
    return "Последние операции банка:\n" + "\n".join(lines)


def main():
    init_db()
    print("Telegram bank prototype is polling. Press Ctrl+C to stop.")
    offset = 0
    while True:
        try:
            updates = api("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": json.dumps(["message", "callback_query"])})
            for update in updates:
                offset = update["update_id"] + 1
                try:
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
