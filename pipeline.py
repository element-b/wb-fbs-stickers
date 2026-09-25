from __future__ import annotations

import base64
import binascii
from collections import defaultdict
from io import BytesIO

from PIL import Image

from wb_api import is_cross_border


class DataCheckError(RuntimeError):
    pass


def collect_and_group(client, selected_supplies: list[dict]) -> dict:
    if not selected_supplies:
        raise DataCheckError("Выберите хотя бы одну поставку.")

    # Один ID задания может встречаться в нескольких выбранных поставках.
    order_supplies: dict[int, set[str]] = defaultdict(set)
    empty_supplies = []

    for supply in selected_supplies:
        supply_id = str(supply["id"])

        if is_cross_border(supply.get("crossBorderType", 0)):
            raise DataCheckError(
                f"Поставка {supply_id} отмечена как трансграничная. "
                "Такие стикеры пока не поддерживаются."
            )

        ids = client.supply_order_ids(supply_id)
        if not ids:
            empty_supplies.append(supply_id)
        for order_id in ids:
            order_supplies[int(order_id)].add(supply_id)

    target_ids = set(order_supplies)
    if not target_ids:
        message = "В выбранных поставках нет сборочных заданий."
        if empty_supplies:
            message += " Пустые поставки: " + ", ".join(empty_supplies)
        raise DataCheckError(message)

    orders = client.orders_for_ids(target_ids)
    missing_orders = sorted(target_ids - set(orders))
    if missing_orders:
        raise DataCheckError(
            "Не найдены данные для заданий в окне поиска за 365 дней: "
            + ", ".join(map(str, missing_orders[:40]))
            + (f" и ещё {len(missing_orders) - 40}" if len(missing_orders) > 40 else "")
            + ". Неполный файл не сформирован."
        )

    cross_border_ids = sorted(
        order_id for order_id, order in orders.items()
        if is_cross_border(order.get("crossBorderType", 0))
    )
    if cross_border_ids:
        raise DataCheckError(
            "Среди заданий есть трансграничные: "
            + ", ".join(map(str, cross_border_ids[:40]))
            + ". Для них нужен отдельный метод WB; неполный файл не сформирован."
        )

    bad_articles = sorted(
        order_id for order_id, order in orders.items()
        if not isinstance(order.get("article"), str)
        or not order["article"].strip()
    )
    if bad_articles:
        raise DataCheckError(
            "Не найден артикул продавца у заданий: "
            + ", ".join(map(str, bad_articles[:40]))
        )

    # Не нормализуем регистр или пробелы: это точные значения WB.
    groups: dict[str, list[int]] = defaultdict(list)
    for order_id, order in orders.items():
        groups[order["article"]].append(order_id)

    def order_sort_key(order_id: int):
        return min(order_supplies[order_id]), order_id

    sorted_ids = [
        order_id
        for article in sorted(groups, key=lambda value: (value.casefold(), value))
        for order_id in sorted(groups[article], key=order_sort_key)
    ]

    response = client.stickers(sorted_ids)
    sticker_by_id = {}

    for sticker in response:
        try:
            order_id = int(sticker["orderId"])
            raw = base64.b64decode(sticker["file"], validate=True)
            with Image.open(BytesIO(raw)) as image:
                if image.format != "PNG":
                    raise ValueError
                image.verify()
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise DataCheckError("WB вернул повреждённый или некорректный PNG.")

        if order_id not in target_ids:
            raise DataCheckError("WB вернул лишний стикер; файл не сформирован.")
        if order_id in sticker_by_id:
            raise DataCheckError(
                f"WB вернул повторный стикер для задания {order_id}."
            )
        sticker_by_id[order_id] = raw

    missing_stickers = sorted(target_ids - set(sticker_by_id))
    if missing_stickers:
        raise DataCheckError(
            "WB не вернул стикеры для заданий: "
            + ", ".join(map(str, missing_stickers[:40]))
            + ". Возможно, задания не в статусе «На сборке» или «В доставке»."
        )

    if len(sticker_by_id) != len(target_ids):
        raise DataCheckError("Число стикеров не совпало с числом заданий.")

    grouped = []
    for article in sorted(groups, key=lambda value: (value.casefold(), value)):
        ids = sorted(groups[article], key=order_sort_key)
        supply_ids = {
            supply_id for order_id in ids
            for supply_id in order_supplies[order_id]
        }
        grouped.append({
            "article": article,
            "order_ids": ids,
            "stickers": [sticker_by_id[order_id] for order_id in ids],
            "supply_count": len(supply_ids),
        })

    return {
        "groups": grouped,
        "supply_count": len(selected_supplies),
        "empty_supplies": empty_supplies,
        "order_count": len(target_ids),
        "article_count": len(grouped),
    }