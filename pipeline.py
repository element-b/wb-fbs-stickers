from __future__ import annotations

import base64
import binascii
from collections import defaultdict
from io import BytesIO
from typing import Any

from PIL import Image

from wb_api import is_cross_border


class DataCheckError(RuntimeError):
    """Ошибка полноты или целостности данных перед созданием PDF."""


def format_ids(
    values: list[int],
    max_items: int = 40,
) -> str:
    """Форматирует список ID для безопасного сообщения пользователю."""
    shown = values[:max_items]
    text = ", ".join(map(str, shown))

    if len(values) > max_items:
        text += f" и ещё {len(values) - max_items}"

    return text


def validate_png_sticker(sticker: dict) -> tuple[int, bytes]:
    """
    Проверяет один стикер WB и возвращает пару ID задания и PNG-байты.

    Не сохраняет base64-строку и не выводит её в сообщения об ошибках.
    """
    try:
        order_id = int(sticker["orderId"])
        file_base64 = sticker["file"]

        if not isinstance(file_base64, str) or not file_base64:
            raise ValueError

        raw_bytes = base64.b64decode(
            file_base64,
            validate=True,
        )

        with Image.open(BytesIO(raw_bytes)) as image:
            if image.format != "PNG":
                raise ValueError

            image.verify()

        return order_id, raw_bytes

    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        OSError,
    ) as error:
        raise DataCheckError(
            "WB вернул повреждённый или некорректный PNG-стикер."
        ) from error


def collect_and_group(
    client: Any,
    selected_supplies: list[dict],
    lookback_days: int,
    sticker_width: int,
    sticker_height: int,
) -> dict:
    """
    Собирает, проверяет и группирует стикеры по точному артикулу продавца.

    Инварианты перед созданием PDF:

    - для каждого выбранного задания найдены данные заказа;
    - у каждого задания определён непустой article;
    - трансграничные задания блокируют обычный PDF;
    - получен ровно один стикер для каждого задания;
    - WB не вернул лишние или повторные стикеры;
    - все стикеры сопоставлены по orderId, а не по позиции в ответе.
    """
    if not selected_supplies:
        raise DataCheckError("Выберите хотя бы одну поставку.")

    if (sticker_width, sticker_height) not in ((58, 40), (40, 30)):
        raise DataCheckError(
            "Допустимы только размеры стикера 58×40 или 40×30 мм."
        )

    # ID задания -> множество поставок, где это задание найдено.
    order_supplies: dict[int, set[str]] = defaultdict(set)
    empty_supplies: list[str] = []

    for supply in selected_supplies:
        supply_id = str(supply.get("id", "")).strip()

        if not supply_id:
            raise DataCheckError(
                "В выбранном списке есть поставка без корректного ID."
            )

        # Поставку можно заблокировать сразу, если WB вернул её тип.
        if is_cross_border(supply.get("crossBorderType", 0)):
            raise DataCheckError(
                f"Поставка {supply_id} отмечена как трансграничная. "
                "Для неё требуется отдельный метод WB "
                "`/api/v3/orders/stickers/cross-border`. "
                "Неполный файл не сформирован."
            )

        order_ids = client.supply_order_ids(supply_id)

        if not order_ids:
            empty_supplies.append(supply_id)
            continue

        for order_id in order_ids:
            order_supplies[int(order_id)].add(supply_id)

    target_ids = set(order_supplies)

    if not target_ids:
        message = "В выбранных поставках нет сборочных заданий."

        if empty_supplies:
            message += (
                " Пустые поставки: "
                + ", ".join(empty_supplies)
                + "."
            )

        raise DataCheckError(message)

    orders = client.orders_for_ids(
        target_ids=target_ids,
        lookback_days=lookback_days,
    )

    found_ids = set(orders)
    missing_orders = sorted(target_ids - found_ids)

    if missing_orders:
        raise DataCheckError(
            "Не найдены данные для заданий за выбранный период "
            f"последних {lookback_days} дней: "
            f"{format_ids(missing_orders)}. "
            "Увеличьте период поиска либо проверьте задания в WB Seller. "
            "Неполный файл не сформирован."
        )

    cross_border_ids = sorted(
        order_id
        for order_id, order in orders.items()
        if is_cross_border(order.get("crossBorderType", 0))
    )

    if cross_border_ids:
        raise DataCheckError(
            "Среди выбранных заданий найдены трансграничные: "
            f"{format_ids(cross_border_ids)}. "
            "Для них нужен отдельный метод WB "
            "`/api/v3/orders/stickers/cross-border`. "
            "Неполный файл не сформирован."
        )

    invalid_article_ids = sorted(
        order_id
        for order_id, order in orders.items()
        if not isinstance(order.get("article"), str)
        or not order["article"].strip()
    )

    if invalid_article_ids:
        raise DataCheckError(
            "Не найден артикул продавца у заданий: "
            f"{format_ids(invalid_article_ids)}. "
            "Неполный файл не сформирован."
        )

    # Важно: значения article используются как есть.
    # Не меняем регистр, не склеиваем пробелы и не заменяем article на nmId.
    article_orders: dict[str, list[int]] = defaultdict(list)

    for order_id, order in orders.items():
        article_orders[order["article"]].append(order_id)

    def order_sort_key(order_id: int) -> tuple[str, int]:
        """
        Стабильный порядок внутри артикула:
        сначала ID поставки, затем ID задания.
        """
        return (
            min(order_supplies[order_id]),
            order_id,
        )

    sorted_order_ids = [
        order_id
        for article in sorted(
            article_orders,
            key=lambda value: (value.casefold(), value),
        )
        for order_id in sorted(
            article_orders[article],
            key=order_sort_key,
        )
    ]

    response_stickers = client.stickers(
        order_ids=sorted_order_ids,
        width=sticker_width,
        height=sticker_height,
    )

    sticker_by_order_id: dict[int, bytes] = {}

    for sticker in response_stickers:
        order_id, png_bytes = validate_png_sticker(sticker)

        if order_id not in target_ids:
            raise DataCheckError(
                "WB вернул стикер для задания, которого нет "
                "в выбранных поставках. Файл не сформирован."
            )

        if order_id in sticker_by_order_id:
            raise DataCheckError(
                f"WB вернул повторный стикер для задания {order_id}. "
                "Файл не сформирован."
            )

        sticker_by_order_id[order_id] = png_bytes

    returned_ids = set(sticker_by_order_id)

    missing_stickers = sorted(target_ids - returned_ids)

    if missing_stickers:
        raise DataCheckError(
            "WB не вернул стикеры для заданий: "
            f"{format_ids(missing_stickers)}. "
            "Стикеры выдаются WB только для заданий в статусах "
            "confirm («На сборке») и complete («В доставке»). "
            "Неполный файл не сформирован."
        )

    extra_stickers = sorted(returned_ids - target_ids)

    if extra_stickers:
        raise DataCheckError(
            "WB вернул лишние стикеры: "
            f"{format_ids(extra_stickers)}. "
            "Файл не сформирован."
        )

    if len(sticker_by_order_id) != len(target_ids):
        raise DataCheckError(
            "Число стикеров не совпало с числом выбранных заданий. "
            "Файл не сформирован."
        )

    grouped: list[dict] = []

    for article in sorted(
        article_orders,
        key=lambda value: (value.casefold(), value),
    ):
        order_ids = sorted(
            article_orders[article],
            key=order_sort_key,
        )

        supply_ids = {
            supply_id
            for order_id in order_ids
            for supply_id in order_supplies[order_id]
        }

        grouped.append(
            {
                "article": article,
                "order_ids": order_ids,
                "stickers": [
                    sticker_by_order_id[order_id]
                    for order_id in order_ids
                ],
                "supply_count": len(supply_ids),
            }
        )

    return {
        "groups": grouped,
        "supply_count": len(selected_supplies),
        "empty_supplies": empty_supplies,
        "order_count": len(target_ids),
        "article_count": len(grouped),
    }