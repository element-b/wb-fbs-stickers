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


def _value_to_date(value: object) -> date | None:
    """Преобразует значение Streamlit в объект `date`."""
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return None


def _parse_date_range(
    selected_value: object,
) -> tuple[date | None, date | None]:
    """Разбирает одну дату либо диапазон из `st.date_input`."""
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
    """Преобразует момент МоегоСклада в datetime."""
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
    """Форматирует дату документа для интерфейса."""
    parsed = _parse_moment(value)

    if parsed is None:
        return str(value or "Дата не определена").strip()

    return parsed.strftime("%d.%m.%Y %H:%M")


def _entity_id(value: object) -> str:
    """Извлекает ID сущности из `id` либо `meta.href`."""
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

    return href.rsplit("/", maxsplit=1)[-1] if href else ""


def _state_id_from_object(value: object) -> str:
    """Извлекает ID статуса."""
    return _entity_id(value)


def _normal_text(value: object) -> str:
    """Нормализует текст для сравнения."""
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

        if existing_name and existing_name != state_name:
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
    """Возвращает название статуса."""
    state = document.get("state")

    if not isinstance(state, dict):
        return ""

    embedded_name = str(state.get("name") or "").strip()

    if embedded_name:
        return embedded_name

    return state_names.get(
        _state_id_from_object(state),
        "",
    )


def _order_number(order: dict) -> str:
    """Возвращает номер заказа."""
    return str(order.get("name") or "").strip() or "Без номера"


def _demand_number(demand: dict) -> str:
    """Возвращает номер отгрузки."""
    return str(demand.get("name") or "").strip() or "Без номера"


def _linked_demand_ids(order: dict) -> list[str]:
    """Возвращает уникальные ID связанных отгрузок."""
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


def _position_quantity(
    value: object,
    document_number: str,
    document_label: str,
) -> int:
    """Проверяет и возвращает целое неотрицательное количество."""
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
    Возвращает артикул позиции.

    Для модификации используется её article. Если он отсутствует,
    допускается article развёрнутого основного товара.
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
        f"«{document_label}» `{document_number}` "
        "не заполнен артикул. Невозможно корректно сопоставить позиции."
    )


def _positions_by_article(
    client: MySkladClient,
    scope: FinishedProductsScope,
    positions: list[dict],
    document_number: str,
    document_label: str,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Суммирует только готовую продукцию по артикулу.

    Расходники и все позиции вне дерева «Готовая продукция» исключаются
    и учитываются только в технических безопасных счётчиках.
    """
    by_article: dict[str, int] = {}

    stats = {
        "source_positions": 0,
        "included_positions": 0,
        "excluded_positions": 0,
        "included_quantity": 0,
        "excluded_quantity": 0,
        "outside_finished_positions": 0,
        "without_folder_positions": 0,
        "unsupported_positions": 0,
    }

    for position in positions:
        if not isinstance(position, dict):
            raise MySkladApiError(
                "МойСклад вернул некорректную товарную позицию "
                f"в документе «{document_label}» `{document_number}`."
            )

        assortment = position.get("assortment")

        if not isinstance(assortment, dict):
            raise MySkladApiError(
                "МойСклад не вернул развёрнутую номенклатуру позиции "
                f"в документе «{document_label}» `{document_number}`."
            )

        meta = assortment.get("meta")

        if not isinstance(meta, dict):
            raise MySkladApiError(
                "МойСклад вернул номенклатуру без `meta` "
                f"в документе «{document_label}» `{document_number}`."
            )

        assortment_type = str(meta.get("type") or "").strip()

        quantity = _position_quantity(
            value=position.get("quantity"),
            document_number=document_number,
            document_label=document_label,
        )

        stats["source_positions"] += 1

        if assortment_type not in PHYSICAL_ASSORTMENT_TYPES:
            stats["excluded_positions"] += 1
            stats["excluded_quantity"] += quantity
            stats["unsupported_positions"] += 1
            continue

        folder_id = client.assortment_product_folder_id(
            assortment=assortment
        )

        if not folder_id:
            stats["excluded_positions"] += 1
            stats["excluded_quantity"] += quantity
            stats["without_folder_positions"] += 1
            continue

        if folder_id not in scope.included_folder_ids:
            stats["excluded_positions"] += 1
            stats["excluded_quantity"] += quantity
            stats["outside_finished_positions"] += 1
            continue

        article = _article_from_assortment(
            assortment=assortment,
            document_number=document_number,
            document_label=document_label,
        )

        by_article[article] = (
            by_article.get(article, 0) + quantity
        )

        stats["included_positions"] += 1
        stats["included_quantity"] += quantity

    return by_article, stats


def _order_label(
    order_id: str,
    order_by_id: dict[str, dict],
) -> str:
    """Формирует подпись заказа в списке."""
    order = order_by_id[order_id]

    return (
        f"{order['number']}  |  "
        f"{order['moment_text']}"
    )


def _manager_text(
    marketplace_name: str,
    result: dict[str, Any],
    order_ids: set[str],
) -> str:
    """
    Формирует текст для копирования менеджеру WB или Ozon.

    Содержит только готовую продукцию. Расходники и позиции вне
    «Готовой продукции» не включаются.
    """
    if not order_ids:
        return (
            f"{marketplace_name}: заказы не выбраны.\n"
            "Перед диагностикой распределите выбранные заказы "
            "между WB и Ozon."
        )

    summaries_by_id = {
        row["_order_id"]: row
        for row in result["order_summary_rows"]
    }

    demand_rows_by_order_id: dict[str, list[dict]] = defaultdict(list)

    for row in result["demand_diagnostic_rows"]:
        demand_rows_by_order_id[row["_order_id"]].append(row)

    detail_rows_by_order_id: dict[str, list[dict]] = defaultdict(list)

    for row in result["detail_rows"]:
        detail_rows_by_order_id[row["_order_id"]].append(row)

    selected_summaries = [
        summaries_by_id[order_id]
        for order_id in order_ids
        if order_id in summaries_by_id
    ]

    selected_summaries.sort(
        key=lambda row: (
            row["Заказ"].casefold(),
            row["Заказ"],
        )
    )

    lines = [
        f"{marketplace_name} — сверка заказов и отгрузок",
        (
            "Учитывается только товар из раздела "
            f"«{result['finished_products_folder_name']}»."
        ),
        (
            "Расходники, гофрокороба, этикет-лента и позиции вне "
            "готовой продукции исключены."
        ),
    ]

    for summary in selected_summaries:
        order_id = summary["_order_id"]
        order_number = summary["Заказ"]

        lines.append("")
        lines.append(
            f"Заказ: {order_number} "
            f"| Статус: {summary['Статус заказа']}"
        )

        demand_rows = demand_rows_by_order_id.get(
            order_id,
            [],
        )

        if demand_rows:
            demand_text = "; ".join(
                (
                    f"{row['Отгрузка']} "
                    f"({row['Статус отгрузки']})"
                )
                for row in demand_rows
            )

            lines.append(
                f"Связанные отгрузки: {demand_text}"
            )
        else:
            lines.append("Связанные отгрузки: не найдены")

        detail_rows = detail_rows_by_order_id.get(
            order_id,
            [],
        )

        if not detail_rows:
            lines.append(
                "Готовая продукция: товарные строки не найдены."
            )
            continue

        for row in detail_rows:
            deviation = row["Отклонение"]

            if deviation < 0:
                result_text = f"недобор {abs(deviation)}"
            elif deviation > 0:
                result_text = f"перебор {deviation}"
            else:
                result_text = "совпадает"

            lines.append(
                "• "
                f"{row['Артикул']} — "
                f"заказано {row['Заказано']}, "
                f"отгружено {row['В связанных отгрузках']}, "
                f"{result_text}."
            )

    return "\n".join(lines)


def build_reconciliation_catalog(
    client: MySkladClient,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    """Загружает список заказов покупателей по `moment`."""
    orders = client.customer_orders_by_moment(
        date_from=date_from,
        date_to=date_to,
    )

    rows: list[dict] = []
    seen_ids: set[str] = set()

    for order in orders:
        if not isinstance(order, dict):
            continue

        order_id = str(order.get("id") or "").strip()

        if not order_id:
            raise MySkladApiError(
                "МойСклад вернул заказ покупателя без корректного ID."
            )

        if order_id in seen_ids:
            raise MySkladApiError(
                "МойСклад вернул заказ повторно. "
                "Список не сформирован, чтобы исключить двойной учёт."
            )

        seen_ids.add(order_id)

        rows.append(
            {
                "id": order_id,
                "number": _order_number(order),
                "moment_text": _format_moment(
                    order.get("moment")
                ),
            }
        )

    rows.sort(
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
        "order_rows": rows,
    }


def build_order_shipment_reconciliation(
    client: MySkladClient,
    catalog: dict[str, Any],
    selected_order_ids: set[str],
    wb_order_ids: set[str],
    ozon_order_ids: set[str],
) -> dict[str, Any]:
    """
    Сверяет готовую продукцию в заказах и связанных отгрузках.

    В расчёте участвуют только позиции из товарной группы
    «Готовая продукция» и всех её дочерних групп.
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

    normalized_wb_ids = {
        str(order_id).strip()
        for order_id in wb_order_ids
        if str(order_id).strip()
    }

    normalized_ozon_ids = {
        str(order_id).strip()
        for order_id in ozon_order_ids
        if str(order_id).strip()
    }

    overlap_ids = normalized_wb_ids & normalized_ozon_ids

    if overlap_ids:
        raise ValueError(
            "Один заказ нельзя одновременно отнести к WB и Ozon. "
            "Исправьте распределение заказов перед диагностикой."
        )

    assigned_not_selected = (
        normalized_wb_ids
        | normalized_ozon_ids
    ) - normalized_selected_ids

    if assigned_not_selected:
        raise ValueError(
            "В WB или Ozon отмечен заказ, который не выбран "
            "для диагностики."
        )

    catalog_by_id = {
        str(row.get("id") or "").strip(): row
        for row in order_rows
        if isinstance(row, dict)
    }

    unknown_ids = normalized_selected_ids - set(catalog_by_id)

    if unknown_ids:
        raise ValueError(
            "Один или несколько выбранных заказов отсутствуют "
            "в текущем списке периода. Загрузите список заново."
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

    total_linked_demands = 0
    orders_without_demands_count = 0
    total_status_mismatches = 0
    total_finished_order_quantity = 0
    total_finished_demand_quantity = 0
    total_excluded_positions = 0

    for catalog_row in selected_catalog_rows:
        order_id = catalog_row["id"]

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
        ) or "Без статуса"

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

        linked_demand_ids = _linked_demand_ids(order)

        if not linked_demand_ids:
            orders_without_demands_count += 1

        shipped_by_article: dict[str, int] = {}
        demand_numbers: list[str] = []
        order_demand_quantity = 0
        order_excluded_positions = order_stats[
            "excluded_positions"
        ]

        for demand_id in linked_demand_ids:
            total_linked_demands += 1

            demand = client.demand_by_id(demand_id)

            if str(demand.get("id") or "").strip() != demand_id:
                raise MySkladApiError(
                    "МойСклад вернул отгрузку с ID, не совпадающим "
                    "с запрошенным."
                )

            demand_number = _demand_number(demand)

            demand_status = _status_name(
                document=demand,
                state_names=demand_state_names,
            ) or "Без статуса"

            statuses_match = (
                _normal_text(order_status)
                == _normal_text(demand_status)
            )

            if not statuses_match:
                total_status_mismatches += 1

            reverse_order_id = _entity_id(
                demand.get("customerOrder")
            )

            if not reverse_order_id:
                link_check = (
                    "Только в связанных документах заказа"
                )
            elif reverse_order_id == order_id:
                link_check = (
                    "Подтверждена в заказе и отгрузке"
                )
            else:
                link_check = (
                    "Конфликт: отгрузка ссылается на другой заказ"
                )

            demand_positions = client.demand_positions(
                demand_id=demand_id,
                expand_assortment=True,
            )

            demand_by_article, demand_stats = _positions_by_article(
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

            demand_numbers.append(demand_number)
            order_demand_quantity += demand_stats[
                "included_quantity"
            ]

            order_excluded_positions += demand_stats[
                "excluded_positions"
            ]

            demand_diagnostic_rows.append(
                {
                    "_order_id": order_id,
                    "Заказ": order_number,
                    "Статус заказа": order_status,
                    "Отгрузка": demand_number,
                    "Статус отгрузки": demand_status,
                    "Статус совпадает": (
                        "Да" if statuses_match else "Нет"
                    ),
                    "Проверка связи": link_check,
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

        article_keys = sorted(
            set(ordered_by_article) | set(shipped_by_article),
            key=lambda value: (value.casefold(), value),
        )

        demands_text = (
            ", ".join(demand_numbers)
            if demand_numbers
            else "Нет связанных отгрузок"
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
                "_order_id": order_id,
                "Заказ": order_number,
                "Статус заказа": order_status,
                "Связанные отгрузки": demands_text,
                "Артикул": article,
                "Заказано": ordered_quantity,
                "В связанных отгрузках": shipped_quantity,
                "Отклонение": deviation,
                "Результат": result_text,
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
            aggregate_row["Отгрузок"].update(demand_numbers)

        total_finished_order_quantity += order_stats[
            "included_quantity"
        ]
        total_finished_demand_quantity += order_demand_quantity
        total_excluded_positions += order_excluded_positions

        order_summary_rows.append(
            {
                "_order_id": order_id,
                "Заказ": order_number,
                "Статус заказа": order_status,
                "Позиций готовой продукции в заказе": (
                    order_stats["included_positions"]
                ),
                "Количество готовой продукции в заказе": (
                    order_stats["included_quantity"]
                ),
                "Связанных отгрузок": len(linked_demand_ids),
                "Количество готовой продукции в отгрузках": (
                    order_demand_quantity
                ),
                "Исключено позиций": order_excluded_positions,
            }
        )

    aggregate_rows: list[dict] = []

    for row in aggregate_by_article.values():
        deviation = (
            row["В связанных отгрузках"]
            - row["В заказах"]
        )

        aggregate_rows.append(
            {
                "Артикул": row["Артикул"],
                "В заказах": row["В заказах"],
                "В связанных отгрузках": (
                    row["В связанных отгрузках"]
                ),
                "Отклонение": deviation,
                "Результат": (
                    "Недобор"
                    if deviation < 0
                    else "Перебор"
                    if deviation > 0
                    else "Совпадает"
                ),
                "Заказов": len(row["Заказов"]),
                "Отгрузок": len(row["Отгрузок"]),
            }
        )

    def detail_sort_key(row: dict) -> tuple:
        return (
            row["Заказ"].casefold(),
            row["Заказ"],
            0 if row["Отклонение"] < 0 else 1,
            -abs(row["Отклонение"]),
            row["Артикул"].casefold(),
            row["Артикул"],
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

    detail_rows.sort(key=detail_sort_key)
    problem_rows.sort(key=detail_sort_key)

    aggregate_rows.sort(
        key=lambda row: (
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
        "selected_order_count": len(normalized_selected_ids),
        "total_linked_demands": total_linked_demands,
        "orders_without_demands_count": (
            orders_without_demands_count
        ),
        "status_mismatches_count": total_status_mismatches,
        "total_finished_order_quantity": (
            total_finished_order_quantity
        ),
        "total_finished_demand_quantity": (
            total_finished_demand_quantity
        ),
        "total_excluded_positions": total_excluded_positions,
        "total_shortage": total_shortage,
        "total_overage": total_overage,
        "order_summary_rows": order_summary_rows,
        "demand_diagnostic_rows": demand_diagnostic_rows,
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
        marketplace_name="WB",
        result=result,
        order_ids=normalized_wb_ids,
    )

    result["ozon_manager_text"] = _manager_text(
        marketplace_name="Ozon",
        result=result,
        order_ids=normalized_ozon_ids,
    )

    return result


def _render_order_summary_table(rows: list[dict]) -> None:
    """Показывает сводку выбранных заказов."""
    columns = [
        "Заказ",
        "Статус заказа",
        "Позиций готовой продукции в заказе",
        "Количество готовой продукции в заказе",
        "Связанных отгрузок",
        "Количество готовой продукции в отгрузках",
        "Исключено позиций",
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_demand_table(rows: list[dict]) -> None:
    """Показывает диагностику связанных отгрузок."""
    columns = [
        "Заказ",
        "Статус заказа",
        "Отгрузка",
        "Статус отгрузки",
        "Статус совпадает",
        "Проверка связи",
        "Позиций готовой продукции",
        "Исключено позиций",
        "Количество готовой продукции",
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_detail_table(rows: list[dict]) -> None:
    """Показывает построчное сравнение артикулов."""
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

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_aggregate_table(rows: list[dict]) -> None:
    """Показывает агрегацию по артикулам."""
    columns = [
        "Артикул",
        "В заказах",
        "В связанных отгрузках",
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


def _render_result(result: dict[str, Any]) -> None:
    """Отображает результат диагностики."""
    st.divider()
    st.subheader("🧪 Сводка по готовой продукции")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric(
        "Выбрано заказов",
        result["selected_order_count"],
    )

    metric_2.metric(
        "Связанных отгрузок",
        result["total_linked_demands"],
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
        "Недобор",
        result["total_shortage"],
    )

    metric_6.metric(
        "Перебор",
        result["total_overage"],
    )

    metric_7.metric(
        "Заказов без отгрузок",
        result["orders_without_demands_count"],
    )

    metric_8.metric(
        "Исключено позиций",
        result["total_excluded_positions"],
    )

    st.success(
        "Сверка выполнена только по товарам из раздела "
        f"«{result['finished_products_folder_name']}» "
        "и его вложенных групп. Расходники не участвуют."
    )

    st.caption(
        "В дереве готовой продукции: "
        f"**{result['finished_products_folder_count']}** товарных групп."
    )

    if result["status_mismatches_count"] > 0:
        st.warning(
            "Найдены отгрузки с несовпадающими статусами заказа "
            "и отгрузки: "
            f"**{result['status_mismatches_count']}**. "
            "Они показаны в диагностике и не исключены автоматически."
        )

    st.divider()
    st.subheader("📦 Заказы и связанные отгрузки")

    _render_order_summary_table(
        result["order_summary_rows"]
    )

    st.divider()
    st.subheader("🔗 Связанные отгрузки")

    if result["demand_diagnostic_rows"]:
        _render_demand_table(
            result["demand_diagnostic_rows"]
        )
    else:
        st.info("У выбранных заказов нет связанных отгрузок.")

    st.divider()
    st.subheader("📋 Данные для менеджеров")

    st.caption(
        "Тексты можно выделить и скопировать. Они содержат только "
        "номера заказов, связанные отгрузки, артикулы и количества "
        "готовой продукции."
    )

    wb_col, ozon_col = st.columns(2)

    with wb_col:
        st.text_area(
            "WB — текст для отправки",
            value=result["wb_manager_text"],
            height=420,
            key="mysklad_reconciliation_wb_manager_text",
        )

    with ozon_col:
        st.text_area(
            "Ozon — текст для отправки",
            value=result["ozon_manager_text"],
            height=420,
            key="mysklad_reconciliation_ozon_manager_text",
        )

    st.divider()
    st.subheader("⚠️ Отклонения по заказам")

    if result["problem_rows"]:
        _render_detail_table(result["problem_rows"])
    else:
        st.success(
            "По выбранным заказам количество готовой продукции "
            "совпало с количеством в связанных отгрузках."
        )

    st.divider()
    st.subheader("📌 Итог по артикулам")

    if result["problem_aggregate_rows"]:
        _render_aggregate_table(
            result["problem_aggregate_rows"]
        )
    else:
        st.info("Нет артикулов с отклонениями.")

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
        if result["detail_rows"]:
            _render_detail_table(result["detail_rows"])
        else:
            st.info(
                "В выбранных заказах и отгрузках не найдено "
                "позиций готовой продукции."
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
    """Отображает диагностику FBO-заказов и связанных отгрузок."""
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
            Выберите заказы покупателей и сравните их с отгрузками
            из блока связанных документов. В сверку попадает только
            <b>готовая продукция</b>; расходники исключаются.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Все запросы выполняются только методом GET. Приложение "
        "не меняет заказы, отгрузки, статусы, позиции и связи."
    )

    mysklad_token = _get_mysklad_token()

    if not mysklad_token:
        st.error(
            "Для диагностики не настроен `MYSKLAD_API_TOKEN`."
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
                "Дата связанных отгрузок не ограничивается, "
                "чтобы увидеть все документы из связи заказа."
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
        st.warning("Выберите одну дату или диапазон дат.")
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
                "Получаю список заказов покупателей…"
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

            st.success("Список заказов успешно загружен.")

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

    order_by_id = {
        row["id"]: row
        for row in order_rows
    }

    order_ids = list(order_by_id)

    widget_version = st.session_state[
        "mysklad_reconciliation_order_widget_version"
    ]

    selected_widget_key = (
        "reconciliation_selected_orders_"
        f"{widget_version}"
    )

    st.divider()
    st.subheader("Выбор заказов")

    selected_order_ids = st.multiselect(
        "Заказы для диагностики",
        options=order_ids,
        format_func=lambda order_id: _order_label(
            order_id=order_id,
            order_by_id=order_by_id,
        ),
        key=selected_widget_key,
        placeholder="Введите номер заказа и выберите его",
    )

    unique_selected_ids = list(
        dict.fromkeys(selected_order_ids)
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

    if not unique_selected_ids:
        st.info(
            "Выберите один или несколько заказов для диагностики."
        )
        return

    st.divider()
    st.subheader("Распределение для сообщений менеджерам")

    st.caption(
        "Распределите выбранные заказы вручную. Это исключает "
        "небезопасное угадывание маркетплейса по названию контрагента."
    )

    wb_widget_key = (
        "reconciliation_wb_orders_"
        f"{widget_version}"
    )

    ozon_widget_key = (
        "reconciliation_ozon_orders_"
        f"{widget_version}"
    )

    channel_col_1, channel_col_2 = st.columns(2)

    with channel_col_1:
        wb_order_ids = st.multiselect(
            "Заказы для менеджера WB",
            options=unique_selected_ids,
            format_func=lambda order_id: _order_label(
                order_id=order_id,
                order_by_id=order_by_id,
            ),
            key=wb_widget_key,
            placeholder="Выберите заказы WB",
        )

    with channel_col_2:
        ozon_order_ids = st.multiselect(
            "Заказы для менеджера Ozon",
            options=unique_selected_ids,
            format_func=lambda order_id: _order_label(
                order_id=order_id,
                order_by_id=order_by_id,
            ),
            key=ozon_widget_key,
            placeholder="Выберите заказы Ozon",
        )

    duplicate_channel_ids = set(wb_order_ids) & set(ozon_order_ids)

    if duplicate_channel_ids:
        st.error(
            "Один заказ выбран одновременно для WB и Ozon. "
            "Снимите повторный выбор перед запуском диагностики."
        )

    result_signature = (
        catalog_signature,
        tuple(sorted(unique_selected_ids)),
        tuple(sorted(set(wb_order_ids))),
        tuple(sorted(set(ozon_order_ids))),
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

    diagnose_clicked = st.button(
        "🧪 Диагностировать заказы и отгрузки",
        type="primary",
        use_container_width=True,
        key="run_order_shipment_reconciliation",
        disabled=bool(duplicate_channel_ids),
    )

    if diagnose_clicked:
        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Получаю товарные группы, готовую продукцию заказов "
                "и связанных отгрузок…"
            ):
                result = build_order_shipment_reconciliation(
                    client=client,
                    catalog=catalog,
                    selected_order_ids=set(unique_selected_ids),
                    wb_order_ids=set(wb_order_ids),
                    ozon_order_ids=set(ozon_order_ids),
                )

            st.session_state[
                "mysklad_reconciliation_result"
            ] = {
                "signature": result_signature,
                "result": result,
            }

            st.success(
                "Диагностика готовой продукции завершена."
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
            "Распределите заказы между WB и Ozon при необходимости, "
            "затем запустите диагностику."
        )
        return

    _render_result(saved_result["result"])