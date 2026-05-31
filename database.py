"""
База данных оплат — SQLite (бесплатно, без подписок)
Хранит данные об оплативших учениках
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "payments.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Создаёт таблицу оплат при первом запуске"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_username TEXT,
            email TEXT,
            phone TEXT,
            name TEXT,
            product TEXT,
            amount TEXT,
            order_id TEXT,
            paid_at TEXT DEFAULT CURRENT_TIMESTAMP,
            verified INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER UNIQUE,
            telegram_username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            warned INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def normalize_username(username):
    """
    Нормализует username: убирает @, пробелы, приводит к нижнему регистру
    @Ivanova_M  →  ivanova_m
    ivanova_m   →  ivanova_m
     @test      →  test
    """
    if not username:
        return None
    return username.strip().lstrip("@").lower()


def add_payment(telegram_username, email, phone, name, product="", amount="", order_id=""):
    """Сохраняет данные об оплате"""
    conn = get_db()
    conn.execute(
        """INSERT INTO payments (telegram_username, email, phone, name, product, amount, order_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            normalize_username(telegram_username),
            email.strip().lower() if email else None,
            normalize_phone(phone),
            name.strip() if name else None,
            product,
            amount,
            order_id,
        ),
    )
    conn.commit()
    conn.close()


def normalize_phone(phone):
    """
    Нормализует номер телефона: оставляет только цифры
    +7 (999) 123-45-67  →  79991234567
    8-999-123-45-67     →  89991234567
    """
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits if digits else None


def find_payment_by_username(username):
    """Ищет оплату по Telegram username"""
    username = normalize_username(username)
    if not username:
        return None
    conn = get_db()
    result = conn.execute(
        "SELECT * FROM payments WHERE telegram_username = ? ORDER BY paid_at DESC LIMIT 1",
        (username,),
    ).fetchone()
    conn.close()
    return result


def find_payment_by_phone(phone):
    """Ищет оплату по номеру телефона"""
    phone = normalize_phone(phone)
    if not phone:
        return None
    conn = get_db()
    # Ищем по последним 10 цифрам (на случай 8 vs +7)
    result = conn.execute(
        "SELECT * FROM payments WHERE phone LIKE ? ORDER BY paid_at DESC LIMIT 1",
        (f"%{phone[-10:]}",),
    ).fetchone()
    conn.close()
    return result


def find_payment_by_email(email):
    """Ищет оплату по email"""
    if not email:
        return None
    email = email.strip().lower()
    conn = get_db()
    result = conn.execute(
        "SELECT * FROM payments WHERE email = ? ORDER BY paid_at DESC LIMIT 1",
        (email,),
    ).fetchone()
    conn.close()
    return result


def add_pending_user(user_id, username, first_name, last_name):
    """Добавляет пользователя в список ожидающих проверки"""
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO pending_users (telegram_user_id, telegram_username, first_name, last_name)
           VALUES (?, ?, ?, ?)""",
        (user_id, normalize_username(username), first_name, last_name),
    )
    conn.commit()
    conn.close()


def remove_pending_user(user_id):
    """Удаляет пользователя из списка ожидающих"""
    conn = get_db()
    conn.execute("DELETE FROM pending_users WHERE telegram_user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_pending_users():
    """Возвращает всех ожидающих проверки"""
    conn = get_db()
    result = conn.execute("SELECT * FROM pending_users").fetchall()
    conn.close()
    return result


def mark_payment_verified(payment_id):
    """Отмечает оплату как проверенную"""
    conn = get_db()
    conn.execute("UPDATE payments SET verified = 1 WHERE id = ?", (payment_id,))
    conn.commit()
    conn.close()
