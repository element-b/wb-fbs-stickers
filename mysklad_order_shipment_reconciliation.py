from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import streamlit as st

from mysklad_api import MySkladApiError, MySkladClient


DEFAULT_RECONCILIATION_STATUS_NAME = "Отгружен"


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
    """Форматирует период для отображения."""
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
    """Извлекает ID статуса из объекта или его meta.href."""
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


def _entity_href(value: object) -> str:
    """Возвращает href из meta объекта связанной сущности."""
    if not isinstance(value, dict):
        return ""

    meta = value.get("meta")

    if not isinstance(meta, dict):
        return ""

    return str(meta.get("href") or "").strip().rstrip("/")


def _entity_id(value: object) -> str:
    """Возвращает ID сущности из поля id или meta.href."""
    if not isinstance(value, dict):
        return ""

    direct_id = str(value.get("id") or "").strip()

    if direct_id:
        return direct_id

    href = _entity_href(value)

    if not href:
        return ""

    return href.rsplit("/", maxsplit=1)[-1]


def _normal_status_name(value: object) -> str:
    """Нормализует название статуса для безопасного сравнения."""
    return " ".join(
        str(value or "").strip().split()
    ).casefold()


def _build_state_name_map(
    states: list[dict],
    document_label: str,
) -> dict[str, str]:
    """Строит соответствие ID статуса и названия."""
    result: dict[str, str] = {}

    for state in states:
        state_id = _state_id_from_object(state)
        state_name = str(state.get("name") or "").strip()

        if not state_id or not state_name:
            raise MySkladApiError(
                "МойСклад вернул некорректный статус "
                f"для документа «{document_label}»."
            )

        existing_name = result.get(state_id)

        if existing_name is not None and existing_name != state_name:
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


def _common_status_options(
    customer_order_states: list[dict],
    demand_states: list[dict],
) -> list[dict[str, str]]:
    """
    Формирует общие названия статусов заказов и отгрузок.

    Технические UUID статусов разных документов отличаются, поэтому
    сопоставление выполняется по нормализованному названию статуса.
    """
    order_state_names = _build_state_name_map(
        states=customer_order_states,
        document_label="Заказ покупателя",
    )

    demand_state_names = _build_state_name_map(
        states=demand_states,
        document_label="Отгрузка",
    )

    order_by_name = {
        _normal_status_name(name): name
        for name in order_state_names.values()
        if _normal_status_name(name)
    }

    demand_by_name = {
        _normal_status_name(name): name
        for name in demand_state_names.values()
        if _normal_status_name(name)
    }

    common_keys = set(order_by_name) & set(demand_by_name)

    return [
        {
            "key": key,
            "name": order_by_name[key],
        }
        for key in sorted(
            common_keys,
            key=lambda key: (
                order_by_name[key].casefold(),
                order_by_name[key],
            ),
        )
    ]


def _status_name(
    document: dict,
    state_name_by_id: dict[str, str],
) -> str:
    """Возвращает название статуса документа."""
    state = document.get("state")

    if not isinstance(state, dict):
        return ""

    embedded_name = str(state.get("name") or "").strip()

    if embedded_name:
        return embedded_name

    state_id = _state_id_from_object(state)

    return state_name_by_id.get(state_id, "")


def _agent_key(agent: object) -> str:
    """
    Возвращает стабильный ключ контрагента.

    Используется meta.href, потому что UUID разных типов сущностей
    потенциально может быть недостаточен для полной идентификации.
    """
    return _entity_href(agent)


def _agent_name(agent: object) -> str:
    """Возвращает отображаемое имя контрагента."""
    if not isinstance(agent, dict):
        return ""

    return str(agent.get("name") or "").strip()


def _order_number(order: dict) -> str:
    """Возвращает номер заказа без данных покупателя."""
    value = str(order.get("name") or "").strip()

    return value or "Без номера"


def _demand_number(demand: dict) -> str:
    """Возвращает номер отгрузки без данных покупателя."""
    value = str(demand.get("name") or "").strip()

    return value or "Без номера"


def _linked_demand_ids(order: dict) -> list[str]:
    """
    Возвращает ID отгрузок только из связанных документов заказа.

    Основной источник связи — `customerorder.demands`, как требуется
    бизнес-правилом. Одна и та же связанная отгрузка не учитывается
    дважды даже при повторе ссылки в ответе API.
    """
    demands = order.get("demands")

    if demands is None:
        return []

    if not isinstance(demands, list):
        raise MySkladApiError(
            "МойСклад вернул некорректное поле `demands` "
            "у заказа покупателя."
        )

    result: list[str] = []
    seen_ids: set[str] = set()

    for demand_reference in demands:
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
    Возвращает article и quantity товарной позиции.

    В сверке учитываются только позиции типа `product` с целым
    неотрицательным количеством. Это соответствует согласованной
    модели: в документах находятся штучные товары.
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
    """Суммирует товарные позиции документа по точному article."""
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


def _copyable_telegram_text(
    date_from: date,
    date_to: date,
    problem_order_rows: list[dict],
    excluded_rows: list[dict],
) -> str:
    """
    Формирует текст для ручной отправки менеджерам в Telegram.

    Номер каждого рассматриваемого заказа обязателен в блоке.
    """
    grouped_problems: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for row in problem_order_rows:
        grouped_problems[row["Контрагент"]][row["Заказ"]].append(row)

    grouped_excluded: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for row in excluded_rows:
        grouped_excluded[row["Контрагент"]][row["Заказ"]].append(row)

    agent_names = sorted(
        set(grouped_problems) | set(grouped_excluded),
        key=lambda value: (value.casefold(), value),
    )

    if not agent_names:
        return (
            "Сверка заказов и связанных отгрузок\n"
            f"Период заказов: {_format_date_range(date_from, date_to)}\n\n"
            "Отклонений и проблемных связей не найдено."
        )

    blocks: list[str] = []

    for agent_name in agent_names:
        lines = [
            agent_name,
            (
                "Период заказов: "
                f"{_format_date_range(date_from, date_to)}"
            ),
        ]

        problems_by_order = grouped_problems.get(agent_name, {})

        if problems_by_order:
            lines.append("")
            lines.append("Недобор / перебор:")

            for order_number in sorted(
                problems_by_order,
                key=lambda value: (value.casefold(), value),
            ):
                lines.append(f"Заказ {order_number}:")

                rows = sorted(
                    problems_by_order[order_number],
                    key=lambda row: (
                        row["Артикул"].casefold(),
                        row["Артикул"],
                    ),
                )

                for row in rows:
                    ordered = row["Заказано"]
                    shipped = row["Отгружено"]
                    deviation = row["Отклонение"]

                    if deviation < 0:
                        lines.append(
                            "• "
                            f"{row['Артикул']} — "
                            f"заказано {ordered}, "
                            f"отгружено {shipped}, "
                            f"недобор {abs(deviation)}."
                        )
                    else:
                        lines.append(
                            "• "
                            f"{row['Артикул']} — "
                            f"заказано {ordered}, "
                            f"отгружено {shipped}, "
                            f"перебор {deviation}."
                        )

        excluded_by_order = grouped_excluded.get(agent_name, {})

        if excluded_by_order:
            lines.append("")
            lines.append("Требуют проверки:")

            for order_number in sorted(
                excluded_by_order,
                key=lambda value: (value.casefold(), value),
            ):
                reasons = sorted(
                    {
                        row["Причина"]
                        for row in excluded_by_order[order_number]
                    }
                )

                lines.append(
                    f"• Заказ {order_number} — "
                    + "; ".join(reasons)
                    + "."
                )

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_reconciliation_catalog(
    client: MySkladClient,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    """
    Загружает фильтры и список заказов за период.

    Период применяется только к `customerorder.moment`.
    Связанные отгрузки позднее получают по `demands` выбранных заказов:
    их собственный `moment` намеренно не ограничивается.
    """
    customer_order_states = client.customer_order_states()
    demand_states = client.demand_states()

    order_state_names = _build_state_name_map(
        states=customer_order_states,
        document_label="Заказ покупателя",
    )

    common_statuses = _common_status_options(
        customer_order_states=customer_order_states,
        demand_states=demand_states,
    )

    orders = client.customer_orders_by_moment(
        date_from=date_from,
        date_to=date_to,
    )

    order_rows: list[dict] = []
    agent_options_map: dict[str, str] = {}
    seen_order_ids: set[str] = set()

    for order in orders:
        order_id = str(order.get("id") or "").strip()

        if not order_id:
            raise MySkladApiError(
                "МойСклад вернул заказ покупателя без корректного ID."
            )

        if order_id in seen_order_ids:
            raise MySkladApiError(
                "МойСклад вернул один и тот же заказ покупателя повторно. "
                "Список не сформирован, чтобы исключить двойной учёт."
            )

        seen_order_ids.add(order_id)

        agent = order.get("agent")
        agent_key = _agent_key(agent)
        agent_name = _agent_name(agent)

        if not agent_key or not agent_name:
            raise MySkladApiError(
                "У заказа покупателя отсутствует заполненный контрагент. "
                "Список не сформирован, чтобы не смешать направления."
            )

        agent_options_map[agent_key] = agent_name

        state_name = _status_name(
            document=order,
            state_name_by_id=order_state_names,
        )

        if not state_name:
            state_name = "Без статуса"

        order_rows.append(
            {
                "id": order_id,
                "number": _order_number(order),
                "moment": order.get("moment"),
                "moment_text": _format_moment(
                    order.get("moment")
                ),
                "agent_id": agent_key,
                "agent_name": agent_name,
                "status_name": state_name,
                "status_key": _normal_status_name(state_name),
            }
        )

    order_rows.sort(
        key=lambda row: (
            row["moment_text"],
            row["number"].casefold(),
            row["number"],
            row["id"],
        ),
        reverse=True,
    )

    agent_options = [
        {
            "id": agent_id,
            "name": agent_name,
        }
        for agent_id, agent_name in agent_options_map.items()
    ]

    agent_options.sort(
        key=lambda row: (
            row["name"].casefold(),
            row["name"],
            row["id"],
        )
    )

    return {
        "date_from": date_from,
        "date_to": date_to,
        "common_statuses": common_statuses,
        "agent_options": agent_options,
        "order_rows": order_rows,
    }


def build_order_shipment_reconciliation(
    client: MySkladClient,
    catalog: dict[str, Any],
    selected_order_ids: set[str],
    selected_status_keys: set[str],
) -> dict[str, Any]:
    """
    Сверяет выбранные заказы покупателей и связанные отгрузки.

    Условия включения отгрузки:

    - находится в `customerorder.demands`;
    - статус совпадает со статусом заказа;
    - общий статус выбран пользователем;
    - обратная связь `demand.customerOrder` указывает на этот заказ;
    - контрагент отгрузки совпадает с контрагентом заказа.

    Несколько связанных корректных отгрузок одного заказа суммируются
    по article. Одна отгрузка не может быть учтена дважды.
    """
    order_rows = catalog.get("order_rows")

    if not isinstance(order_rows, list):
        raise ValueError(
            "Список заказов устарел. Загрузите заказы и фильтры заново."
        )

    selected_ids = {
        str(value).strip()
        for value in selected_order_ids
        if str(value).strip()
    }

    selected_statuses = {
        str(value).strip()
        for value in selected_status_keys
        if str(value).strip()
    }

    if not selected_ids:
        raise ValueError(
            "Выберите хотя бы один заказ покупателя."
        )

    if not selected_statuses:
        raise ValueError(
            "Выберите хотя бы один общий статус для сверки."
        )

    catalog_by_id = {
        str(row.get("id") or "").strip(): row
        for row in order_rows
        if isinstance(row, dict)
    }

    unknown_order_ids = selected_ids - set(catalog_by_id)

    if unknown_order_ids:
        raise ValueError(
            "Выбран один или несколько заказов, которых нет в текущем "
            "списке периода. Загрузите заказы и фильтры заново."
        )

    selected_catalog_rows = [
        catalog_by_id[order_id]
        for order_id in selected_ids
    ]

    selected_catalog_rows = [
        row
        for row in selected_catalog_rows
        if row["status_key"] in selected_statuses
    ]

    if not selected_catalog_rows:
        raise ValueError(
            "Среди выбранных заказов нет заказов с выбранными статусами."
        )

    customer_order_states = client.customer_order_states()
    demand_states = client.demand_states()

    order_state_names = _build_state_name_map(
        states=customer_order_states,
        document_label="Заказ покупателя",
    )

    demand_state_names = _build_state_name_map(
        states=demand_states,
        document_label="Отгрузка",
    )

    aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}
    order_detail_rows: list[dict] = []
    problem_rows: list[dict] = []
    excluded_rows: list[dict] = []

    checked_demand_ids: set[str] = set()

    included_order_count = 0
    orders_without_demands_count = 0
    linked_demands_count = 0
    included_demands_count = 0
    excluded_demands_count = 0

    for catalog_row in sorted(
        selected_catalog_rows,
        key=lambda row: (
            row["agent_name"].casefold(),
            row["agent_name"],
            row["number"].casefold(),
            row["number"],
        ),
    ):
        order_id = catalog_row["id"]
        order = client.customer_order_by_id(order_id)

        actual_order_id = str(order.get("id") or "").strip()

        if actual_order_id != order_id:
            raise MySkladApiError(
                "МойСклад вернул заказ с ID, не совпадающим "
                "с запрошенным. Сверка остановлена."
            )

        order_number = _order_number(order)

        order_agent = order.get("agent")
        order_agent_id = _agent_key(order_agent)
        order_agent_name = _agent_name(order_agent)

        if not order_agent_id or not order_agent_name:
            raise MySkladApiError(
                f"У заказа `{order_number}` отсутствует контрагент. "
                "Сверка остановлена."
            )

        order_status = _status_name(
            document=order,
            state_name_by_id=order_state_names,
        )

        order_status_key = _normal_status_name(order_status)

        if order_status_key not in selected_statuses:
            excluded_rows.append(
                {
                    "Контрагент": order_agent_name,
                    "Заказ": order_number,
                    "ID заказа": order_id,
                    "Статус заказа": order_status or "Без статуса",
                    "Связанная отгрузка": "—",
                    "Причина": (
                        "Статус заказа изменился после загрузки списка "
                        "и больше не входит в выбранный фильтр."
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
        current_order_has_integrity_problem = False

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

            demand_status = _status_name(
                document=demand,
                state_name_by_id=demand_state_names,
            )

            demand_status_key = _normal_status_name(demand_status)

            demand_agent = demand.get("agent")
            demand_agent_id = _agent_key(demand_agent)

            linked_customer_order = demand.get("customerOrder")
            reverse_order_id = _entity_id(linked_customer_order)

            reason = ""

            if demand_id in checked_demand_ids:
                reason = (
                    "Эта отгрузка уже была учтена у другого выбранного "
                    "заказа. Повторный учёт заблокирован."
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

            elif not demand_agent_id:
                reason = "У связанной отгрузки отсутствует контрагент."

            elif demand_agent_id != order_agent_id:
                reason = (
                    "Контрагент связанной отгрузки не совпадает "
                    "с контрагентом заказа."
                )

            elif demand_status_key != order_status_key:
                reason = (
                    "Статус отгрузки не совпадает "
                    "со статусом заказа."
                )

            elif demand_status_key not in selected_statuses:
                reason = (
                    "Статус связанной отгрузки не входит "
                    "в выбранный фильтр."
                )

            if reason:
                excluded_demands_count += 1
                current_order_has_integrity_problem = True

                excluded_rows.append(
                    {
                        "Контрагент": order_agent_name,
                        "Заказ": order_number,
                        "ID заказа": order_id,
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

        included_order_count += 1

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

            order_detail_rows.append(
                {
                    "Контрагент": order_agent_name,
                    "Заказ": order_number,
                    "ID заказа": order_id,
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
            )

            if deviation != 0:
                problem_rows.append(
                    {
                        "Контрагент": order_agent_name,
                        "Заказ": order_number,
                        "ID заказа": order_id,
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
                )

            aggregate_key = (
                order_agent_id,
                order_agent_name,
                article,
            )

            aggregate_row = aggregate.setdefault(
                aggregate_key,
                {
                    "Контрагент": order_agent_name,
                    "Артикул": article,
                    "В заказах": 0,
                    "В отгрузках": 0,
                    "Заказов": set(),
                    "Отгрузок": set(),
                },
            )

            aggregate_row["В заказах"] += ordered_quantity
            aggregate_row["В отгрузках"] += shipped_quantity
            aggregate_row["Заказов"].add(order_id)

            for demand_number in included_demand_numbers:
                aggregate_row["Отгрузок"].add(demand_number)

        if current_order_has_integrity_problem:
            continue

    aggregate_rows: list[dict] = []

    for row in aggregate.values():
        deviation = row["В отгрузках"] - row["В заказах"]

        if deviation < 0:
            result = "Недобор"
        elif deviation > 0:
            result = "Перебор"
        else:
            result = "Совпадает"

        aggregate_rows.append(
            {
                "Контрагент": row["Контрагент"],
                "Артикул": row["Артикул"],
                "В заказах": row["В заказах"],
                "В отгрузках": row["В отгрузках"],
                "Отклонение": deviation,
                "Результат": result,
                "Заказов": len(row["Заказов"]),
                "Отгрузок": len(row["Отгрузок"]),
            }
        )

    aggregate_rows.sort(
        key=lambda row: (
            0 if row["Результат"] == "Недобор" else 1,
            -abs(row["Отклонение"]),
            row["Контрагент"].casefold(),
            row["Контрагент"],
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    order_detail_rows.sort(
        key=lambda row: (
            row["Контрагент"].casefold(),
            row["Контрагент"],
            row["Заказ"].casefold(),
            row["Заказ"],
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    problem_rows.sort(
        key=lambda row: (
            row["Контрагент"].casefold(),
            row["Контрагент"],
            row["Заказ"].casefold(),
            row["Заказ"],
            0 if row["Результат"] == "Недобор" else 1,
            -abs(row["Отклонение"]),
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    excluded_rows.sort(
        key=lambda row: (
            row["Контрагент"].casefold(),
            row["Контрагент"],
            row["Заказ"].casefold(),
            row["Заказ"],
            row["Связанная отгрузка"].casefold(),
            row["Связанная отгрузка"],
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
        for row in problem_aggregate_rows
        if row["Отклонение"] < 0
    )

    total_overage = sum(
        row["Отклонение"]
        for row in problem_aggregate_rows
        if row["Отклонение"] > 0
    )

    return {
        "date_from": catalog["date_from"],
        "date_to": catalog["date_to"],
        "generated_at": datetime.now(),
        "selected_order_count": len(selected_catalog_rows),
        "included_order_count": included_order_count,
        "orders_without_demands_count": orders_without_demands_count,
        "linked_demands_count": linked_demands_count,
        "included_demands_count": included_demands_count,
        "excluded_demands_count": excluded_demands_count,
        "shortage_article_count": sum(
            row["Отклонение"] < 0
            for row in problem_aggregate_rows
        ),
        "overage_article_count": sum(
            row["Отклонение"] > 0
            for row in problem_aggregate_rows
        ),
        "total_shortage": total_shortage,
        "total_overage": total_overage,
        "aggregate_rows": aggregate_rows,
        "problem_aggregate_rows": problem_aggregate_rows,
        "matching_aggregate_rows": matching_aggregate_rows,
        "order_detail_rows": order_detail_rows,
        "problem_rows": problem_rows,
        "excluded_rows": excluded_rows,
        "telegram_text": _copyable_telegram_text(
            date_from=catalog["date_from"],
            date_to=catalog["date_to"],
            problem_order_rows=problem_rows,
            excluded_rows=excluded_rows,
        ),
    }


def _render_aggregate_table(
    rows: list[dict],
) -> None:
    """Показывает сводную таблицу отклонений по артикулам."""
    columns = [
        "Контрагент",
        "Артикул",
        "В заказах",
        "В отгрузках",
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
            "Контрагент": st.column_config.TextColumn(
                "Контрагент",
                width="medium",
            ),
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
                width="small",
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


def _render_order_detail_table(
    rows: list[dict],
) -> None:
    """Показывает детализацию по заказам и артикулам."""
    columns = [
        "Контрагент",
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
            "Контрагент": st.column_config.TextColumn(
                "Контрагент",
                width="medium",
            ),
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
    """Показывает связи, не вошедшие в расчёт."""
    columns = [
        "Контрагент",
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
            "Контрагент": st.column_config.TextColumn(
                "Контрагент",
                width="medium",
            ),
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
        result["included_order_count"],
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
        "Артикулов с недобором",
        result["shortage_article_count"],
    )

    metric_6.metric(
        "Всего недобор",
        result["total_shortage"],
    )

    metric_7.metric(
        "Артикулов с перебором",
        result["overage_article_count"],
    )

    metric_8.metric(
        "Всего перебор",
        result["total_overage"],
    )

    st.caption(
        "Период фильтрует `moment` заказов покупателей: "
        f"**{_format_date_range(result['date_from'], result['date_to'])}**."
    )

    st.caption(
        "В отгрузках учитываются только документы из связанных "
        "документов выбранного заказа. Если у заказа несколько "
        "связанных отгрузок, их количество по одинаковому артикулу "
        "суммируется."
    )

    if result["problem_aggregate_rows"]:
        st.error(
            "Найдены недобор или перебор по артикулам. "
            "Скопируйте подготовленный текст и отправьте нужному "
            "маркетологу."
        )
    else:
        st.success(
            "Недобора и перебора по включённым заказам не найдено."
        )

    if result["excluded_demands_count"] > 0:
        st.warning(
            "Некоторые связанные отгрузки не вошли в расчёт из-за "
            "несовпадения статуса, контрагента либо обратной связи "
            "с заказом. Они приведены в блоке «Требуют проверки»."
        )

    st.divider()
    st.subheader("📋 Текст для Telegram")

    st.caption(
        "Каждый блок содержит контрагента, период и номер "
        "рассматриваемого заказа."
    )

    st.code(
        result["telegram_text"],
        language=None,
    )

    st.divider()
    st.subheader("⚠️ Недобор / перебор по артикулам")

    if not result["problem_aggregate_rows"]:
        st.success(
            "По выбранным заказам все сравниваемые артикулы совпали."
        )
    else:
        _render_aggregate_table(
            result["problem_aggregate_rows"]
        )

    st.divider()
    st.subheader("🔎 Детализация проблемных заказов")

    if not result["problem_rows"]:
        st.info(
            "Нет заказов с недобором или перебором."
        )
    else:
        _render_order_detail_table(
            result["problem_rows"]
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


def _order_label(
    order_id: str,
    order_by_id: dict[str, dict],
) -> str:
    """Формирует подпись заказа для мультивыбора."""
    row = order_by_id[order_id]

    return (
        f"{row['number']}  |  {row['moment_text']}  |  "
        f"{row['agent_name']}  |  {row['status_name']}"
    )


def render_mysklad_order_shipment_reconciliation_tab() -> None:
    """
    Отображает вкладку сверки заказов покупателей и отгрузок.

    Все обращения к МоемСкладу выполняются только через GET.
    Документы, статусы, контрагенты и позиции не изменяются.
    """
    st.title("⚖️ Сверка заказов и отгрузок FBO")

    st.markdown(
        """
        <div class="description-box">
            Выберите заказы покупателей по периоду, контрагенту и статусу.
            Приложение сравнит количество товаров в заказах с количеством
            в их связанных отгрузках и покажет недобор или перебор
            по артикулам.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Для включения отгрузки в расчёт должны совпасть: контрагент, "
        "статус заказа и статус отгрузки. Отгрузка также обязана "
        "быть указана в связанных документах заказа и обратной ссылкой "
        "подтверждать этот же заказ."
    )

    mysklad_token = _get_mysklad_token()

    if not mysklad_token:
        st.error(
            "Для сверки не настроен `MYSKLAD_API_TOKEN` "
            "в Streamlit Secrets."
        )
        return

    st.subheader("Фильтры заказов")

    today = datetime.now().date()

    date_col, load_col = st.columns([2, 1])

    with date_col:
        selected_date_range = st.date_input(
            "Период по дате заказа",
            value=(today, today),
            format="DD.MM.YYYY",
            key="reconciliation_date_range",
            help=(
                "Фильтруется `customerorder.moment` — дата документа "
                "заказа покупателя. Дата отгрузки не ограничивается: "
                "учитываются связанные отгрузки выбранных заказов."
            ),
        )

    with load_col:
        st.markdown("<br>", unsafe_allow_html=True)

        load_clicked = st.button(
            "⟳ Загрузить заказы и фильтры",
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

    current_catalog = st.session_state.get(
        "mysklad_reconciliation_catalog"
    )

    if (
        current_catalog is not None
        and current_catalog.get("signature") != catalog_signature
    ):
        st.session_state[
            "mysklad_reconciliation_catalog"
        ] = None
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None
        current_catalog = None

    if load_clicked:
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Получаю статусы, контрагентов и заказы "
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

            st.success(
                "Заказы и доступные фильтры успешно загружены."
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
            "«Загрузить заказы и фильтры»."
        )
        return

    catalog = saved_catalog["catalog"]

    agent_options = catalog["agent_options"]
    common_statuses = catalog["common_statuses"]
    order_rows = catalog["order_rows"]

    if not order_rows:
        st.warning(
            "За выбранный период не найдено заказов покупателей."
        )
        return

    if not agent_options:
        st.warning(
            "У заказов выбранного периода не найдены контрагенты."
        )
        return

    if not common_statuses:
        st.warning(
            "Не найдено общих статусов, одновременно доступных "
            "для заказов покупателей и отгрузок."
        )
        return

    agent_name_by_id = {
        item["id"]: item["name"]
        for item in agent_options
    }

    agent_ids = list(agent_name_by_id)

    default_agent_ids = agent_ids

    selected_agent_ids = st.multiselect(
        "Контрагенты заказов",
        options=agent_ids,
        default=default_agent_ids,
        format_func=lambda agent_id: agent_name_by_id[agent_id],
        key="reconciliation_agent_ids",
        placeholder="Выберите один или несколько контрагентов",
    )

    status_name_by_key = {
        item["key"]: item["name"]
        for item in common_statuses
    }

    status_keys = list(status_name_by_key)

    default_status_keys = [
        key
        for key, name in status_name_by_key.items()
        if _normal_status_name(name)
        == _normal_status_name(
            DEFAULT_RECONCILIATION_STATUS_NAME
        )
    ]

    if not default_status_keys:
        default_status_keys = status_keys

    selected_status_keys = st.multiselect(
        "Общие статусы заказа и отгрузки",
        options=status_keys,
        default=default_status_keys,
        format_func=lambda status_key: status_name_by_key[status_key],
        key="reconciliation_status_keys",
        placeholder="Выберите один или несколько статусов",
        help=(
            "Статус должен совпадать у заказа и его связанной "
            "отгрузки. По умолчанию выбран «Отгружен», если он "
            "существует у обоих типов документов."
        ),
    )

    selected_agent_set = set(selected_agent_ids)
    selected_status_set = set(selected_status_keys)

    visible_order_rows = [
        row
        for row in order_rows
        if (
            row["agent_id"] in selected_agent_set
            and row["status_key"] in selected_status_set
        )
    ]

    visible_order_rows.sort(
        key=lambda row: (
            row["moment_text"],
            row["number"].casefold(),
            row["number"],
        ),
        reverse=True,
    )

    st.caption(
        "Период заказов: "
        f"**{_format_date_range(date_from, date_to)}**. "
        f"Найдено заказов по текущим фильтрам: "
        f"**{len(visible_order_rows)}**."
    )

    if not selected_agent_ids or not selected_status_keys:
        st.warning(
            "Выберите хотя бы одного контрагента и хотя бы один статус."
        )
        return

    if not visible_order_rows:
        st.warning(
            "По выбранным контрагентам и статусам заказы не найдены."
        )
        return

    order_by_id = {
        row["id"]: row
        for row in visible_order_rows
    }

    visible_order_ids = list(order_by_id)

    widget_version_key = (
        "mysklad_reconciliation_order_widget_version"
    )

    if widget_version_key not in st.session_state:
        st.session_state[widget_version_key] = 0

    widget_key = (
        "reconciliation_selected_orders_"
        f"{st.session_state[widget_version_key]}"
    )

    saved_selected_order_ids = st.session_state.get(
        widget_key,
        [],
    )

    if not isinstance(saved_selected_order_ids, list):
        saved_selected_order_ids = []

    valid_saved_ids = [
        order_id
        for order_id in saved_selected_order_ids
        if order_id in order_by_id
    ]

    if not valid_saved_ids:
        valid_saved_ids = visible_order_ids

    st.divider()
    st.subheader("Выбор заказов для сверки")

    selected_order_ids = st.multiselect(
        "Выберите заказы",
        options=visible_order_ids,
        default=valid_saved_ids,
        format_func=lambda order_id: _order_label(
            order_id=order_id,
            order_by_id=order_by_id,
        ),
        key=widget_key,
        placeholder="Выберите один или несколько заказов",
    )

    clear_col, all_col, _ = st.columns([1, 1, 2])

    with clear_col:
        clear_clicked = st.button(
            "🧹 Очистить выбор",
            use_container_width=True,
            key="clear_reconciliation_orders",
        )

    with all_col:
        select_all_clicked = st.button(
            "☑️ Выбрать все",
            use_container_width=True,
            key="select_all_reconciliation_orders",
        )

    if clear_clicked:
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None
        st.session_state[widget_version_key] += 1
        st.rerun()

    if select_all_clicked:
        st.session_state[widget_key] = visible_order_ids
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None
        st.rerun()

    unique_selected_order_ids = list(
        dict.fromkeys(selected_order_ids)
    )

    if unique_selected_order_ids:
        st.success(
            f"Выбрано заказов для сверки: "
            f"{len(unique_selected_order_ids)}."
        )

    result_signature = (
        catalog_signature,
        tuple(sorted(selected_agent_ids)),
        tuple(sorted(selected_status_keys)),
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
                "Получаю связанные отгрузки, проверяю связи "
                "и суммирую товары по артикулам…"
            ):
                result = build_order_shipment_reconciliation(
                    client=client,
                    catalog=catalog,
                    selected_order_ids=set(
                        unique_selected_order_ids
                    ),
                    selected_status_keys=set(
                        selected_status_keys
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
            "Выберите контрагентов, статусы и заказы, затем нажмите "
            "«Сверить заказы и отгрузки»."
        )
        return

    _render_result(saved_result["result"])