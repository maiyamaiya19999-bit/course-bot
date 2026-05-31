"""
Обработка webhook от Prodamus
Проверяет подпись и извлекает данные об оплате
"""

import hashlib
import hmac
import json


def verify_signature(data: dict, secret_key: str) -> bool:
    """
    Проверяет подпись webhook от Prodamus.
    Prodamus подписывает данные HMAC-SHA256.
    Если подпись не совпадает — значит webhook поддельный.
    """
    signature = data.get("signature")
    if not signature:
        return False

    # Убираем подпись из данных для проверки
    check_data = {k: v for k, v in data.items() if k != "signature"}

    # Сортируем ключи и формируем строку
    sorted_data = sorted(check_data.items())
    check_string = "&".join(f"{k}={v}" for k, v in sorted_data if v)

    # Вычисляем HMAC-SHA256
    expected = hmac.new(
        secret_key.encode("utf-8"),
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def extract_payment_data(data: dict) -> dict:
    """
    Извлекает нужные поля из webhook Prodamus.
    Prodamus может присылать данные в разных форматах,
    поэтому проверяем несколько вариантов названий полей.
    """

    # Поля формы Tilda могут приходить в разных ключах
    # Стандартные поля Prodamus
    result = {
        "order_id": data.get("order_num", data.get("order_id", "")),
        "status": data.get("payment_status", data.get("status", "")),
        "amount": data.get("sum", data.get("amount", "")),
        "product": data.get("product_title", data.get("products", "")),
    }

    # Пользовательские поля (из формы Tilda)
    # Могут приходить в customer_extra, products[0][extra] или напрямую
    customer = data.get("customer", {})
    if isinstance(customer, str):
        try:
            customer = json.loads(customer)
        except (json.JSONDecodeError, TypeError):
            customer = {}

    result["email"] = (
        customer.get("email", "")
        or data.get("customer_email", "")
        or data.get("email", "")
    )
    result["phone"] = (
        customer.get("phone", "")
        or data.get("customer_phone", "")
        or data.get("phone", "")
    )
    result["name"] = (
        customer.get("name", "")
        or data.get("customer_name", "")
        or data.get("name", "")
    )

    # Ник в телеграм — кастомное поле из Tilda
    # Может быть в customer_extra или в отдельном поле
    result["telegram_username"] = (
        data.get("telegram", "")
        or data.get("telegram_username", "")
        or data.get("customer_telegram", "")
        or _find_telegram_in_extra(data)
        or _find_telegram_in_extra(customer)
    )

    return result


def _find_telegram_in_extra(data: dict) -> str:
    """Ищет поле с телеграм-ником в дополнительных полях"""
    if not isinstance(data, dict):
        return ""

    # Проверяем customer_extra
    extra = data.get("customer_extra", data.get("extra", {}))
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            return ""

    if isinstance(extra, dict):
        for key, value in extra.items():
            key_lower = key.lower()
            if any(word in key_lower for word in ["telegram", "телеграм", "ник", "tg"]):
                return str(value)

    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict):
                name = str(item.get("name", item.get("title", ""))).lower()
                if any(word in name for word in ["telegram", "телеграм", "ник", "tg"]):
                    return str(item.get("value", ""))

    return ""
