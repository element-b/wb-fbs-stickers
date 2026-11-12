from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import streamlit as st

from mysklad_api import (
    FinishedProductsScope,
    MySkladApiError,
    MySkladClient,
)


FINISHED_PRODUCTS_FOLDER_NAME = "Готовая продукция"

PHYSICAL_ASSORTMENT_TYPES = {
    "product",
    "variant",
}

MARKETPLACE_WB = "WB FBO"
MARKETPLACE_OZON = "Ozon FBO"

MARKETPLACE_SORT_ORDER = {
    MARKETPLACE_WB: 0,
    MARKETPLACE_OZON: 1,
}


def _value_to_date(value: object) -> date | None:
    """Преобразует значение Streamlit в date."""
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return None


def _parse_date_range(
    selected_value: object,
) -> tuple[date | None, date | None]:
    """Разбирает одну дату или диапазон из st.date_input."""
    if isinstance(selected_value, (tuple, list)):
        values = list(selected_value)

        if not values:
            return None, None

        date_from = _value_to_date(values[0])

        if date_from is None:
            return None, None

        date_to = (
            _value_to_date(values[1])
            if len(values) > 1
            else date_from
        )

        if date_to is None:
            date_to = date_from

        return (
            min(date_from, date_to),
            max(date_from, date_to),
        )

    selected_date = _value_to_date(selected_value)

    return selected_date, selected_date


def _format_date_range(
    date_from: date,
    date_to: date,
) -> str:
    """Форматирует период."""
    if date_from == date_to:
        return date_from.strftime("%d.%m.%Y")

    return (
        f"{date_from.strftime('%d.%m.%Y')} — "
        f"{date_to.strftime('%d.%m.%Y')}"
    )


def _parse_moment(value: object) -> datetime | None:
    """Преобразует moment МоегоСклада в datetime."""
    if not isinstance(value, str) or not value.strip():
        return None

    for pattern in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(
                value.strip(),
                pattern,
            )
        except ValueError:
            continue

    return None


def _format_moment(value: object) -> str:
    """Форматирует дату документа."""
    parsed = _parse_moment(value)

    if parsed is None:
        return str(value or "Дата не определена").strip()

    return parsed.strftime("%d.%m.%Y %H:%M")


def _entity_id(value: object) -> str:
    """Извлекает ID сущности из id либо meta.href."""
    if not isinstance(value, dict):
        return ""

    direct_id = str(value.get("id") or "").strip()

    if direct_id:
        return direct_id

    meta = value.get("meta")

    if not isinstance(meta, dict):
        return ""

    meta_id = str(meta.get("id") or "").strip()

    if meta_id:
        return meta_id

    href = str(meta.get("href") or "").strip().rstrip("/")

    if not href:
        return ""

    return href.rsplit("/", maxsplit=1)[-1]


def _normal_text(value: object) -> str:
    """Нормализует текст для поиска."""
    return " ".join(
        str(value or "").strip().split()
    ).casefold()


def _status_id(value: object) -> str:
    """Возвращает ID статуса."""
    return _entity_id(value)


def _build_state_name_map(
    states: list[dict],
    document_label: str,
) -> dict[str, str]:
    """Строит соответствие ID статуса и названия."""
    result: dict[str, str] = {}

    for state in states:
        if not isinstance(state, dict):
            continue

        state_id = _status_id(state)
        state_name = str(state.get("name") or "").strip()

        if not state_id or not state_name:
            raise MySkladApiError(
                "МойСклад вернул некорректный статус "
                f"для документа «{document_label}»."
            )

        previous_name = result.get(state_id)

        if previous_name and previous_name != state_name:
            raise MySkladApiError(
                "МойСклад вернул один ID статуса "
                f"«{document_label}» с разными названиями."
            )

        result[state_id] = state_name

    if not result:
        raise MySkladApiError(
            "МойСклад не вернул статусы "
            f"для документа «{document_label}»."
        )

    return result


def _status_name(
    document: dict,
    state_names: dict[str, str],
) -> str:
    """Возвращает отображаемое название статуса."""
    state = document.get("state")

    if not isinstance(state, dict):
        return "Без статуса"

    embedded_name = str(state.get("name") or "").strip()

    if embedded_name:
        return embedded_name

    return state_names.get(
        _status_id(state),
        "Статус не определён",
    )


def _order_number(order: dict) -> str:
    """Возвращает номер заказа."""
    return str(order.get("name") or "").strip() or "Без номера"


def _demand_number(demand: dict) -> str:
    """Возвращает номер отгрузки."""
    return str(demand.get("name") or "").strip() or "Без номера"


def _agent_name(document: dict) -> str:
    """
    Возвращает название контрагента документа.

    Для каталога используется именно `demand.agent`: он является
    надёжным источником контрагента фактической отгрузки.
    """
    agent = document.get("agent")

    if not isinstance(agent, dict):
        return ""

    return str(agent.get("name") or "").strip()


def _marketplace_from_counterparty_name(
    counterparty_name: str,
) -> str | None:
    """
    Определяет WB или Ozon по контрагенту отгрузки.

    Обязательное слово FBO не требуется: в ваших данных FBS-отгрузки
    не имеют связи `customerOrder`, поэтому отсекаются раньше.

    Поддерживаются маркеры:

    - Ozon / Озон;
    - WB / ВБ;
    - Wildberries / Вайлдберриз.

    Если площадку невозможно определить однозначно, документ
    не включается в выбор.
    """
    text = _normal_text(counterparty_name)

    if not text:
        return None

    has_ozon = (
        "ozon" in text
        or "озон" in text
    )

    has_wb = (
        "wildberries" in text
        or "вайлдберриз" in text
        or "вб" in text
        or " wb " in f" {text} "
    )

    if has_ozon and has_wb:
        return None

    if has_ozon:
        return MARKETPLACE_OZON

    if has_wb:
        return MARKETPLACE_WB

    return None


def _linked_demand_ids(order: dict) -> list[str]:
    """Возвращает все уникальные связанные отгрузки заказа."""
    raw_demands = order.get("demands")

    if raw_demands is None:
        return []

    if not isinstance(raw_demands, list):
        raise MySkladApiError(
            "МойСклад вернул некорректное поле `demands` "
            "у заказа покупателя."
        )

    result: list[str] = []
    seen_ids: set[str] = set()

    for reference in raw_demands:
        demand_id = _entity_id(reference)

        if not demand_id:
            raise MySkladApiError(
                "МойСклад вернул связанную отгрузку без корректного ID."
            )

        if demand_id not in seen_ids:
            seen_ids.add(demand_id)
            result.append(demand_id)

    return result


def _quantity_as_int(
    value: object,
    document_number: str,
    document_label: str,
) -> int:
    """Проверяет целое неотрицательное количество."""
    if isinstance(value, bool):
        raise MySkladApiError(
            "МойСклад вернул некорректное количество "
            f"в документе «{document_label}» `{document_number}`."
        )

    try:
        quantity = Decimal(str(value))
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ) as error:
        raise MySkladApiError(
            "МойСклад вернул некорректное количество "
            f"в документе «{document_label}» `{document_number}`."
        ) from error

    if not quantity.is_finite() or quantity < 0:
        raise MySkladApiError(
            "МойСклад вернул некорректное количество "
            f"в документе «{document_label}» `{document_number}`."
        )

    if quantity != quantity.to_integral_value():
        raise MySkladApiError(
            "МойСклад вернул дробное количество "
            f"в документе «{document_label}» `{document_number}`. "
            "Диагностика поддерживает только штучную продукцию."
        )

    return int(quantity)


def _article_from_assortment(
    assortment: dict,
    document_number: str,
    document_label: str,
) -> str:
    """
    Возвращает артикул товара.

    Для variant сначала используется article модификации. При его
    отсутствии допускается article развёрнутого основного товара.
    """
    article = str(assortment.get("article") or "").strip()

    if article:
        return article

    product = assortment.get("product")

    if isinstance(product, dict):
        parent_article = str(
            product.get("article") or ""
        ).strip()

        if parent_article:
            return parent_article

    raise MySkladApiError(
        "У готовой продукции в документе "
        f"«{document_label}» `{document_number}` не заполнен артикул."
    )


def _positions_by_article(
    client: MySkladClient,
    scope: FinishedProductsScope,
    positions: list[dict],
    document_number: str,
    document_label: str,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Суммирует по article только позиции готовой продукции.

    Расходники, гофрокороба, этикет-лента, услуги, комплекты
    и товары вне группы «Готовая продукция» не участвуют в сверке.
    """
    by_article: dict[str, int] = {}

    stats = {
        "source_positions": 0,
        "included_positions": 0,
        "excluded_positions": 0,
        "included_quantity": 0,
    }

    for position in positions:
        if not isinstance(position, dict):
            raise MySkladApiError(
                "МойСклад вернул некорректную позицию "
                f"в документе «{document_label}» `{document_number}`."
            )

        assortment = position.get("assortment")

        if not isinstance(assortment, dict):
            raise MySkladApiError(
                "МойСклад не вернул развёрнутую номенклатуру "
                f"в документе «{document_label}» `{document_number}`."
            )

        meta = assortment.get("meta")

        if not isinstance(meta, dict):
            raise MySkladApiError(
                "МойСклад вернул номенклатуру без `meta` "
                f"в документе «{document_label}» `{document_number}`."
            )

        stats["source_positions"] += 1

        assortment_type = str(meta.get("type") or "").strip()

        if assortment_type not in PHYSICAL_ASSORTMENT_TYPES:
            stats["excluded_positions"] += 1
            continue

        folder_id = client.assortment_product_folder_id(
            assortment=assortment
        )

        if not folder_id:
            stats["excluded_positions"] += 1
            continue

        if folder_id not in scope.included_folder_ids:
            stats["excluded_positions"] += 1
            continue

        article = _article_from_assortment(
            assortment=assortment,
            document_number=document_number,
            document_label=document_label,
        )

        quantity = _quantity_as_int(
            value=position.get("quantity"),
            document_number=document_number,
            document_label=document_label,
        )

        by_article[article] = (
            by_article.get(article, 0)
            + quantity
        )

        stats["included_positions"] += 1
        stats["included_quantity"] += quantity

    return by_article, stats


def _catalog_label(
    demand_id: str,
    catalog_by_demand_id: dict[str, dict],
) -> str:
    """Формирует подпись отгрузки в мультивыборе."""
    row = catalog_by_demand_id[demand_id]

    return (
        f"{row['Маркетплейс']}  |  "
        f"{row['Контрагент']}  |  "
        f"заказ {row['Заказ']}  |  "
        f"отгрузка {row['Отгрузка']}  |  "
        f"{row['Дата отгрузки']}"
    )


def _manager_text(
    marketplace: str,
    result: dict[str, Any],
) -> str:
    """
    Формирует copy-ready сообщение для менеджера WB или Ozon.

    Сообщение содержит отдельные заказы и общую итоговую сводку
    недобора / перебора по артикулам.
    """
    marketplace_orders = [
        row
        for row in result["order_summary_rows"]
        if row["_marketplace"] == marketplace
    ]

    marketplace_details = [
        row
        for row in result["detail_rows"]
        if row["_marketplace"] == marketplace
    ]

    if not marketplace_orders:
        return (
            f"{marketplace}\n"
            "За выбранный период нет отгрузок этого маркетплейса."
        )

    details_by_order_id: dict[str, list[dict]] = defaultdict(list)

    for row in marketplace_details:
        details_by_order_id[row["_order_id"]].append(row)

    lines = [
        f"{marketplace} — сверка заказов и отгрузок",
        (
            "Учитывается только готовая продукция из раздела "
            f"«{result['finished_products_folder_name']}»."
        ),
        (
            "Расходники, этикет-лента, гофрокороба и прочие позиции "
            "вне готовой продукции исключены."
        ),
        "",
        "Детализация по заказам:",
    ]

    for order_row in marketplace_orders:
        order_id = order_row["_order_id"]

        lines.extend(
            [
                "",
                (
                    f"Заказ: {order_row['Заказ']} "
                    f"| Статус: {order_row['Статус заказа']}"
                ),
                (
                    "Отгрузки периода: "
                    f"{order_row['Выбранные отгрузки']}"
                ),
            ]
        )

        if order_row["Есть отгрузки вне периода"] == "Да":
            lines.append(
                "Внимание: у заказа есть связанные отгрузки вне "
                "выбранного периода. В расчёте учтены только "
                "отгрузки текущего периода."
            )

        detail_rows = sorted(
            details_by_order_id.get(order_id, []),
            key=lambda row: (
                row["Артикул"].casefold(),
                row["Артикул"],
            ),
        )

        if not detail_rows:
            lines.append(
                "Готовая продукция: товарные позиции не найдены."
            )
            continue

        for row in detail_rows:
            deviation = row["Отклонение"]

            if deviation < 0:
                deviation_text = (
                    f"недобор {abs(deviation)}"
                )
            elif deviation > 0:
                deviation_text = (
                    f"перебор {deviation}"
                )
            else:
                deviation_text = "совпадает"

            lines.append(
                "• "
                f"{row['Артикул']} — "
                f"заказано {row['Заказано']}, "
                f"отгружено {row['В выбранных отгрузках']}, "
                f"{deviation_text}."
            )

    aggregate: dict[str, dict[str, int]] = {}

    for row in marketplace_details:
        article = row["Артикул"]

        aggregate_row = aggregate.setdefault(
            article,
            {
                "ordered": 0,
                "shipped": 0,
            },
        )

        aggregate_row["ordered"] += row["Заказано"]
        aggregate_row["shipped"] += (
            row["В выбранных отгрузках"]
        )

    total_shortage = 0
    total_overage = 0

    lines.extend(
        [
            "",
            "Сводка по всем выбранным заказам:",
        ]
    )

    for article in sorted(
        aggregate,
        key=lambda value: (
            value.casefold(),
            value,
        ),
    ):
        row = aggregate[article]

        deviation = row["shipped"] - row["ordered"]

        if deviation < 0:
            deviation_text = (
                f"недобор {abs(deviation)}"
            )
            total_shortage += abs(deviation)
        elif deviation > 0:
            deviation_text = (
                f"перебор {deviation}"
            )
            total_overage += deviation
        else:
            deviation_text = "совпадает"

        lines.append(
            "• "
            f"{article} — "
            f"заказано {row['ordered']}, "
            f"отгружено {row['shipped']}, "
            f"{deviation_text}."
        )

    lines.extend(
        [
            "",
            f"Итого недобор: {total_shortage}.",
            f"Итого перебор: {total_overage}.",
        ]
    )

    return "\n".join(lines)


def build_reconciliation_catalog(
    client: MySkladClient,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    """
    Создаёт каталог FBO-отгрузок периода.

    Период применяется строго к `demand.moment`.

    Алгоритм:

    1. Получает отгрузки в периоде.
    2. Исключает FBS-документы без `customerOrder`.
    3. Получает агент отгрузки через `expand=agent`.
    4. Определяет WB или Ozon по названию агента отгрузки.
    5. Получает связанный заказ для последующей сверки.
    """
    demands = client.demands_by_moment(
        date_from=date_from,
        date_to=date_to,
    )

    rows: list[dict] = []
    seen_demand_ids: set[str] = set()
    order_cache: dict[str, dict] = {}

    total_demands_in_period = 0
    without_customer_order_count = 0
    non_marketplace_count = 0

    for demand_list_row in demands:
        if not isinstance(demand_list_row, dict):
            continue

        demand_id = str(
            demand_list_row.get("id") or ""
        ).strip()

        if not demand_id:
            raise MySkladApiError(
                "МойСклад вернул отгрузку без корректного ID."
            )

        if demand_id in seen_demand_ids:
            raise MySkladApiError(
                "МойСклад вернул одну отгрузку повторно. "
                "Список не сформирован, чтобы исключить двойной учёт."
            )

        seen_demand_ids.add(demand_id)
        total_demands_in_period += 1

        demand = client.demand_by_id(demand_id)

        if str(demand.get("id") or "").strip() != demand_id:
            raise MySkladApiError(
                "МойСклад вернул отгрузку с ID, не совпадающим "
                "с запрошенным."
            )

        order_id = _entity_id(
            demand.get("customerOrder")
        )

        if not order_id:
            without_customer_order_count += 1
            continue

        counterparty_name = _agent_name(demand)

        marketplace = _marketplace_from_counterparty_name(
            counterparty_name
        )

        if marketplace is None:
            non_marketplace_count += 1
            continue

        order = order_cache.get(order_id)

        if order is None:
            order = client.customer_order_by_id(
                customer_order_id=order_id
            )

            if str(order.get("id") or "").strip() != order_id:
                raise MySkladApiError(
                    "МойСклад вернул заказ с ID, не совпадающим "
                    "с запрошенным."
                )

            order_cache[order_id] = order

        rows.append(
            {
                "demand_id": demand_id,
                "order_id": order_id,
                "marketplace": marketplace,
                "counterparty_name": counterparty_name,
                "demand_number": _demand_number(demand),
                "demand_moment": _format_moment(
                    demand.get("moment")
                ),
                "order_number": _order_number(order),
            }
        )

    rows.sort(
        key=lambda row: (
            MARKETPLACE_SORT_ORDER.get(
                row["marketplace"],
                99,
            ),
            row["counterparty_name"].casefold(),
            row["counterparty_name"],
            row["order_number"].casefold(),
            row["order_number"],
            row["demand_moment"],
            row["demand_number"].casefold(),
            row["demand_number"],
        )
    )

    return {
        "date_from": date_from,
        "date_to": date_to,
        "demand_rows": rows,
        "total_demands_in_period": total_demands_in_period,
        "without_customer_order_count": (
            without_customer_order_count
        ),
        "non_marketplace_count": non_marketplace_count,
    }


def build_order_shipment_reconciliation(
    client: MySkladClient,
    catalog: dict[str, Any],
    selected_demand_ids: set[str],
) -> dict[str, Any]:
    """
    Сверяет готовую продукцию выбранных отгрузок и связанных заказов.

    Формула:

    ```text
    В выбранных отгрузках − В заказе = Отклонение
    ```

    В сравнение включаются только отгрузки, выбранные пользователем
    из текущего периода. Дополнительные отгрузки заказа вне периода
    не прибавляются, чтобы не смешивать разные даты фактической
    отгрузки.
    """
    demand_rows = catalog.get("demand_rows")

    if not isinstance(demand_rows, list):
        raise ValueError(
            "Список отгрузок устарел. Загрузите его заново."
        )

    normalized_selected_ids = {
        str(demand_id).strip()
        for demand_id in selected_demand_ids
        if str(demand_id).strip()
    }

    if not normalized_selected_ids:
        raise ValueError(
            "Выберите хотя бы одну FBO-отгрузку."
        )

    catalog_by_demand_id = {
        str(row.get("demand_id") or "").strip(): row
        for row in demand_rows
        if isinstance(row, dict)
    }

    unknown_demand_ids = (
        normalized_selected_ids
        - set(catalog_by_demand_id)
    )

    if unknown_demand_ids:
        raise ValueError(
            "Одна или несколько выбранных отгрузок отсутствуют "
            "в текущем списке. Обновите список и повторите расчёт."
        )

    scope = client.finished_products_scope(
        root_folder_name=FINISHED_PRODUCTS_FOLDER_NAME
    )

    order_state_names = _build_state_name_map(
        states=client.customer_order_states(),
        document_label="Заказ покупателя",
    )

    demand_state_names = _build_state_name_map(
        states=client.demand_states(),
        document_label="Отгрузка",
    )

    selected_ids_by_order_id: dict[str, list[str]] = defaultdict(list)

    for demand_id in normalized_selected_ids:
        order_id = catalog_by_demand_id[demand_id]["order_id"]

        selected_ids_by_order_id[order_id].append(demand_id)

    order_catalog_rows: dict[str, dict] = {}

    for demand_id in normalized_selected_ids:
        row = catalog_by_demand_id[demand_id]
        order_id = row["order_id"]

        previous_row = order_catalog_rows.get(order_id)

        if (
            previous_row is not None
            and previous_row["marketplace"] != row["marketplace"]
        ):
            raise MySkladApiError(
                "Один заказ связан с отгрузками разных маркетплейсов. "
                "Диагностика остановлена."
            )

        order_catalog_rows[order_id] = row

    aggregate_by_key: dict[
        tuple[str, str],
        dict[str, Any],
    ] = {}

    order_summary_rows: list[dict] = []
    demand_diagnostic_rows: list[dict] = []
    detail_rows: list[dict] = []
    problem_rows: list[dict] = []

    total_finished_order_quantity = 0
    total_finished_demand_quantity = 0
    total_excluded_positions = 0
    total_status_mismatches = 0
    total_additional_demands = 0

    for order_id in sorted(
        selected_ids_by_order_id,
        key=lambda value: (
            MARKETPLACE_SORT_ORDER.get(
                order_catalog_rows[value]["marketplace"],
                99,
            ),
            order_catalog_rows[value][
                "counterparty_name"
            ].casefold(),
            order_catalog_rows[value][
                "order_number"
            ].casefold(),
            order_catalog_rows[value]["order_number"],
        ),
    ):
        catalog_row = order_catalog_rows[order_id]

        marketplace = catalog_row["marketplace"]

        order = client.customer_order_by_id(
            customer_order_id=order_id
        )

        if str(order.get("id") or "").strip() != order_id:
            raise MySkladApiError(
                "МойСклад вернул заказ с ID, не совпадающим "
                "с запрошенным."
            )

        order_number = _order_number(order)

        order_status = _status_name(
            document=order,
            state_names=order_state_names,
        )

        order_positions = client.customer_order_positions(
            customer_order_id=order_id,
            expand_assortment=True,
        )

        ordered_by_article, order_stats = _positions_by_article(
            client=client,
            scope=scope,
            positions=order_positions,
            document_number=order_number,
            document_label="Заказ покупателя",
        )

        selected_ids = set(
            selected_ids_by_order_id[order_id]
        )

        linked_demand_ids = set(
            _linked_demand_ids(order)
        )

        additional_demand_ids = (
            linked_demand_ids - selected_ids
        )

        total_additional_demands += len(
            additional_demand_ids
        )

        shipped_by_article: dict[str, int] = {}
        selected_demand_numbers: list[str] = []

        order_shipped_quantity = 0
        order_excluded_positions = order_stats[
            "excluded_positions"
        ]

        for demand_id in sorted(selected_ids):
            demand = client.demand_by_id(demand_id)

            if str(demand.get("id") or "").strip() != demand_id:
                raise MySkladApiError(
                    "МойСклад вернул отгрузку с ID, не совпадающим "
                    "с запрошенным."
                )

            reverse_order_id = _entity_id(
                demand.get("customerOrder")
            )

            if reverse_order_id != order_id:
                raise MySkladApiError(
                    "Выбранная отгрузка больше не связана "
                    "с ожидаемым заказом. Обновите список."
                )

            demand_counterparty_name = _agent_name(demand)

            demand_marketplace = _marketplace_from_counterparty_name(
                demand_counterparty_name
            )

            if demand_marketplace != marketplace:
                raise MySkladApiError(
                    "Контрагент выбранной отгрузки был изменён "
                    "или не определяется как WB / Ozon. "
                    "Обновите список отгрузок."
                )

            demand_number = _demand_number(demand)

            demand_status = _status_name(
                document=demand,
                state_names=demand_state_names,
            )

            statuses_match = (
                _normal_text(order_status)
                == _normal_text(demand_status)
            )

            if not statuses_match:
                total_status_mismatches += 1

            demand_positions = client.demand_positions(
                demand_id=demand_id,
                expand_assortment=True,
            )

            (
                demand_by_article,
                demand_stats,
            ) = _positions_by_article(
                client=client,
                scope=scope,
                positions=demand_positions,
                document_number=demand_number,
                document_label="Отгрузка",
            )

            for article, quantity in demand_by_article.items():
                shipped_by_article[article] = (
                    shipped_by_article.get(article, 0)
                    + quantity
                )

            selected_demand_numbers.append(demand_number)

            order_shipped_quantity += demand_stats[
                "included_quantity"
            ]

            order_excluded_positions += demand_stats[
                "excluded_positions"
            ]

            demand_diagnostic_rows.append(
                {
                    "_marketplace": marketplace,
                    "_order_id": order_id,
                    "Маркетплейс": marketplace,
                    "Контрагент": demand_counterparty_name,
                    "Заказ": order_number,
                    "Статус заказа": order_status,
                    "Отгрузка": demand_number,
                    "Статус отгрузки": demand_status,
                    "Статус совпадает": (
                        "Да" if statuses_match else "Нет"
                    ),
                    "Позиций готовой продукции": (
                        demand_stats["included_positions"]
                    ),
                    "Исключено позиций": (
                        demand_stats["excluded_positions"]
                    ),
                    "Количество готовой продукции": (
                        demand_stats["included_quantity"]
                    ),
                }
            )

        selected_demands_text = ", ".join(
            sorted(
                selected_demand_numbers,
                key=lambda value: (
                    value.casefold(),
                    value,
                ),
            )
        )

        article_keys = sorted(
            set(ordered_by_article)
            | set(shipped_by_article),
            key=lambda value: (
                value.casefold(),
                value,
            ),
        )

        for article in article_keys:
            ordered_quantity = ordered_by_article.get(article, 0)
            shipped_quantity = shipped_by_article.get(article, 0)

            deviation = shipped_quantity - ordered_quantity

            if deviation < 0:
                result_text = "Недобор"
            elif deviation > 0:
                result_text = "Перебор"
            else:
                result_text = "Совпадает"

            detail_row = {
                "_marketplace": marketplace,
                "_order_id": order_id,
                "Маркетплейс": marketplace,
                "Заказ": order_number,
                "Статус заказа": order_status,
                "Выбранные отгрузки": selected_demands_text,
                "Артикул": article,
                "Заказано": ordered_quantity,
                "В выбранных отгрузках": shipped_quantity,
                "Отклонение": deviation,
                "Результат": result_text,
            }

            detail_rows.append(detail_row)

            if deviation != 0:
                problem_rows.append(detail_row.copy())

            aggregate_key = (
                marketplace,
                article,
            )

            aggregate_row = aggregate_by_key.setdefault(
                aggregate_key,
                {
                    "Маркетплейс": marketplace,
                    "Артикул": article,
                    "В заказах": 0,
                    "В выбранных отгрузках": 0,
                    "Заказов": set(),
                    "Отгрузок": set(),
                },
            )

            aggregate_row["В заказах"] += ordered_quantity
            aggregate_row[
                "В выбранных отгрузках"
            ] += shipped_quantity

            aggregate_row["Заказов"].add(order_number)

            aggregate_row["Отгрузок"].update(
                selected_demand_numbers
            )

        order_summary_rows.append(
            {
                "_marketplace": marketplace,
                "_order_id": order_id,
                "Маркетплейс": marketplace,
                "Контрагент": catalog_row["counterparty_name"],
                "Заказ": order_number,
                "Статус заказа": order_status,
                "Количество готовой продукции в заказе": (
                    order_stats["included_quantity"]
                ),
                "Выбранных отгрузок": len(selected_ids),
                "Выбранные отгрузки": selected_demands_text,
                "Количество готовой продукции в отгрузках": (
                    order_shipped_quantity
                ),
                "Есть отгрузки вне периода": (
                    "Да" if additional_demand_ids else "Нет"
                ),
                "Исключено позиций": order_excluded_positions,
            }
        )

        total_finished_order_quantity += order_stats[
            "included_quantity"
        ]

        total_finished_demand_quantity += (
            order_shipped_quantity
        )

        total_excluded_positions += order_excluded_positions

    aggregate_rows: list[dict] = []

    for aggregate_row in aggregate_by_key.values():
        deviation = (
            aggregate_row["В выбранных отгрузках"]
            - aggregate_row["В заказах"]
        )

        if deviation < 0:
            result_text = "Недобор"
        elif deviation > 0:
            result_text = "Перебор"
        else:
            result_text = "Совпадает"

        aggregate_rows.append(
            {
                "Маркетплейс": aggregate_row["Маркетплейс"],
                "Артикул": aggregate_row["Артикул"],
                "В заказах": aggregate_row["В заказах"],
                "В выбранных отгрузках": (
                    aggregate_row[
                        "В выбранных отгрузках"
                    ]
                ),
                "Отклонение": deviation,
                "Результат": result_text,
                "Заказов": len(
                    aggregate_row["Заказов"]
                ),
                "Отгрузок": len(
                    aggregate_row["Отгрузок"]
                ),
            }
        )

    order_summary_rows.sort(
        key=lambda row: (
            MARKETPLACE_SORT_ORDER.get(
                row["_marketplace"],
                99,
            ),
            row["Контрагент"].casefold(),
            row["Контрагент"],
            row["Заказ"].casefold(),
            row["Заказ"],
        )
    )

    demand_diagnostic_rows.sort(
        key=lambda row: (
            MARKETPLACE_SORT_ORDER.get(
                row["_marketplace"],
                99,
            ),
            row["Контрагент"].casefold(),
            row["Контрагент"],
            row["Заказ"].casefold(),
            row["Заказ"],
            row["Отгрузка"].casefold(),
            row["Отгрузка"],
        )
    )

    detail_rows.sort(
        key=lambda row: (
            MARKETPLACE_SORT_ORDER.get(
                row["_marketplace"],
                99,
            ),
            row["Заказ"].casefold(),
            row["Заказ"],
            0 if row["Отклонение"] < 0 else 1,
            -abs(row["Отклонение"]),
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    problem_rows.sort(
        key=lambda row: (
            MARKETPLACE_SORT_ORDER.get(
                row["_marketplace"],
                99,
            ),
            row["Заказ"].casefold(),
            row["Заказ"],
            0 if row["Отклонение"] < 0 else 1,
            -abs(row["Отклонение"]),
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    aggregate_rows.sort(
        key=lambda row: (
            MARKETPLACE_SORT_ORDER.get(
                row["Маркетплейс"],
                99,
            ),
            0 if row["Отклонение"] < 0 else 1,
            -abs(row["Отклонение"]),
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    total_shortage = sum(
        abs(row["Отклонение"])
        for row in problem_rows
        if row["Отклонение"] < 0
    )

    total_overage = sum(
        row["Отклонение"]
        for row in problem_rows
        if row["Отклонение"] > 0
    )

    result = {
        "date_from": catalog["date_from"],
        "date_to": catalog["date_to"],
        "generated_at": datetime.now(),
        "finished_products_folder_name": scope.root_folder_name,
        "finished_products_folder_count": scope.folder_count,
        "selected_demand_count": len(
            normalized_selected_ids
        ),
        "selected_order_count": len(
            selected_ids_by_order_id
        ),
        "total_finished_order_quantity": (
            total_finished_order_quantity
        ),
        "total_finished_demand_quantity": (
            total_finished_demand_quantity
        ),
        "total_excluded_positions": (
            total_excluded_positions
        ),
        "total_status_mismatches": (
            total_status_mismatches
        ),
        "total_additional_demands": (
            total_additional_demands
        ),
        "total_shortage": total_shortage,
        "total_overage": total_overage,
        "order_summary_rows": order_summary_rows,
        "demand_diagnostic_rows": (
            demand_diagnostic_rows
        ),
        "detail_rows": detail_rows,
        "problem_rows": problem_rows,
        "aggregate_rows": aggregate_rows,
        "problem_aggregate_rows": [
            row
            for row in aggregate_rows
            if row["Отклонение"] != 0
        ],
        "matching_aggregate_rows": [
            row
            for row in aggregate_rows
            if row["Отклонение"] == 0
        ],
    }

    result["wb_manager_text"] = _manager_text(
        marketplace=MARKETPLACE_WB,
        result=result,
    )

    result["ozon_manager_text"] = _manager_text(
        marketplace=MARKETPLACE_OZON,
        result=result,
    )

    return result


def _render_order_summary_table(
    rows: list[dict],
) -> None:
    """Показывает связанные заказы."""
    columns = [
        "Маркетплейс",
        "Контрагент",
        "Заказ",
        "Статус заказа",
        "Количество готовой продукции в заказе",
        "Выбранных отгрузок",
        "Выбранные отгрузки",
        "Количество готовой продукции в отгрузках",
        "Есть отгрузки вне периода",
        "Исключено позиций",
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_demand_table(
    rows: list[dict],
) -> None:
    """Показывает выбранные отгрузки."""
    columns = [
        "Маркетплейс",
        "Контрагент",
        "Заказ",
        "Статус заказа",
        "Отгрузка",
        "Статус отгрузки",
        "Статус совпадает",
        "Позиций готовой продукции",
        "Исключено позиций",
        "Количество готовой продукции",
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_detail_table(
    rows: list[dict],
) -> None:
    """Показывает построчную сверку артикулов."""
    columns = [
        "Маркетплейс",
        "Заказ",
        "Статус заказа",
        "Выбранные отгрузки",
        "Артикул",
        "Заказано",
        "В выбранных отгрузках",
        "Отклонение",
        "Результат",
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_aggregate_table(
    rows: list[dict],
) -> None:
    """Показывает суммарную сверку по артикулам."""
    columns = [
        "Маркетплейс",
        "Артикул",
        "В заказах",
        "В выбранных отгрузках",
        "Отклонение",
        "Результат",
        "Заказов",
        "Отгрузок",
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_result(
    result: dict[str, Any],
) -> None:
    """Отображает итог диагностики."""
    st.divider()
    st.subheader("🧪 Сводка по выбранным отгрузкам")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric(
        "Выбрано отгрузок",
        result["selected_demand_count"],
    )

    metric_2.metric(
        "Связанных заказов",
        result["selected_order_count"],
    )

    metric_3.metric(
        "Готовой продукции в заказах",
        result["total_finished_order_quantity"],
    )

    metric_4.metric(
        "Готовой продукции в отгрузках",
        result["total_finished_demand_quantity"],
    )

    metric_5, metric_6, metric_7, metric_8 = st.columns(4)

    metric_5.metric(
        "Общий недобор",
        result["total_shortage"],
    )

    metric_6.metric(
        "Общий перебор",
        result["total_overage"],
    )

    metric_7.metric(
        "Исключено позиций",
        result["total_excluded_positions"],
    )

    metric_8.metric(
        "Связанных отгрузок вне периода",
        result["total_additional_demands"],
    )

    st.caption(
        "Период применяется к `demand.moment`: "
        f"**{_format_date_range(result['date_from'], result['date_to'])}**."
    )

    st.success(
        "В расчёт включена только готовая продукция из дерева "
        f"«{result['finished_products_folder_name']}». "
        "Расходники и позиции вне этого раздела исключены."
    )

    if result["total_additional_demands"] > 0:
        st.warning(
            "У некоторых связанных заказов есть дополнительные "
            "отгрузки вне выбранного периода. Они намеренно не "
            "участвуют в текущем расчёте."
        )

    if result["total_status_mismatches"] > 0:
        st.info(
            "Найдены несовпадения статусов заказа и отгрузки: "
            f"**{result['total_status_mismatches']}**."
        )

    st.divider()
    st.subheader("📦 Связанные заказы")

    _render_order_summary_table(
        result["order_summary_rows"]
    )

    st.divider()
    st.subheader("🚚 Выбранные отгрузки")

    _render_demand_table(
        result["demand_diagnostic_rows"]
    )

    st.divider()
    st.subheader("📋 Данные для менеджеров")

    st.caption(
        "В каждом поле указаны отдельные заказы, выбранные отгрузки "
        "и итоговая сводка недобора / перебора по артикулам."
    )

    wb_col, ozon_col = st.columns(2)

    with wb_col:
        st.text_area(
            "WB — текст для копирования",
            value=result["wb_manager_text"],
            height=560,
            key="mysklad_reconciliation_wb_manager_text",
        )

    with ozon_col:
        st.text_area(
            "Ozon — текст для копирования",
            value=result["ozon_manager_text"],
            height=560,
            key="mysklad_reconciliation_ozon_manager_text",
        )

    st.divider()
    st.subheader("⚠️ Отклонения по заказам")

    if result["problem_rows"]:
        _render_detail_table(result["problem_rows"])
    else:
        st.success(
            "По выбранным отгрузкам количество готовой продукции "
            "совпадает с количеством в связанных заказах."
        )

    st.divider()
    st.subheader("📌 Итог по артикулам")

    if result["problem_aggregate_rows"]:
        _render_aggregate_table(
            result["problem_aggregate_rows"]
        )
    else:
        st.info(
            "По выбранным отгрузкам нет недобора или перебора."
        )

    if result["matching_aggregate_rows"]:
        with st.expander(
            "Совпавшие артикулы",
            expanded=False,
        ):
            _render_aggregate_table(
                result["matching_aggregate_rows"]
            )

    with st.expander(
        "Все строки сравнения",
        expanded=False,
    ):
        _render_detail_table(
            result["detail_rows"]
        )


def _get_mysklad_token() -> str:
    """Безопасно читает токен МоегоСклада."""
    try:
        return str(
            st.secrets["MYSKLAD_API_TOKEN"]
        ).strip()
    except Exception:
        return ""


def render_mysklad_order_shipment_reconciliation_tab() -> None:
    """
    Отображает read-only диагностику FBO-заказов и отгрузок.

    Все обращения к МойСклад выполняются только GET-запросами.
    """
    st.session_state.setdefault(
        "mysklad_reconciliation_catalog",
        None,
    )

    st.session_state.setdefault(
        "mysklad_reconciliation_result",
        None,
    )

    st.session_state.setdefault(
        "mysklad_reconciliation_order_widget_version",
        0,
    )

    st.title("🧪 Диагностика заказов и отгрузок FBO")

    st.markdown(
        """
        <div class="description-box">
            Выберите период по дате фактической отгрузки. В список
            попадут только отгрузки WB и Ozon, связанные с заказами
            покупателей. Расходники автоматически исключаются:
            сверяется только готовая продукция.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "FBS-отгрузки без связанного заказа не входят в выбор. "
        "WB / Ozon определяется по контрагенту самой отгрузки."
    )

    mysklad_token = _get_mysklad_token()

    if not mysklad_token:
        st.error(
            "Для диагностики не настроен `MYSKLAD_API_TOKEN`."
        )
        return

    st.subheader("Период и список FBO-отгрузок")

    today = datetime.now().date()

    date_col, load_col = st.columns([2, 1])

    with date_col:
        selected_date_range = st.date_input(
            "Период по дате отгрузки",
            value=(today, today),
            format="DD.MM.YYYY",
            key="reconciliation_date_range",
            help=(
                "Используется `demand.moment`. Заказ может быть создан "
                "раньше даты фактической отгрузки."
            ),
        )

    with load_col:
        st.markdown("<br>", unsafe_allow_html=True)

        load_clicked = st.button(
            "⟳ Загрузить FBO-отгрузки",
            use_container_width=True,
            key="load_reconciliation_catalog",
        )

    date_from, date_to = _parse_date_range(
        selected_date_range
    )

    if date_from is None or date_to is None:
        st.warning(
            "Выберите одну дату или полный диапазон дат."
        )
        return

    catalog_signature = (
        date_from.isoformat(),
        date_to.isoformat(),
    )

    saved_catalog = st.session_state.get(
        "mysklad_reconciliation_catalog"
    )

    if (
        saved_catalog is not None
        and saved_catalog.get("signature") != catalog_signature
    ):
        st.session_state[
            "mysklad_reconciliation_catalog"
        ] = None

        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

        st.session_state[
            "mysklad_reconciliation_order_widget_version"
        ] += 1

        saved_catalog = None

    if load_clicked:
        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Получаю отгрузки периода, связанных заказов "
                "и контрагентов отгрузок…"
            ):
                catalog = build_reconciliation_catalog(
                    client=client,
                    date_from=date_from,
                    date_to=date_to,
                )

            st.session_state[
                "mysklad_reconciliation_catalog"
            ] = {
                "signature": catalog_signature,
                "catalog": catalog,
            }

            st.session_state[
                "mysklad_reconciliation_result"
            ] = None

            st.session_state[
                "mysklad_reconciliation_order_widget_version"
            ] += 1

            st.success(
                "Список FBO-отгрузок успешно загружен."
            )

        except (MySkladApiError, ValueError) as error:
            st.error(str(error))

    saved_catalog = st.session_state.get(
        "mysklad_reconciliation_catalog"
    )

    if (
        saved_catalog is None
        or saved_catalog.get("signature") != catalog_signature
    ):
        st.info(
            "Выберите период и нажмите "
            "«Загрузить FBO-отгрузки»."
        )
        return

    catalog = saved_catalog["catalog"]
    demand_rows = catalog["demand_rows"]

    st.caption(
        "Всего отгрузок в периоде: "
        f"**{catalog['total_demands_in_period']}**. "
        "Без связанного заказа: "
        f"**{catalog['without_customer_order_count']}**. "
        "Не распознано как WB / Ozon: "
        f"**{catalog['non_marketplace_count']}**. "
        "Доступно для выбора: "
        f"**{len(demand_rows)}**."
    )

    if not demand_rows:
        st.warning(
            "За выбранный период не найдено отгрузок WB или Ozon, "
            "связанных с заказами покупателей."
        )

        st.info(
            "Проверьте название контрагента в самой отгрузке. "
            "Оно должно содержать, например, `WB`, `Wildberries`, "
            "`ВБ`, `Ozon` или `Озон`."
        )
        return

    catalog_by_demand_id = {
        row["demand_id"]: row
        for row in demand_rows
    }

    demand_ids = [
        row["demand_id"]
        for row in demand_rows
    ]

    widget_key = (
        "reconciliation_selected_demands_"
        f"{st.session_state['mysklad_reconciliation_order_widget_version']}"
    )

    st.divider()
    st.subheader("Выбор FBO-отгрузок")

    selected_demand_ids = st.multiselect(
        "Выберите отгрузки для диагностики",
        options=demand_ids,
        format_func=lambda demand_id: _catalog_label(
            demand_id=demand_id,
            catalog_by_demand_id=catalog_by_demand_id,
        ),
        key=widget_key,
        placeholder=(
            "Выберите одну или несколько FBO-отгрузок"
        ),
        help=(
            "Список отсортирован по WB / Ozon, контрагенту, "
            "заказу и отгрузке."
        ),
    )

    clear_col, _ = st.columns([1, 3])

    with clear_col:
        clear_clicked = st.button(
            "🧹 Очистить выбор",
            use_container_width=True,
            key="clear_reconciliation_demands",
        )

    if clear_clicked:
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

        st.session_state[
            "mysklad_reconciliation_order_widget_version"
        ] += 1

        st.rerun()

    unique_selected_ids = list(
        dict.fromkeys(selected_demand_ids)
    )

    if unique_selected_ids:
        wb_count = sum(
            1
            for demand_id in unique_selected_ids
            if catalog_by_demand_id[demand_id][
                "marketplace"
            ]
            == MARKETPLACE_WB
        )

        ozon_count = sum(
            1
            for demand_id in unique_selected_ids
            if catalog_by_demand_id[demand_id][
                "marketplace"
            ]
            == MARKETPLACE_OZON
        )

        metric_1, metric_2, metric_3 = st.columns(3)

        metric_1.metric(
            "Выбрано отгрузок",
            len(unique_selected_ids),
        )

        metric_2.metric("WB", wb_count)
        metric_3.metric("Ozon", ozon_count)

    result_signature = (
        catalog_signature,
        tuple(sorted(unique_selected_ids)),
    )

    saved_result = st.session_state.get(
        "mysklad_reconciliation_result"
    )

    if (
        saved_result is not None
        and saved_result.get("signature") != result_signature
    ):
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

    st.markdown("<br>", unsafe_allow_html=True)

    calculate_clicked = st.button(
        "🧪 Сверить заказы и отгрузки",
        type="primary",
        use_container_width=True,
        key="run_order_shipment_reconciliation",
        disabled=not unique_selected_ids,
    )

    if calculate_clicked:
        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Получаю готовую продукцию выбранных отгрузок "
                "и связанных заказов…"
            ):
                result = build_order_shipment_reconciliation(
                    client=client,
                    catalog=catalog,
                    selected_demand_ids=set(
                        unique_selected_ids
                    ),
                )

            st.session_state[
                "mysklad_reconciliation_result"
            ] = {
                "signature": result_signature,
                "result": result,
            }

            st.success(
                "Сверка заказов и отгрузок завершена."
            )

        except (MySkladApiError, ValueError) as error:
            st.error(str(error))

    saved_result = st.session_state.get(
        "mysklad_reconciliation_result"
    )

    if (
        saved_result is None
        or saved_result.get("signature") != result_signature
    ):
        st.info(
            "Выберите FBO-отгрузки и нажмите "
            "«Сверить заказы и отгрузки»."
        )
        return

    _render_result(saved_result["result"])