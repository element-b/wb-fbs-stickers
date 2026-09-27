from __future__ import annotations

import base64
import binascii
import re
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


def normalize_sticker_part(value: object) -> str | None:
    """
    Подготавливает partA или partB WB для поиска.

    Для поиска по физическому стикеру используются только цифровые части.
    Например: `231648 9753`.
    """
    if value is None:
        return None

    text = str(value).strip()

    if not text or not text.isdigit():
        return None

    return text


def extract_distribution_center(
    supply_name: object,
    supply_id: str,
) -> str:
    """
    Извлекает название СЦ из имени поставки.

    Пример:
    `Накл Софьино от 26.09.2026` -> `Софьино`.

    Если имя поставки не соответствует ожидаемому формату, используется
    полное название поставки или её ID.
    """
    name = str(supply_name or "").strip()

    if not name:
        return f"Поставка {supply_id}"

    center_name = re.sub(
        r"^\s*накл\s+",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()

    center_name = re.split(
        r"\s+от\s+",
        center_name,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()

    return center_name or name


def validate_png_sticker(sticker: dict) -> dict:
    """
    Проверяет один стикер WB.

    Возвращает только данные, нужные приложению:
    - order_id;
    - PNG-байты;
    - part_a и part_b для поиска по цифровому коду на стикере.

    Base64-содержимое не выводится в интерфейс и не логируется.
    """
    try:
        order_id = int(sticker["orderId"])
        file_base64 = sticker["file"]

        if not isinstance(file_base64, str) or not file_base64:
            raise ValueError

        png_bytes = base64.b64decode(
            file_base64,
            validate=True,
        )

        with Image.open(BytesIO(png_bytes)) as image:
            if image.format != "PNG":
                raise ValueError

            image.verify()

        return {
            "order_id": order_id,
            "png_bytes": png_bytes,
            "part_a": normalize_sticker_part(sticker.get("partA")),
            "part_b": normalize_sticker_part(sticker.get("partB")),
        }

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
    Собирает, проверяет и группирует стикеры по точному артикулу.

    Проверки перед созданием PDF:

    - у каждого задания найдены данные;
    - у каждого задания есть непустой артикул;
    - трансграничные задания блокируют результат;
    - каждый выбранный заказ получил ровно один стикер;
    - WB не вернул лишние или дублирующиеся стикеры;
    - стикеры сопоставляются по orderId, а не по позиции в ответе;
    - для поиска перепечатки сохраняется связь partA + partB со стикером.
    """
    if not selected_supplies:
        raise DataCheckError("Выберите хотя бы одну поставку.")

    if (sticker_width, sticker_height) not in ((58, 40), (40, 30)):
        raise DataCheckError(
            "Допустимы только размеры стикера 58×40 или 40×30 мм."
        )

    # ID задания -> множество поставок.
    order_supplies: dict[int, set[str]] = defaultdict(set)

    # ID поставки -> название СЦ.
    supply_centers: dict[str, str] = {}

    empty_supplies: list[str] = []

    for supply in selected_supplies:
        supply_id = str(supply.get("id", "")).strip()

        if not supply_id:
            raise DataCheckError(
                "В выбранном списке есть поставка без корректного ID."
            )

        if is_cross_border(supply.get("crossBorderType", 0)):
            raise DataCheckError(
                f"Поставка {supply_id} отмечена как трансграничная. "
                "Для неё нужен отдельный метод WB "
                "`/api/v3/orders/stickers/cross-border`. "
                "Неполный файл не сформирован."
            )

        supply_centers[supply_id] = extract_distribution_center(
            supply_name=supply.get("name"),
            supply_id=supply_id,
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

    missing_orders = sorted(target_ids - set(orders))

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

    # Не нормализуем article: регистр и пробелы должны оставаться точными.
    article_orders: dict[str, list[int]] = defaultdict(list)

    for order_id, order in orders.items():
        article_orders[order["article"]].append(order_id)

    def order_sort_key(order_id: int) -> tuple[str, int]:
        """Стабильный порядок: ID поставки, затем ID задания."""
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

    sticker_by_order_id: dict[int, dict] = {}

    for raw_sticker in response_stickers:
        sticker = validate_png_sticker(raw_sticker)

        order_id = sticker["order_id"]

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

        sticker_by_order_id[order_id] = sticker

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

        distribution_centers = sorted(
            {
                supply_centers.get(
                    supply_id,
                    f"Поставка {supply_id}",
                )
                for supply_id in supply_ids
            },
            key=lambda value: (value.casefold(), value),
        )

        grouped.append(
            {
                "article": article,
                "order_ids": order_ids,
                "stickers": [
                    sticker_by_order_id[order_id]["png_bytes"]
                    for order_id in order_ids
                ],
                "supply_count": len(supply_ids),
                "distribution_centers": distribution_centers,
            }
        )

    # Индекс для быстрой перепечатки по паре partA + partB.
    sticker_lookup: dict[str, dict] = {}

    article_group_by_name = {
        group["article"]: group
        for group in grouped
    }

    for article, order_ids in article_orders.items():
        group = article_group_by_name[article]

        for order_id in order_ids:
            sticker = sticker_by_order_id[order_id]
            part_a = sticker["part_a"]
            part_b = sticker["part_b"]

            # В некоторых ответах WB эти поля могут отсутствовать.
            if part_a is None or part_b is None:
                continue

            lookup_key = f"{part_a} {part_b}"

            if lookup_key in sticker_lookup:
                raise DataCheckError(
                    "WB вернул одинаковый цифровой код стикера "
                    "для разных заданий. Перепечатка заблокирована."
                )

            sticker_lookup[lookup_key] = {
                "order_id": order_id,
                "article": article,
                "png_bytes": sticker["png_bytes"],
                "distribution_centers": group["distribution_centers"],
            }

    return {
        "groups": grouped,
        "supply_count": len(selected_supplies),
        "empty_supplies": empty_supplies,
        "order_count": len(target_ids),
        "article_count": len(grouped),
        "sticker_lookup": sticker_lookup,
        "searchable_sticker_count": len(sticker_lookup),
    }