"""
База данных оплат — PostgreSQL (Supabase)
Хранит данные об оплативших учениках в облаке — не теряется при перезапуске
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL", "")


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    """Создаёт таблицы при первом запуске"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            telegram_username TEXT,
            email TEXT,
            phone TEXT,
            name TEXT,
            product TEXT,
            amount TEXT,
            order_id TEXT,
            paid_at TIMESTAMP DEFAULT NOW(),
            verified INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def normalize_username(username):
    """
    Нормализует username: убирает @, пробелы, приводит к нижнему регистру
    @Ivanova_M  →  ivanova_m
    """
    if not username:
        return None
    return username.strip().lstrip("@").lower()


def normalize_phone(phone):
    """
    Нормализует номер телефона: оставляет только цифры
    +7 (999) 123-45-67  →  79991234567
    """
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits if digits else None


def add_payment(telegram_username, email, phone, name, product="", amount="", order_id=""):
    """Сохраняет данные об оплате"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO payments (telegram_username, email, phone, name, product, amount, order_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
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
    cur.close()
    conn.close()


def find_payment_by_username(username):
    """Ищет оплату по Telegram username"""
    username = normalize_username(username)
    if not username:
        return None
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM payments WHERE telegram_username = %s ORDER BY paid_at DESC LIMIT 1",
        (username,),
    )
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result
