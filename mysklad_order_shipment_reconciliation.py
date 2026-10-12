from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import streamlit as st

from mysklad_api import MySkladApiError, MySkladClient


def _value_to_date(value: object) -> date | None:
    """Преобразует значение Streamlit в объект date."""
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return None


def _parse_date_range(
    selected_value: object,
) -> tuple[date | None, date | None]:
    """Разбирает одиночную дату или диапазон из st.date_input."""
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
    """Форматирует выбранный период."""
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

    text = value.strip()

    for pattern in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue

    return None


def _format_moment(value: object) -> str:
    """Форматирует дату документа для безопасного отображения."""
    parsed = _parse_moment(value)

    if parsed is None:
        return str(value or "Дата не определена").strip()

    return parsed.strftime("%d.%m.%Y %H:%M")


def _state_id_from_object(value: object) -> str:
    """Извлекает ID статуса из объекта или meta.href."""
    if not isinstance(value, dict):
        return ""

    direct_id = str(value.get("id") or "").strip()

    if direct_id:
        return direct_id

    meta = value.get("meta")

    if not isinstance(meta, dict):
        return ""

    href = str(meta.get("href") or "").strip().rstrip("/")

    if not href:
        return ""

    return href.rsplit("/", maxsplit=1)[-1]


def _entity_id(value: object) -> str:
    """Извлекает ID сущности из поля id или meta.href."""
    if not isinstance(value, dict):
        return ""

    direct_id = str(value.get("id") or "").strip()

    if direct_id:
        return direct_id

    meta = value.get("meta")

    if not isinstance(meta, dict):
        return ""

    href = str(meta.get("href") or "").strip().rstrip("/")

    if not href:
        return ""

    return href.rsplit("/", maxsplit=1)[-1]


def _normal_text(value: object) -> str:
    """Нормализует текст для безопасного сравнения."""
    return " ".join(
        str(value or "").strip().split()
    ).casefold()


def _build_state_name_map(
    states: list[dict],
    document_label: str,
) -> dict[str, str]:
    """Строит соответствие ID статуса и названия статуса."""
    result: dict[str, str] = {}

    for state in states:
        if not isinstance(state, dict):
            continue

        state_id = _state_id_from_object(state)
        state_name = str(state.get("name") or "").strip()

        if not state_id or not state_name:
            raise MySkladApiError(
                "МойСклад вернул некорректный статус "
                f"для документа «{document_label}»."
            )

        previous_name = result.get(state_id)

        if (
            previous_name is not None
            and previous_name != state_name
        ):
            raise MySkladApiError(
                "МойСклад вернул один ID статуса "
                f"«{document_label}» с разными названиями."
            )

        result[state_id] = state_name

    if not result:
        raise MySkladApiError(
            "МойСклад не вернул доступных статусов "
            f"для документа «{document_label}»."
        )

    return result


def _status_name(
    document: dict,
    state_names: dict[str, str],
) -> str:
    """
    Возвращает название статуса документа.

    Сначала используется название, которое пришло в самом документе.
    Если его нет, используется ID статуса и справочник metadata.
    """
    state = document.get("state")

    if not isinstance(state, dict):
        return ""

    embedded_name = str(state.get("name") or "").strip()

    if embedded_name:
        return embedded_name

    state_id = _state_id_from_object(state)

    return state_names.get(state_id, "")


def _order_number(order: dict) -> str:
    """Возвращает номер заказа покупателя."""
    number = str(order.get("name") or "").strip()

    return number or "Без номера"


def _demand_number(demand: dict) -> str:
    """Возвращает номер отгрузки."""
    number = str(demand.get("name") or "").strip()

    return number or "Без номера"


def _linked_demand_ids(order: dict) -> list[str]:
    """
    Возвращает уникальные ID отгрузок из связанных документов заказа.

    Единственный источник связи — поле `customerorder.demands`.
    Отгрузка, отсутствующая в этом поле, в расчёт не попадает.
    """
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

    for demand_reference in raw_demands:
        demand_id = _entity_id(demand_reference)

        if not demand_id:
            raise MySkladApiError(
                "МойСклад вернул связанную отгрузку без корректного ID "
                "у заказа покупателя."
            )

        if demand_id in seen_ids:
            continue

        seen_ids.add(demand_id)
        result.append(demand_id)

    return result


def _article_and_quantity(
    position: dict,
    document_number: str,
    document_label: str,
) -> tuple[str, int]:
    """
    Возвращает article и целое количество товарной позиции.

    В сверке поддерживаются только позиции номенклатуры типа `product`
    с неотрицательным целым количеством.
    """
    assortment = position.get("assortment")

    if not isinstance(assortment, dict):
        raise MySkladApiError(
            "МойСклад вернул позицию без номенклатуры "
            f"в документе «{document_label}» `{document_number}`."
        )

    meta = assortment.get("meta")

    if not isinstance(meta, dict):
        raise MySkladApiError(
            "МойСклад вернул номенклатуру без `meta` "
            f"в документе «{document_label}» `{document_number}`."
        )

    assortment_type = str(meta.get("type") or "").strip()

    if assortment_type != "product":
        raise MySkladApiError(
            "В документе "
            f"«{document_label}» `{document_number}` "
            "найден тип номенклатуры, отличный от `product`: "
            f"`{assortment_type or 'не определён'}`. "
            "Сверка остановлена, чтобы не смешать товары, услуги, "
            "комплекты или модификации."
        )

    article = str(assortment.get("article") or "").strip()

    if not article:
        raise MySkladApiError(
            "У товара в документе "
            f"«{document_label}» `{document_number}` "
            "не заполнен артикул. Сверка остановлена, чтобы не "
            "смешать позиции разных товаров."
        )

    quantity_value = position.get("quantity")

    if isinstance(quantity_value, bool):
        raise MySkladApiError(
            "МойСклад вернул некорректное количество "
            f"в документе «{document_label}» `{document_number}`."
        )

    try:
        quantity = Decimal(str(quantity_value))
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
            "Сверка поддерживает только целое количество товаров."
        )

    return article, int(quantity)


def _positions_by_article(
    positions: list[dict],
    document_number: str,
    document_label: str,
) -> dict[str, int]:
    """Суммирует позиции документа по точному артикулу."""
    result: dict[str, int] = {}

    for position in positions:
        if not isinstance(position, dict):
            raise MySkladApiError(
                "МойСклад вернул некорректную товарную позицию "
                f"в документе «{document_label}» `{document_number}`."
            )

        article, quantity = _article_and_quantity(
            position=position,
            document_number=document_number,
            document_label=document_label,
        )

        result[article] = result.get(article, 0) + quantity

    return result


def _order_label(
    order_id: str,
    order_by_id: dict[str, dict],
) -> str:
    """Формирует короткую подпись заказа в списке выбора."""
    order = order_by_id[order_id]

    return (
        f"{order['number']}  |  "
        f"{order['moment_text']}"
    )


def _copyable_telegram_text(
    date_from: date,
    date_to: date,
    problem_rows: list[dict],
    excluded_rows: list[dict],
) -> str:
    """
    Формирует текст для ручной отправки в Telegram.

    Номер каждого рассматриваемого заказа выводится обязательно.
    """
    problems_by_order: dict[str, list[dict]] = defaultdict(list)

    for row in problem_rows:
        problems_by_order[row["Заказ"]].append(row)

    excluded_by_order: dict[str, list[dict]] = defaultdict(list)

    for row in excluded_rows:
        excluded_by_order[row["Заказ"]].append(row)

    order_numbers = sorted(
        set(problems_by_order) | set(excluded_by_order),
        key=lambda value: (value.casefold(), value),
    )

    if not order_numbers:
        return (
            "Сверка заказов и связанных отгрузок\n"
            f"Период заказов: {_format_date_range(date_from, date_to)}\n\n"
            "Отклонений и проблемных связей не найдено."
        )

    lines = [
        "Сверка заказов и связанных отгрузок",
        (
            "Период заказов: "
            f"{_format_date_range(date_from, date_to)}"
        ),
    ]

    for order_number in order_numbers:
        lines.append("")
        lines.append(f"Заказ {order_number}:")

        order_problem_rows = sorted(
            problems_by_order.get(order_number, []),
            key=lambda row: (
                0 if row["Результат"] == "Недобор" else 1,
                -abs(row["Отклонение"]),
                row["Артикул"].casefold(),
                row["Артикул"],
            ),
        )

        if order_problem_rows:
            for row in order_problem_rows:
                if row["Отклонение"] < 0:
                    lines.append(
                        "• "
                        f"{row['Артикул']} — "
                        f"заказано {row['Заказано']}, "
                        f"отгружено {row['Отгружено']}, "
                        f"недобор {abs(row['Отклонение'])}."
                    )
                else:
                    lines.append(
                        "• "
                        f"{row['Артикул']} — "
                        f"заказано {row['Заказано']}, "
                        f"отгружено {row['Отгружено']}, "
                        f"перебор {row['Отклонение']}."
                    )

        order_excluded_rows = excluded_by_order.get(
            order_number,
            [],
        )

        if order_excluded_rows:
            reasons = sorted(
                {
                    row["Причина"]
                    for row in order_excluded_rows
                }
            )

            lines.append(
                "• Требует проверки: "
                + "; ".join(reasons)
                + "."
            )

    return "\n".join(lines)


def build_reconciliation_catalog(
    client: MySkladClient,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    """
    Загружает список заказов покупателей по `moment`.

    Контрагент не является обязательным: заказ можно выбрать вручную
    только по номеру и дате документа.
    """
    orders = client.customer_orders_by_moment(
        date_from=date_from,
        date_to=date_to,
    )

    result_rows: list[dict] = []
    seen_order_ids: set[str] = set()

    for order in orders:
        if not isinstance(order, dict):
            continue

        order_id = str(order.get("id") or "").strip()

        if not order_id:
            raise MySkladApiError(
                "МойСклад вернул заказ покупателя без корректного ID."
            )

        if order_id in seen_order_ids:
            raise MySkladApiError(
                "МойСклад вернул один и тот же заказ покупателя "
                "повторно. Список не сформирован, чтобы исключить "
                "двойной учёт."
            )

        seen_order_ids.add(order_id)

        result_rows.append(
            {
                "id": order_id,
                "number": _order_number(order),
                "moment_text": _format_moment(
                    order.get("moment")
                ),
            }
        )

    result_rows.sort(
        key=lambda row: (
            row["moment_text"],
            row["number"].casefold(),
            row["number"],
            row["id"],
        ),
        reverse=True,
    )

    return {
        "date_from": date_from,
        "date_to": date_to,
        "order_rows": result_rows,
    }


def build_order_shipment_reconciliation(
    client: MySkladClient,
    catalog: dict[str, Any],
    selected_order_ids: set[str],
) -> dict[str, Any]:
    """
    Сверяет выбранные заказы покупателей и связанные отгрузки.

    Условия включения отгрузки в расчёт:

    - она указана в `customerorder.demands`;
    - отгрузка обратной ссылкой `customerOrder` указывает на тот же заказ;
    - статус отгрузки совпадает со статусом заказа;
    - одна отгрузка не учитывается повторно для другого заказа.

    Если заказ имеет несколько связанных корректных отгрузок, количество
    их товарных позиций суммируется по артикулу.
    """
    order_rows = catalog.get("order_rows")

    if not isinstance(order_rows, list):
        raise ValueError(
            "Список заказов устарел. Загрузите его заново."
        )

    normalized_selected_ids = {
        str(order_id).strip()
        for order_id in selected_order_ids
        if str(order_id).strip()
    }

    if not normalized_selected_ids:
        raise ValueError(
            "Выберите хотя бы один заказ покупателя."
        )

    catalog_by_id = {
        str(row.get("id") or "").strip(): row
        for row in order_rows
        if isinstance(row, dict)
    }

    unknown_order_ids = (
        normalized_selected_ids - set(catalog_by_id)
    )

    if unknown_order_ids:
        raise ValueError(
            "Один или несколько выбранных заказов отсутствуют "
            "в текущем списке периода. Загрузите список заново."
        )

    customer_order_state_names = _build_state_name_map(
        states=client.customer_order_states(),
        document_label="Заказ покупателя",
    )

    demand_state_names = _build_state_name_map(
        states=client.demand_states(),
        document_label="Отгрузка",
    )

    selected_catalog_rows = sorted(
        (
            catalog_by_id[order_id]
            for order_id in normalized_selected_ids
        ),
        key=lambda row: (
            row["number"].casefold(),
            row["number"],
            row["id"],
        ),
    )

    aggregate_by_article: dict[str, dict[str, Any]] = {}
    detail_rows: list[dict] = []
    problem_rows: list[dict] = []
    excluded_rows: list[dict] = []

    checked_demand_ids: set[str] = set()

    compared_order_count = 0
    orders_without_demands_count = 0
    linked_demands_count = 0
    included_demands_count = 0
    excluded_demands_count = 0

    for catalog_row in selected_catalog_rows:
        order_id = catalog_row["id"]

        order = client.customer_order_by_id(
            customer_order_id=order_id
        )

        actual_order_id = str(order.get("id") or "").strip()

        if actual_order_id != order_id:
            raise MySkladApiError(
                "МойСклад вернул заказ с ID, не совпадающим "
                "с запрошенным. Сверка остановлена."
            )

        order_number = _order_number(order)

        order_status = _status_name(
            document=order,
            state_names=customer_order_state_names,
        )

        if not order_status:
            excluded_rows.append(
                {
                    "Заказ": order_number,
                    "Статус заказа": "Без статуса",
                    "Связанная отгрузка": "—",
                    "Причина": (
                        "У заказа покупателя не указан статус. "
                        "Невозможно проверить совпадение статусов."
                    ),
                }
            )
            continue

        order_positions = client.customer_order_positions(
            customer_order_id=order_id,
            expand_assortment=True,
        )

        ordered_by_article = _positions_by_article(
            positions=order_positions,
            document_number=order_number,
            document_label="Заказ покупателя",
        )

        linked_demand_ids = _linked_demand_ids(order)

        if not linked_demand_ids:
            orders_without_demands_count += 1

        shipped_by_article: dict[str, int] = {}
        included_demand_numbers: list[str] = []

        for demand_id in linked_demand_ids:
            linked_demands_count += 1

            demand = client.demand_by_id(demand_id)
            demand_number = _demand_number(demand)

            actual_demand_id = str(demand.get("id") or "").strip()

            if actual_demand_id != demand_id:
                raise MySkladApiError(
                    "МойСклад вернул отгрузку с ID, не совпадающим "
                    "с запрошенным. Сверка остановлена."
                )

            reverse_order_id = _entity_id(
                demand.get("customerOrder")
            )

            demand_status = _status_name(
                document=demand,
                state_names=demand_state_names,
            )

            reason = ""

            if demand_id in checked_demand_ids:
                reason = (
                    "Эта отгрузка уже была учтена у другого "
                    "выбранного заказа. Повторный учёт заблокирован."
                )

            elif not reverse_order_id:
                reason = (
                    "В отгрузке отсутствует обратная связь "
                    "с заказом покупателя."
                )

            elif reverse_order_id != order_id:
                reason = (
                    "Отгрузка обратной связью указывает "
                    "на другой заказ покупателя."
                )

            elif not demand_status:
                reason = (
                    "У связанной отгрузки не указан статус. "
                    "Невозможно проверить совпадение статусов."
                )

            elif (
                _normal_text(demand_status)
                != _normal_text(order_status)
            ):
                reason = (
                    "Статус отгрузки не совпадает "
                    "со статусом заказа."
                )

            if reason:
                excluded_demands_count += 1

                excluded_rows.append(
                    {
                        "Заказ": order_number,
                        "Статус заказа": order_status,
                        "Связанная отгрузка": demand_number,
                        "Причина": reason,
                    }
                )
                continue

            checked_demand_ids.add(demand_id)

            demand_positions = client.demand_positions(
                demand_id=demand_id,
                expand_assortment=True,
            )

            demand_by_article = _positions_by_article(
                positions=demand_positions,
                document_number=demand_number,
                document_label="Отгрузка",
            )

            for article, quantity in demand_by_article.items():
                shipped_by_article[article] = (
                    shipped_by_article.get(article, 0)
                    + quantity
                )

            included_demands_count += 1
            included_demand_numbers.append(demand_number)

        compared_order_count += 1

        article_keys = sorted(
            set(ordered_by_article) | set(shipped_by_article),
            key=lambda value: (value.casefold(), value),
        )

        for article in article_keys:
            ordered_quantity = ordered_by_article.get(article, 0)
            shipped_quantity = shipped_by_article.get(article, 0)
            deviation = shipped_quantity - ordered_quantity

            if deviation < 0:
                result = "Недобор"
            elif deviation > 0:
                result = "Перебор"
            else:
                result = "Совпадает"

            detail_row = {
                "Заказ": order_number,
                "Статус": order_status,
                "Связанные отгрузки": (
                    ", ".join(included_demand_numbers)
                    if included_demand_numbers
                    else "Нет корректных отгрузок"
                ),
                "Артикул": article,
                "Заказано": ordered_quantity,
                "Отгружено": shipped_quantity,
                "Отклонение": deviation,
                "Результат": result,
            }

            detail_rows.append(detail_row)

            if deviation != 0:
                problem_rows.append(detail_row.copy())

            aggregate_row = aggregate_by_article.setdefault(
                article,
                {
                    "Артикул": article,
                    "В заказах": 0,
                    "В отгрузках": 0,
                    "Заказов": set(),
                    "Отгрузок": set(),
                    "Заказов с отклонением": set(),
                },
            )

            aggregate_row["В заказах"] += ordered_quantity
            aggregate_row["В отгрузках"] += shipped_quantity
            aggregate_row["Заказов"].add(order_number)

            if deviation != 0:
                aggregate_row["Заказов с отклонением"].add(
                    order_number
                )

            for demand_number in included_demand_numbers:
                aggregate_row["Отгрузок"].add(demand_number)

    aggregate_rows: list[dict] = []

    for aggregate_row in aggregate_by_article.values():
        deviation = (
            aggregate_row["В отгрузках"]
            - aggregate_row["В заказах"]
        )

        order_problem_count = len(
            aggregate_row["Заказов с отклонением"]
        )

        if deviation < 0:
            result = "Недобор"
        elif deviation > 0:
            result = "Перебор"
        elif order_problem_count > 0:
            result = (
                "Отклонения по заказам "
                "взаимно компенсировались"
            )
        else:
            result = "Совпадает"

        aggregate_rows.append(
            {
                "Артикул": aggregate_row["Артикул"],
                "В заказах": aggregate_row["В заказах"],
                "В отгрузках": aggregate_row["В отгрузках"],
                "Отклонение": deviation,
                "Результат": result,
                "Заказов": len(aggregate_row["Заказов"]),
                "Отгрузок": len(aggregate_row["Отгрузок"]),
                "Заказов с отклонением": order_problem_count,
            }
        )

    detail_rows.sort(
        key=lambda row: (
            row["Заказ"].casefold(),
            row["Заказ"],
            0 if row["Результат"] == "Недобор" else 1,
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    problem_rows.sort(
        key=lambda row: (
            row["Заказ"].casefold(),
            row["Заказ"],
            0 if row["Результат"] == "Недобор" else 1,
            -abs(row["Отклонение"]),
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    aggregate_rows.sort(
        key=lambda row: (
            0 if row["Результат"] == "Недобор" else 1,
            0 if row["Результат"] == "Перебор" else 1,
            -abs(row["Отклонение"]),
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    excluded_rows.sort(
        key=lambda row: (
            row["Заказ"].casefold(),
            row["Заказ"],
            row["Связанная отгрузка"].casefold(),
            row["Связанная отгрузка"],
            row["Причина"],
        )
    )

    problem_aggregate_rows = [
        row
        for row in aggregate_rows
        if (
            row["Отклонение"] != 0
            or row["Заказов с отклонением"] > 0
        )
    ]

    matching_aggregate_rows = [
        row
        for row in aggregate_rows
        if (
            row["Отклонение"] == 0
            and row["Заказов с отклонением"] == 0
        )
    ]

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

    return {
        "date_from": catalog["date_from"],
        "date_to": catalog["date_to"],
        "generated_at": datetime.now(),
        "selected_order_count": len(normalized_selected_ids),
        "compared_order_count": compared_order_count,
        "orders_without_demands_count": (
            orders_without_demands_count
        ),
        "linked_demands_count": linked_demands_count,
        "included_demands_count": included_demands_count,
        "excluded_demands_count": excluded_demands_count,
        "shortage_line_count": sum(
            row["Отклонение"] < 0
            for row in problem_rows
        ),
        "overage_line_count": sum(
            row["Отклонение"] > 0
            for row in problem_rows
        ),
        "total_shortage": total_shortage,
        "total_overage": total_overage,
        "aggregate_rows": aggregate_rows,
        "problem_aggregate_rows": problem_aggregate_rows,
        "matching_aggregate_rows": matching_aggregate_rows,
        "detail_rows": detail_rows,
        "problem_rows": problem_rows,
        "excluded_rows": excluded_rows,
        "telegram_text": _copyable_telegram_text(
            date_from=catalog["date_from"],
            date_to=catalog["date_to"],
            problem_rows=problem_rows,
            excluded_rows=excluded_rows,
        ),
    }


def _render_aggregate_table(
    rows: list[dict],
) -> None:
    """Показывает итоговую сводку отклонений по артикулам."""
    columns = [
        "Артикул",
        "В заказах",
        "В отгрузках",
        "Отклонение",
        "Результат",
        "Заказов",
        "Отгрузок",
        "Заказов с отклонением",
    ]

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Артикул": st.column_config.TextColumn(
                "Артикул",
                width="large",
            ),
            "В заказах": st.column_config.NumberColumn(
                "В заказах",
                format="%d",
            ),
            "В отгрузках": st.column_config.NumberColumn(
                "В отгрузках",
                format="%d",
            ),
            "Отклонение": st.column_config.NumberColumn(
                "Отклонение",
                format="%+d",
            ),
            "Результат": st.column_config.TextColumn(
                "Результат",
                width="large",
            ),
            "Заказов": st.column_config.NumberColumn(
                "Заказов",
                format="%d",
            ),
            "Отгрузок": st.column_config.NumberColumn(
                "Отгрузок",
                format="%d",
            ),
            "Заказов с отклонением": st.column_config.NumberColumn(
                "Заказов с отклонением",
                format="%d",
            ),
        },
    )


def _render_detail_table(
    rows: list[dict],
) -> None:
    """Показывает детализацию сравнения по заказу и артикулу."""
    columns = [
        "Заказ",
        "Статус",
        "Связанные отгрузки",
        "Артикул",
        "Заказано",
        "Отгружено",
        "Отклонение",
        "Результат",
    ]

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Заказ": st.column_config.TextColumn(
                "Заказ",
                width="medium",
            ),
            "Статус": st.column_config.TextColumn(
                "Статус",
                width="medium",
            ),
            "Связанные отгрузки": st.column_config.TextColumn(
                "Связанные отгрузки",
                width="large",
            ),
            "Артикул": st.column_config.TextColumn(
                "Артикул",
                width="large",
            ),
            "Заказано": st.column_config.NumberColumn(
                "Заказано",
                format="%d",
            ),
            "Отгружено": st.column_config.NumberColumn(
                "Отгружено",
                format="%d",
            ),
            "Отклонение": st.column_config.NumberColumn(
                "Отклонение",
                format="%+d",
            ),
            "Результат": st.column_config.TextColumn(
                "Результат",
                width="small",
            ),
        },
    )


def _render_excluded_table(
    rows: list[dict],
) -> None:
    """Показывает связи, исключённые из расчёта."""
    columns = [
        "Заказ",
        "Статус заказа",
        "Связанная отгрузка",
        "Причина",
    ]

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Заказ": st.column_config.TextColumn(
                "Заказ",
                width="medium",
            ),
            "Статус заказа": st.column_config.TextColumn(
                "Статус заказа",
                width="medium",
            ),
            "Связанная отгрузка": st.column_config.TextColumn(
                "Связанная отгрузка",
                width="medium",
            ),
            "Причина": st.column_config.TextColumn(
                "Причина",
                width="large",
            ),
        },
    )


def _render_result(
    result: dict[str, Any],
) -> None:
    """Отображает результат сверки заказов и связанных отгрузок."""
    st.divider()
    st.subheader("📊 Сводка сверки")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric(
        "Выбрано заказов",
        result["selected_order_count"],
    )

    metric_2.metric(
        "Заказов в сверке",
        result["compared_order_count"],
    )

    metric_3.metric(
        "Связанных отгрузок учтено",
        result["included_demands_count"],
    )

    metric_4.metric(
        "Заказов без отгрузок",
        result["orders_without_demands_count"],
    )

    metric_5, metric_6, metric_7, metric_8 = st.columns(4)

    metric_5.metric(
        "Строк с недобором",
        result["shortage_line_count"],
    )

    metric_6.metric(
        "Всего недобор",
        result["total_shortage"],
    )

    metric_7.metric(
        "Строк с перебором",
        result["overage_line_count"],
    )

    metric_8.metric(
        "Всего перебор",
        result["total_overage"],
    )

    st.caption(
        "Период фильтрует только дату `moment` заказов покупателей: "
        f"**{_format_date_range(result['date_from'], result['date_to'])}**."
    )

    st.caption(
        "Отгрузки учитываются только из связанных документов выбранного "
        "заказа. Если в заказе несколько корректно связанных отгрузок, "
        "их количество по артикулу суммируется."
    )

    st.caption(
        "Отгрузка входит в расчёт только при совпадении её статуса "
        "со статусом заказа и при подтверждённой обратной связи "
        "с тем же заказом."
    )

    if result["problem_rows"]:
        st.error(
            "Найдены недобор или перебор. Ниже подготовлен текст "
            "для ручной отправки маркетологу."
        )
    else:
        st.success(
            "По включённым заказам недобора и перебора не найдено."
        )

    if result["excluded_demands_count"] > 0:
        st.warning(
            "Некоторые связанные отгрузки не вошли в расчёт: "
            "у них не совпал статус либо не подтвердилась обратная "
            "связь с заказом. Причины приведены ниже."
        )

    st.divider()
    st.subheader("📋 Текст для Telegram")

    st.caption(
        "В тексте обязательно указывается номер каждого "
        "рассматриваемого заказа."
    )

    st.code(
        result["telegram_text"],
        language=None,
    )

    st.divider()
    st.subheader("⚠️ Недобор / перебор по заказам")

    if not result["problem_rows"]:
        st.success(
            "Проблемных строк по выбранным заказам нет."
        )
    else:
        _render_detail_table(result["problem_rows"])

    st.divider()
    st.subheader("📌 Итог по артикулам")

    if not result["problem_aggregate_rows"]:
        st.info(
            "Нет артикулов с отклонениями в выбранных заказах."
        )
    else:
        _render_aggregate_table(
            result["problem_aggregate_rows"]
        )

    if result["matching_aggregate_rows"]:
        st.divider()

        with st.expander(
            "Совпавшие артикулы",
            expanded=False,
        ):
            _render_aggregate_table(
                result["matching_aggregate_rows"]
            )

    if result["detail_rows"]:
        st.divider()

        with st.expander(
            "Все строки сравнения",
            expanded=False,
        ):
            _render_detail_table(
                result["detail_rows"]
            )

    if result["excluded_rows"]:
        st.divider()

        with st.expander(
            "Требуют проверки: исключённые связи",
            expanded=False,
        ):
            _render_excluded_table(
                result["excluded_rows"]
            )


def _get_mysklad_token() -> str:
    """Безопасно получает токен МоегоСклада из Streamlit Secrets."""
    try:
        return str(
            st.secrets["MYSKLAD_API_TOKEN"]
        ).strip()
    except Exception:
        return ""


def render_mysklad_order_shipment_reconciliation_tab() -> None:
    """
    Отображает вкладку ручной сверки заказов и связанных отгрузок.

    Контрагент не используется как обязательный фильтр. Пользователь
    самостоятельно выбирает нужные заказы по номеру после загрузки
    списка за период.

    Все запросы к МоемСкладу выполняются только методом GET.
    """
    st.title("⚖️ Сверка заказов и отгрузок FBO")

    st.markdown(
        """
        <div class="description-box">
            Выберите период по дате заказа, загрузите список и вручную
            выберите интересующие заказы по номеру. Приложение сравнит
            количество товаров в заказе с количеством в его связанных
            отгрузках и покажет недобор или перебор.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Контрагент не обязателен для этой версии сверки. "
        "Направление FBO WB / FBO Ozon определяется вручную выбором "
        "нужного заказа из списка."
    )

    mysklad_token = _get_mysklad_token()

    if not mysklad_token:
        st.error(
            "Для сверки не настроен `MYSKLAD_API_TOKEN` "
            "в Streamlit Secrets."
        )
        return

    st.subheader("Период и список заказов")

    today = datetime.now().date()

    date_col, load_col = st.columns([2, 1])

    with date_col:
        selected_date_range = st.date_input(
            "Период по дате заказа",
            value=(today, today),
            format="DD.MM.YYYY",
            key="reconciliation_date_range",
            help=(
                "Фильтруется поле `customerorder.moment`: "
                "дата документа заказа покупателя. "
                "Дата связанной отгрузки не ограничивается."
            ),
        )

    with load_col:
        st.markdown("<br>", unsafe_allow_html=True)

        load_clicked = st.button(
            "⟳ Загрузить заказы",
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
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Получаю список заказов покупателей "
                "МоегоСклада…"
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
                "Список заказов успешно загружен."
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
            "Выберите период и нажмите «Загрузить заказы»."
        )
        return

    catalog = saved_catalog["catalog"]
    order_rows = catalog["order_rows"]

    if not order_rows:
        st.warning(
            "За выбранный период не найдено заказов покупателей."
        )
        return

    st.caption(
        "Период заказов: "
        f"**{_format_date_range(date_from, date_to)}**. "
        f"Найдено заказов: **{len(order_rows)}**."
    )

    order_by_id = {
        row["id"]: row
        for row in order_rows
    }

    order_ids = list(order_by_id)

    widget_key = (
        "reconciliation_selected_orders_"
        f"{st.session_state['mysklad_reconciliation_order_widget_version']}"
    )

    st.divider()
    st.subheader("Выбор заказов для сверки")

    selected_order_ids = st.multiselect(
        "Найдите и выберите нужные заказы по номеру",
        options=order_ids,
        default=[],
        format_func=lambda order_id: _order_label(
            order_id=order_id,
            order_by_id=order_by_id,
        ),
        key=widget_key,
        placeholder="Введите номер заказа и выберите его",
        help=(
            "Можно выбрать один или несколько заказов. "
            "Поиск внутри списка Streamlit работает по номеру заказа."
        ),
    )

    clear_col, _ = st.columns([1, 3])

    with clear_col:
        clear_clicked = st.button(
            "🧹 Очистить выбор",
            use_container_width=True,
            key="clear_reconciliation_orders",
        )

    if clear_clicked:
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

        st.session_state[
            "mysklad_reconciliation_order_widget_version"
        ] += 1

        st.rerun()

    unique_selected_order_ids = list(
        dict.fromkeys(selected_order_ids)
    )

    if unique_selected_order_ids:
        st.success(
            "Выбрано заказов для сверки: "
            f"{len(unique_selected_order_ids)}."
        )

    result_signature = (
        catalog_signature,
        tuple(sorted(unique_selected_order_ids)),
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

        saved_result = None

    st.markdown("<br>", unsafe_allow_html=True)

    compare_clicked = st.button(
        "⚖️ Сверить заказы и отгрузки",
        type="primary",
        use_container_width=True,
        key="run_order_shipment_reconciliation",
        disabled=not unique_selected_order_ids,
    )

    if compare_clicked:
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Получаю связанные отгрузки, проверяю статусы "
                "и суммирую товары по артикулам…"
            ):
                result = build_order_shipment_reconciliation(
                    client=client,
                    catalog=catalog,
                    selected_order_ids=set(
                        unique_selected_order_ids
                    ),
                )

            st.session_state[
                "mysklad_reconciliation_result"
            ] = {
                "signature": result_signature,
                "result": result,
            }

            st.success(
                "Сверка заказов и связанных отгрузок завершена."
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
            "Выберите нужные заказы и нажмите "
            "«Сверить заказы и отгрузки»."
        )
        return

    _render_result(saved_result["result"])