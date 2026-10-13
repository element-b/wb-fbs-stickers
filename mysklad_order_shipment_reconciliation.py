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
    """Разбирает одну дату либо диапазон из st.date_input."""
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
    """Форматирует период для интерфейса."""
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
    """Форматирует дату документа для отображения."""
    parsed = _parse_moment(value)

    if parsed is None:
        return str(value or "Дата не определена").strip()

    return parsed.strftime("%d.%m.%Y %H:%M")


def _state_id_from_object(value: object) -> str:
    """Извлекает ID статуса из объекта либо из meta.href."""
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
    """Извлекает ID сущности из поля id либо meta.href."""
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
    """Нормализует текст для сравнения статусов."""
    return " ".join(
        str(value or "").strip().split()
    ).casefold()


def _build_state_name_map(
    states: list[dict],
    document_label: str,
) -> dict[str, str]:
    """Строит соответствие ID статуса и его названия."""
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

        existing_name = result.get(state_id)

        if (
            existing_name is not None
            and existing_name != state_name
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
    """Возвращает название статуса заказа либо отгрузки."""
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
    Возвращает уникальные ID отгрузок из `customerorder.demands`.

    Это основной источник связи в диагностическом режиме.
    Если отгрузка есть в этом списке, она включается в диагностику,
    даже если API не вернул обратное поле `customerOrder`.
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

    Диагностика поддерживает только номенклатуру `product` с целым
    неотрицательным количеством. Это соответствует текущей модели
    штучных товаров.
    """
    assortment = position.get("assortment")

    if not isinstance(assortment, dict):
        raise MySkladApiError(
            "МойСклад не вернул развёрнутую номенклатуру позиции "
            f"в документе «{document_label}» `{document_number}`. "
            "Невозможно получить артикул для сверки."
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
            "Диагностика остановлена, чтобы не смешать товары, услуги, "
            "комплекты или модификации."
        )

    article = str(assortment.get("article") or "").strip()

    if not article:
        raise MySkladApiError(
            "У товара в документе "
            f"«{document_label}» `{document_number}` "
            "не заполнен артикул. Невозможно корректно сопоставить "
            "товарные позиции."
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
            "Диагностика поддерживает только целое количество товаров."
        )

    return article, int(quantity)


def _positions_by_article(
    positions: list[dict],
    document_number: str,
    document_label: str,
) -> tuple[dict[str, int], int, int]:
    """
    Суммирует позиции документа по артикулу.

    Возвращает:

    - количество по article;
    - число товарных строк;
    - общее количество товаров.
    """
    by_article: dict[str, int] = {}
    position_count = 0
    total_quantity = 0

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

        by_article[article] = (
            by_article.get(article, 0) + quantity
        )

        position_count += 1
        total_quantity += quantity

    return (
        by_article,
        position_count,
        total_quantity,
    )


def _order_label(
    order_id: str,
    order_by_id: dict[str, dict],
) -> str:
    """Формирует подпись заказа в списке выбора."""
    order = order_by_id[order_id]

    return (
        f"{order['number']}  |  "
        f"{order['moment_text']}"
    )


def _telegram_text(
    date_from: date,
    date_to: date,
    problem_rows: list[dict],
    demand_diagnostic_rows: list[dict],
) -> str:
    """
    Формирует диагностический текст для Telegram.

    Он содержит номер заказа, отклонения, связанные отгрузки
    и статусную диагностику. Это не финальная финансовая сверка,
    пока менеджеры не перейдут на новый регламент.
    """
    problems_by_order: dict[str, list[dict]] = defaultdict(list)

    for row in problem_rows:
        problems_by_order[row["Заказ"]].append(row)

    demands_by_order: dict[str, list[dict]] = defaultdict(list)

    for row in demand_diagnostic_rows:
        demands_by_order[row["Заказ"]].append(row)

    order_numbers = sorted(
        set(problems_by_order) | set(demands_by_order),
        key=lambda value: (value.casefold(), value),
    )

    if not order_numbers:
        return (
            "Диагностика заказов и связанных отгрузок\n"
            f"Период заказов: {_format_date_range(date_from, date_to)}\n\n"
            "Для выбранных заказов нет связанных отгрузок "
            "и отклонений."
        )

    lines = [
        "Диагностика заказов и связанных отгрузок",
        (
            "Период заказов: "
            f"{_format_date_range(date_from, date_to)}"
        ),
        (
            "Внимание: старый заказ может содержать отгрузки "
            "из разных периодов."
        ),
    ]

    for order_number in order_numbers:
        lines.append("")
        lines.append(f"Заказ {order_number}:")

        diagnostic_demands = demands_by_order.get(
            order_number,
            [],
        )

        if diagnostic_demands:
            lines.append("Связанные отгрузки:")

            for demand in diagnostic_demands:
                lines.append(
                    "• "
                    f"{demand['Отгрузка']} — "
                    f"статус: {demand['Статус отгрузки']}; "
                    f"товаров: {demand['Количество товара']}; "
                    f"статус совпадает: {demand['Статус совпадает']}; "
                    f"связь: {demand['Проверка связи']}."
                )
        else:
            lines.append(
                "• В списке связанных документов отгрузки не найдены."
            )

        order_problems = sorted(
            problems_by_order.get(order_number, []),
            key=lambda row: (
                0 if row["Отклонение"] < 0 else 1,
                -abs(row["Отклонение"]),
                row["Артикул"].casefold(),
                row["Артикул"],
            ),
        )

        if order_problems:
            lines.append("Отклонения по товарам:")

            for row in order_problems:
                ordered_quantity = row["Заказано"]
                shipped_quantity = row[
                    "В связанных отгрузках"
                ]
                deviation = row["Отклонение"]

                if deviation < 0:
                    lines.append(
                        "• "
                        f"{row['Артикул']} — "
                        f"заказано {ordered_quantity}, "
                        f"в связанных отгрузках "
                        f"{shipped_quantity}, "
                        f"недобор {abs(deviation)}."
                    )
                else:
                    lines.append(
                        "• "
                        f"{row['Артикул']} — "
                        f"заказано {ordered_quantity}, "
                        f"в связанных отгрузках "
                        f"{shipped_quantity}, "
                        f"перебор {deviation}."
                    )

    return "\n".join(lines)


def build_reconciliation_catalog(
    client: MySkladClient,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    """
    Загружает список заказов покупателей по `customerorder.moment`.

    В каталоге не требуется контрагент и не фильтруется статус:
    пользователь выбирает интересующие заказы вручную по номеру.
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
    Выполняет диагностическое сравнение заказов и связанных отгрузок.

    В отличие от будущей финальной версии:

    - учитываются ВСЕ отгрузки, присутствующие в `order.demands`;
    - несовпадающий статус не исключает отгрузку, а показывается;
    - отсутствие `demand.customerOrder` не исключает отгрузку;
    - обратная ссылка на другой заказ не исключает отгрузку,
      но показывается как конфликт связи;
    - количество всех связанных отгрузок суммируется по article.

    Это позволяет увидеть фактические товары и количества,
    привязанные к старым заказам из разных периодов.
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

    order_state_names = _build_state_name_map(
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
    demand_diagnostic_rows: list[dict] = []
    order_summary_rows: list[dict] = []

    demand_owners: dict[str, list[str]] = {}
    demand_row_indexes: dict[str, list[int]] = {}

    total_linked_demands = 0
    total_demands_without_reverse_link = 0
    total_reverse_link_conflicts = 0
    total_status_mismatches = 0
    orders_without_demands_count = 0
    duplicate_demand_links_count = 0

    for catalog_row in selected_catalog_rows:
        order_id = catalog_row["id"]

        order = client.customer_order_by_id(
            customer_order_id=order_id
        )

        actual_order_id = str(order.get("id") or "").strip()

        if actual_order_id != order_id:
            raise MySkladApiError(
                "МойСклад вернул заказ с ID, не совпадающим "
                "с запрошенным. Диагностика остановлена."
            )

        order_number = _order_number(order)

        order_status = _status_name(
            document=order,
            state_names=order_state_names,
        )

        if not order_status:
            order_status = "Без статуса"

        order_positions = client.customer_order_positions(
            customer_order_id=order_id,
            expand_assortment=True,
        )

        (
            ordered_by_article,
            order_position_count,
            order_total_quantity,
        ) = _positions_by_article(
            positions=order_positions,
            document_number=order_number,
            document_label="Заказ покупателя",
        )

        linked_demand_ids = _linked_demand_ids(order)

        if not linked_demand_ids:
            orders_without_demands_count += 1

        shipped_by_article: dict[str, int] = {}
        linked_demand_numbers: list[str] = []
        status_match_count = 0
        status_mismatch_count = 0
        reverse_missing_count = 0
        reverse_conflict_count = 0
        linked_demand_total_quantity = 0

        for demand_id in linked_demand_ids:
            total_linked_demands += 1

            demand = client.demand_by_id(demand_id)
            demand_number = _demand_number(demand)

            actual_demand_id = str(demand.get("id") or "").strip()

            if actual_demand_id != demand_id:
                raise MySkladApiError(
                    "МойСклад вернул отгрузку с ID, не совпадающим "
                    "с запрошенным. Диагностика остановлена."
                )

            demand_status = _status_name(
                document=demand,
                state_names=demand_state_names,
            )

            if not demand_status:
                demand_status = "Без статуса"

            status_matches = (
                _normal_text(order_status)
                == _normal_text(demand_status)
            )

            if status_matches:
                status_match_text = "Да"
                status_match_count += 1
            else:
                status_match_text = "Нет"
                status_mismatch_count += 1
                total_status_mismatches += 1

            reverse_order_id = _entity_id(
                demand.get("customerOrder")
            )

            if not reverse_order_id:
                reverse_missing_count += 1
                total_demands_without_reverse_link += 1

                link_check = (
                    "Только в связанных документах заказа"
                )

            elif reverse_order_id == order_id:
                link_check = (
                    "Подтверждена в заказе и в отгрузке"
                )

            else:
                reverse_conflict_count += 1
                total_reverse_link_conflicts += 1

                link_check = (
                    "Конфликт: отгрузка ссылается "
                    "на другой заказ"
                )

            demand_positions = client.demand_positions(
                demand_id=demand_id,
                expand_assortment=True,
            )

            (
                demand_by_article,
                demand_position_count,
                demand_total_quantity,
            ) = _positions_by_article(
                positions=demand_positions,
                document_number=demand_number,
                document_label="Отгрузка",
            )

            for article, quantity in demand_by_article.items():
                shipped_by_article[article] = (
                    shipped_by_article.get(article, 0)
                    + quantity
                )

            linked_demand_numbers.append(demand_number)
            linked_demand_total_quantity += demand_total_quantity

            previous_owners = demand_owners.get(demand_id, [])

            duplicate_link = bool(
                previous_owners
                and order_number not in previous_owners
            )

            if duplicate_link:
                duplicate_demand_links_count += 1

            demand_owners.setdefault(
                demand_id,
                [],
            ).append(order_number)

            row = {
                "Заказ": order_number,
                "Статус заказа": order_status,
                "Отгрузка": demand_number,
                "Статус отгрузки": demand_status,
                "Статус совпадает": status_match_text,
                "Проверка связи": link_check,
                "Повторная связь": (
                    "Да" if duplicate_link else "Нет"
                ),
                "Товарных позиций": demand_position_count,
                "Уникальных артикулов": len(
                    demand_by_article
                ),
                "Количество товара": demand_total_quantity,
            }

            demand_row_indexes.setdefault(
                demand_id,
                [],
            ).append(len(demand_diagnostic_rows))

            demand_diagnostic_rows.append(row)

            if duplicate_link:
                for row_index in demand_row_indexes[demand_id]:
                    demand_diagnostic_rows[row_index][
                        "Повторная связь"
                    ] = "Да"

        article_keys = sorted(
            set(ordered_by_article) | set(shipped_by_article),
            key=lambda value: (value.casefold(), value),
        )

        linked_demands_text = (
            ", ".join(linked_demand_numbers)
            if linked_demand_numbers
            else "Нет связанных отгрузок"
        )

        for article in article_keys:
            ordered_quantity = ordered_by_article.get(article, 0)
            shipped_quantity = shipped_by_article.get(article, 0)
            deviation = shipped_quantity - ordered_quantity

            if deviation < 0:
                result = "Диагностический недобор"
            elif deviation > 0:
                result = "Диагностический перебор"
            else:
                result = "Совпадает"

            detail_row = {
                "Заказ": order_number,
                "Статус заказа": order_status,
                "Связанные отгрузки": linked_demands_text,
                "Артикул": article,
                "Заказано": ordered_quantity,
                "В связанных отгрузках": shipped_quantity,
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
                    "В связанных отгрузках": 0,
                    "Заказов": set(),
                    "Отгрузок": set(),
                },
            )

            aggregate_row["В заказах"] += ordered_quantity
            aggregate_row["В связанных отгрузках"] += (
                shipped_quantity
            )
            aggregate_row["Заказов"].add(order_number)

            for demand_number in linked_demand_numbers:
                aggregate_row["Отгрузок"].add(demand_number)

        order_summary_rows.append(
            {
                "Заказ": order_number,
                "Статус заказа": order_status,
                "Товарных позиций в заказе": order_position_count,
                "Количество товара в заказе": order_total_quantity,
                "Связанных отгрузок": len(linked_demand_ids),
                "Количество в связанных отгрузках": (
                    linked_demand_total_quantity
                ),
                "Статусы совпали": status_match_count,
                "Статусы не совпали": status_mismatch_count,
                "Без обратной связи": reverse_missing_count,
                "Конфликтов обратной связи": reverse_conflict_count,
            }
        )

    aggregate_rows: list[dict] = []

    for aggregate_row in aggregate_by_article.values():
        deviation = (
            aggregate_row["В связанных отгрузках"]
            - aggregate_row["В заказах"]
        )

        if deviation < 0:
            result = "Диагностический недобор"
        elif deviation > 0:
            result = "Диагностический перебор"
        else:
            result = "Совпадает"

        aggregate_rows.append(
            {
                "Артикул": aggregate_row["Артикул"],
                "В заказах": aggregate_row["В заказах"],
                "В связанных отгрузках": (
                    aggregate_row["В связанных отгрузках"]
                ),
                "Отклонение": deviation,
                "Результат": result,
                "Заказов": len(aggregate_row["Заказов"]),
                "Отгрузок": len(aggregate_row["Отгрузок"]),
            }
        )

    order_summary_rows.sort(
        key=lambda row: (
            row["Заказ"].casefold(),
            row["Заказ"],
        )
    )

    demand_diagnostic_rows.sort(
        key=lambda row: (
            row["Заказ"].casefold(),
            row["Заказ"],
            row["Отгрузка"].casefold(),
            row["Отгрузка"],
        )
    )

    detail_rows.sort(
        key=lambda row: (
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
            0 if row["Отклонение"] < 0 else 1,
            -abs(row["Отклонение"]),
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    problem_aggregate_rows = [
        row
        for row in aggregate_rows
        if row["Отклонение"] != 0
    ]

    matching_aggregate_rows = [
        row
        for row in aggregate_rows
        if row["Отклонение"] == 0
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
        "orders_without_demands_count": (
            orders_without_demands_count
        ),
        "total_linked_demands": total_linked_demands,
        "demands_without_reverse_link_count": (
            total_demands_without_reverse_link
        ),
        "reverse_link_conflicts_count": (
            total_reverse_link_conflicts
        ),
        "status_mismatches_count": total_status_mismatches,
        "duplicate_demand_links_count": (
            duplicate_demand_links_count
        ),
        "total_shortage": total_shortage,
        "total_overage": total_overage,
        "order_summary_rows": order_summary_rows,
        "demand_diagnostic_rows": demand_diagnostic_rows,
        "detail_rows": detail_rows,
        "problem_rows": problem_rows,
        "aggregate_rows": aggregate_rows,
        "problem_aggregate_rows": problem_aggregate_rows,
        "matching_aggregate_rows": matching_aggregate_rows,
        "telegram_text": _telegram_text(
            date_from=catalog["date_from"],
            date_to=catalog["date_to"],
            problem_rows=problem_rows,
            demand_diagnostic_rows=demand_diagnostic_rows,
        ),
    }


def _render_order_summary_table(
    rows: list[dict],
) -> None:
    """Показывает диагностическую сводку по выбранным заказам."""
    columns = [
        "Заказ",
        "Статус заказа",
        "Товарных позиций в заказе",
        "Количество товара в заказе",
        "Связанных отгрузок",
        "Количество в связанных отгрузках",
        "Статусы совпали",
        "Статусы не совпали",
        "Без обратной связи",
        "Конфликтов обратной связи",
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
            "Товарных позиций в заказе": st.column_config.NumberColumn(
                "Товарных позиций в заказе",
                format="%d",
            ),
            "Количество товара в заказе": st.column_config.NumberColumn(
                "Количество товара в заказе",
                format="%d",
            ),
            "Связанных отгрузок": st.column_config.NumberColumn(
                "Связанных отгрузок",
                format="%d",
            ),
            "Количество в связанных отгрузках": (
                st.column_config.NumberColumn(
                    "Количество в связанных отгрузках",
                    format="%d",
                )
            ),
            "Статусы совпали": st.column_config.NumberColumn(
                "Статусы совпали",
                format="%d",
            ),
            "Статусы не совпали": st.column_config.NumberColumn(
                "Статусы не совпали",
                format="%d",
            ),
            "Без обратной связи": st.column_config.NumberColumn(
                "Без обратной связи",
                format="%d",
            ),
            "Конфликтов обратной связи": st.column_config.NumberColumn(
                "Конфликтов обратной связи",
                format="%d",
            ),
        },
    )


def _render_demand_diagnostic_table(
    rows: list[dict],
) -> None:
    """Показывает все связанные отгрузки и их диагностику."""
    columns = [
        "Заказ",
        "Статус заказа",
        "Отгрузка",
        "Статус отгрузки",
        "Статус совпадает",
        "Проверка связи",
        "Повторная связь",
        "Товарных позиций",
        "Уникальных артикулов",
        "Количество товара",
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
            "Отгрузка": st.column_config.TextColumn(
                "Отгрузка",
                width="medium",
            ),
            "Статус отгрузки": st.column_config.TextColumn(
                "Статус отгрузки",
                width="medium",
            ),
            "Статус совпадает": st.column_config.TextColumn(
                "Статус совпадает",
                width="small",
            ),
            "Проверка связи": st.column_config.TextColumn(
                "Проверка связи",
                width="large",
            ),
            "Повторная связь": st.column_config.TextColumn(
                "Повторная связь",
                width="small",
            ),
            "Товарных позиций": st.column_config.NumberColumn(
                "Товарных позиций",
                format="%d",
            ),
            "Уникальных артикулов": st.column_config.NumberColumn(
                "Уникальных артикулов",
                format="%d",
            ),
            "Количество товара": st.column_config.NumberColumn(
                "Количество товара",
                format="%d",
            ),
        },
    )


def _render_detail_table(
    rows: list[dict],
) -> None:
    """Показывает сравнение article и quantity по каждому заказу."""
    columns = [
        "Заказ",
        "Статус заказа",
        "Связанные отгрузки",
        "Артикул",
        "Заказано",
        "В связанных отгрузках",
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
            "Статус заказа": st.column_config.TextColumn(
                "Статус заказа",
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
            "В связанных отгрузках": st.column_config.NumberColumn(
                "В связанных отгрузках",
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
        },
    )


def _render_aggregate_table(
    rows: list[dict],
) -> None:
    """Показывает диагностическую агрегацию по артикулам."""
    columns = [
        "Артикул",
        "В заказах",
        "В связанных отгрузках",
        "Отклонение",
        "Результат",
        "Заказов",
        "Отгрузок",
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
            "В связанных отгрузках": st.column_config.NumberColumn(
                "В связанных отгрузках",
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
        },
    )


def _render_result(
    result: dict[str, Any],
) -> None:
    """Отображает диагностический результат."""
    st.divider()
    st.subheader("🧪 Диагностическая сводка")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric(
        "Выбрано заказов",
        result["selected_order_count"],
    )

    metric_2.metric(
        "Связанных отгрузок найдено",
        result["total_linked_demands"],
    )

    metric_3.metric(
        "Статусы не совпали",
        result["status_mismatches_count"],
    )

    metric_4.metric(
        "Заказов без отгрузок",
        result["orders_without_demands_count"],
    )

    metric_5, metric_6, metric_7, metric_8 = st.columns(4)

    metric_5.metric(
        "Без обратной связи",
        result["demands_without_reverse_link_count"],
    )

    metric_6.metric(
        "Конфликтов обратной связи",
        result["reverse_link_conflicts_count"],
    )

    metric_7.metric(
        "Повторных связей",
        result["duplicate_demand_links_count"],
    )

    metric_8.metric(
        "Диагностический недобор",
        result["total_shortage"],
    )

    st.caption(
        "Период фильтрует только дату `customerorder.moment`: "
        f"**{_format_date_range(result['date_from'], result['date_to'])}**."
    )

    st.warning(
        "Это диагностический режим. В расчёт включены все отгрузки "
        "из связанных документов заказа, даже если статусы не совпадают "
        "или обратная связь `customerOrder` отсутствует. "
        "Старые заказы могут содержать отгрузки из разных периодов, "
        "поэтому результат пока нельзя использовать как финальную "
        "сверку для маркетологов."
    )

    if result["total_linked_demands"] == 0:
        st.error(
            "У выбранных заказов API не вернул связанных отгрузок "
            "в поле `demands`. Проверьте в МоемСкладе блок "
            "«Связанные документы» выбранного заказа."
        )

    elif result["problem_rows"]:
        st.info(
            "Найдены диагностические различия между текущим составом "
            "заказов и всеми связанными отгрузками."
        )
    else:
        st.success(
            "По текущему набору связанных отгрузок количества "
            "по артикулам совпали."
        )

    st.divider()
    st.subheader("📦 Заказы и связанные отгрузки")

    _render_order_summary_table(
        result["order_summary_rows"]
    )

    st.divider()
    st.subheader("🔗 Диагностика связанных отгрузок")

    if not result["demand_diagnostic_rows"]:
        st.info(
            "У выбранных заказов нет связанных отгрузок."
        )
    else:
        st.caption(
            "Отгрузки показаны независимо от совпадения статусов. "
            "Это позволяет увидеть фактические связанные документы "
            "и их товарное количество."
        )

        _render_demand_diagnostic_table(
            result["demand_diagnostic_rows"]
        )

    st.divider()
    st.subheader("📋 Текст для Telegram")

    st.caption(
        "Текст содержит номер каждого рассматриваемого заказа, "
        "связанные отгрузки, их статусы и диагностические отклонения."
    )

    st.code(
        result["telegram_text"],
        language=None,
    )

    st.divider()
    st.subheader("⚠️ Диагностические отклонения по заказам")

    if not result["problem_rows"]:
        st.success(
            "По выбранным заказам артикулы и количества совпали."
        )
    else:
        _render_detail_table(result["problem_rows"])

    st.divider()
    st.subheader("📌 Итог по артикулам")

    if not result["problem_aggregate_rows"]:
        st.info(
            "Нет артикулов с диагностическими отклонениями."
        )
    else:
        if result["duplicate_demand_links_count"] > 0:
            st.warning(
                "Одна или несколько отгрузок встречаются у нескольких "
                "выбранных заказов. Итог по артикулам может содержать "
                "повторное количество; для проверки используйте "
                "детализацию по каждому заказу."
            )

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


def _get_mysklad_token() -> str:
    """Безопасно читает токен МоегоСклада из Streamlit Secrets."""
    try:
        return str(
            st.secrets["MYSKLAD_API_TOKEN"]
        ).strip()
    except Exception:
        return ""


def render_mysklad_order_shipment_reconciliation_tab() -> None:
    """
    Отображает диагностическую вкладку заказов и связанных отгрузок.

    Все вызовы API выполняются методом GET. Приложение не изменяет
    заказы, отгрузки, статусы, позиции или связанные документы.
    """
    st.title("🧪 Диагностика заказов и отгрузок FBO")

    st.markdown(
        """
        <div class="description-box">
            Выберите период по дате заказа, загрузите список и выберите
            интересующие заказы по номеру. Приложение одновременно
            получает артикулы и количества из заказа и из всех его
            связанных отгрузок.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Цель текущего этапа — проверить фактические связи, статусы, "
        "артикулы и количества перед внедрением нового регламента "
        "закрытия заказов."
    )

    mysklad_token = _get_mysklad_token()

    if not mysklad_token:
        st.error(
            "Для диагностики не настроен `MYSKLAD_API_TOKEN` "
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
                "Фильтруется `customerorder.moment`. "
                "Дата связанных отгрузок намеренно не ограничивается: "
                "нужно увидеть все документы из связей заказа."
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
    st.subheader("Выбор заказов для диагностики")

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
            "Поиск Streamlit работает по номеру заказа."
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
            "Выбрано заказов для диагностики: "
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

    diagnose_clicked = st.button(
        "🧪 Диагностировать заказы и отгрузки",
        type="primary",
        use_container_width=True,
        key="run_order_shipment_reconciliation",
        disabled=not unique_selected_order_ids,
    )

    if diagnose_clicked:
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Загружаю артикулы и количество заказа, затем "
                "артикулы и количество всех связанных отгрузок…"
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
                "Диагностика заказов и связанных отгрузок завершена."
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
            "«Диагностировать заказы и отгрузки»."
        )
        return

    _render_result(saved_result["result"])