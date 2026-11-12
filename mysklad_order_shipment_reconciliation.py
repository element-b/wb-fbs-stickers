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
    """Форматирует дату документа."""
    parsed = _parse_moment(value)

    if parsed is None:
        return str(value or "Дата не определена").strip()

    return parsed.strftime("%d.%m.%Y %H:%M")


def _entity_id(value: object) -> str:
    """Извлекает UUID сущности из id либо meta.href."""
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


def _state_id_from_object(value: object) -> str:
    """Извлекает UUID статуса."""
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
    """Строит соответствие ID статуса и отображаемого названия."""
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
    """Возвращает название статуса документа."""
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


def _counterparty_name(order: dict) -> str:
    """
    Возвращает название контрагента заказа.

    В интерфейс попадает только контрагент FBO WB или FBO Ozon.
    Данные покупателей, адреса, телефоны и финансовые данные
    не используются.
    """
    agent = order.get("agent")

    if not isinstance(agent, dict):
        return ""

    return str(agent.get("name") or "").strip()


def _marketplace_from_counterparty_name(
    counterparty_name: str,
) -> str | None:
    """
    Определяет маркетплейс по названию контрагента заказа.

    Важное правило: для безопасного попадания в отчёт должны
    одновременно присутствовать:

    - маркер FBO: `fbo` или `фбо`;
    - маркер конкретного маркетплейса.

    Поддерживаются варианты:

    - FBO Ozon;
    - OZON FBO;
    - ФБО Озон;
    - FBO WB;
    - WB FBO;
    - ФБО WB;
    - ФБО Wildberries;
    - ФБО Вайлдберриз.

    Если контрагент не определён однозначно, такая отгрузка
    не включается в список выбора. Это безопаснее, чем отнести
    FBS, внутреннюю отгрузку или печатного партнёра к маркетплейсу.
    """
    normalized = _normal_text(counterparty_name)

    if not normalized:
        return None

    has_fbo = (
        "fbo" in normalized
        or "фбо" in normalized
    )

    if not has_fbo:
        return None

    has_ozon = (
        "ozon" in normalized
        or "озон" in normalized
    )

    has_wb = (
        "wildberries" in normalized
        or "вайлдберриз" in normalized
        or "вб" in normalized
        or " wb " in f" {normalized} "
    )

    if has_ozon and has_wb:
        return None

    if has_ozon:
        return MARKETPLACE_OZON

    if has_wb:
        return MARKETPLACE_WB

    return None


def _linked_demand_ids(order: dict) -> list[str]:
    """
    Возвращает уникальные ID отгрузок из customerorder.demands.

    Список нужен только для диагностики: приложение показывает,
    есть ли у заказа дополнительные связанные отгрузки, которые
    не вошли в выбранный период.
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

    for reference in raw_demands:
        demand_id = _entity_id(reference)

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


def _quantity_as_int(
    value: object,
    document_number: str,
    document_label: str,
) -> int:
    """
    Возвращает целое неотрицательное количество готовой продукции.

    Дробные количества не поддерживаются: диагностика предназначена
    для штучной готовой продукции.
    """
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
    Возвращает article товара либо модификации.

    Для варианта `variant` используется его собственный артикул.
    Если он отсутствует, допускается артикул развёрнутого основного
    товара. Если артикул нельзя получить однозначно — диагностика
    останавливается.
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
        "не заполнен артикул. Невозможно корректно сопоставить "
        "товарные позиции."
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

    Включаются только:

    - `product`;
    - `variant`;

    и только при принадлежности к разделу «Готовая продукция»
    либо к его дочернему разделу.

    Расходники, этикет-лента, гофрокороба, услуги, комплекты,
    товары без группы и позиции из других разделов исключаются.
    """
    by_article: dict[str, int] = {}

    stats = {
        "source_positions": 0,
        "included_positions": 0,
        "excluded_positions": 0,
        "included_quantity": 0,
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

        stats["source_positions"] += 1

        if assortment_type not in PHYSICAL_ASSORTMENT_TYPES:
            stats["excluded_positions"] += 1
            stats["unsupported_positions"] += 1
            continue

        folder_id = client.assortment_product_folder_id(
            assortment=assortment
        )

        if not folder_id:
            stats["excluded_positions"] += 1
            stats["without_folder_positions"] += 1
            continue

        if folder_id not in scope.included_folder_ids:
            stats["excluded_positions"] += 1
            stats["outside_finished_positions"] += 1
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


def _catalog_demand_label(
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


def _marketplace_order_rows(
    result: dict[str, Any],
    marketplace: str,
) -> list[dict]:
    """Возвращает строки заказов одного маркетплейса."""
    return [
        row
        for row in result["order_summary_rows"]
        if row["_marketplace"] == marketplace
    ]


def _marketplace_detail_rows(
    result: dict[str, Any],
    marketplace: str,
) -> list[dict]:
    """Возвращает товарные строки заказов одного маркетплейса."""
    return [
        row
        for row in result["detail_rows"]
        if row["_marketplace"] == marketplace
    ]


def _manager_text(
    marketplace: str,
    result: dict[str, Any],
) -> str:
    """
    Формирует текст для менеджера WB либо Ozon.

    Структура:

    1. Данные по каждому отдельному заказу.
    2. Номера отгрузок, выбранных по периоду.
    3. Количество готовой продукции по каждому артикулу.
    4. Итоговая сводка по всем выбранным заказам маркетплейса.

    В текст намеренно не добавляются адреса, телефоны, ФИО,
    финансовые данные, API-данные и расходники.
    """
    order_rows = _marketplace_order_rows(
        result=result,
        marketplace=marketplace,
    )

    detail_rows = _marketplace_detail_rows(
        result=result,
        marketplace=marketplace,
    )

    if not order_rows:
        return (
            f"{marketplace}\n"
            "За выбранный период нет отгрузок, связанных с заказами "
            "этого маркетплейса."
        )

    details_by_order_id: dict[str, list[dict]] = defaultdict(list)

    for row in detail_rows:
        details_by_order_id[row["_order_id"]].append(row)

    lines = [
        f"{marketplace} — сверка заказов и отгрузок",
        (
            "В расчёте учитывается только готовая продукция из раздела "
            f"«{result['finished_products_folder_name']}»."
        ),
        (
            "Расходники, этикет-лента, гофрокороба и товары вне "
            "готовой продукции исключены."
        ),
        "",
        "Детализация по заказам:",
    ]

    for order_row in order_rows:
        order_id = order_row["_order_id"]

        lines.append("")
        lines.append(
            f"Заказ: {order_row['Заказ']} "
            f"| Статус: {order_row['Статус заказа']}"
        )

        lines.append(
            "Выбранные отгрузки: "
            f"{order_row['Выбранные отгрузки']}"
        )

        if (
            order_row[
                "Есть невыбранные связанные отгрузки"
            ]
            == "Да"
        ):
            lines.append(
                "Внимание: у заказа есть связанные отгрузки вне "
                "выбранного периода. В сравнении учитываются только "
                "отгрузки, выбранные по текущему фильтру дат."
            )

        order_details = sorted(
            details_by_order_id.get(order_id, []),
            key=lambda row: (
                row["Артикул"].casefold(),
                row["Артикул"],
            ),
        )

        if not order_details:
            lines.append(
                "Готовая продукция: позиции не найдены."
            )
            continue

        for row in order_details:
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

    for row in detail_rows:
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
        aggregate_row = aggregate[article]

        ordered_quantity = aggregate_row["ordered"]
        shipped_quantity = aggregate_row["shipped"]

        deviation = shipped_quantity - ordered_quantity

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
            f"заказано {ordered_quantity}, "
            f"отгружено {shipped_quantity}, "
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
    Загружает каталог отгрузок по `demand.moment`.

    Логика:

    1. В период попадают именно отгрузки.
    2. Для каждой отгрузки определяется связанный заказ через
       `demand.customerOrder`.
    3. У заказа определяется контрагент.
    4. В список выбора попадают только отгрузки, связанные
       с FBO Ozon или FBO WB.

    Внутренние отгрузки, отгрузки печатному партнёру, FBS,
    неидентифицированные документы и отгрузки без связанного заказа
    не включаются в пользовательский выбор.
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
    non_fbo_or_unknown_count = 0

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
                "МойСклад вернул одну и ту же отгрузку повторно. "
                "Список не сформирован, чтобы исключить двойной учёт."
            )

        seen_demand_ids.add(demand_id)
        total_demands_in_period += 1

        demand = client.demand_by_id(demand_id)

        actual_demand_id = str(
            demand.get("id") or ""
        ).strip()

        if actual_demand_id != demand_id:
            raise MySkladApiError(
                "МойСклад вернул отгрузку с ID, не совпадающим "
                "с запрошенным."
            )

        customer_order_id = _entity_id(
            demand.get("customerOrder")
        )

        if not customer_order_id:
            without_customer_order_count += 1
            continue

        order = order_cache.get(customer_order_id)

        if order is None:
            order = client.customer_order_by_id(
                customer_order_id=customer_order_id
            )

            actual_order_id = str(
                order.get("id") or ""
            ).strip()

            if actual_order_id != customer_order_id:
                raise MySkladApiError(
                    "МойСклад вернул заказ с ID, не совпадающим "
                    "с запрошенным."
                )

            order_cache[customer_order_id] = order

        counterparty_name = _counterparty_name(order)

        marketplace = _marketplace_from_counterparty_name(
            counterparty_name=counterparty_name
        )

        if marketplace is None:
            non_fbo_or_unknown_count += 1
            continue

        rows.append(
            {
                "demand_id": demand_id,
                "order_id": customer_order_id,
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
            row["demand_id"],
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
        "non_fbo_or_unknown_count": (
            non_fbo_or_unknown_count
        ),
    }


def build_order_shipment_reconciliation(
    client: MySkladClient,
    catalog: dict[str, Any],
    selected_demand_ids: set[str],
) -> dict[str, Any]:
    """
    Сравнивает готовую продукцию заказов и выбранных отгрузок.

    Сравнение выполняется так:

    ```text
    Количество в выбранных отгрузках
    − Количество в связанном заказе
    = Отклонение
    ```

    Важное ограничение:

    - учитываются только выбранные отгрузки, попавшие в фильтр периода;
    - если у заказа есть дополнительные связанные отгрузки вне периода,
      они не прибавляются к количеству, но показываются как предупреждение;
    - это позволяет не смешивать разные периоды фактической отгрузки.
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
            catalog_by_demand_id[demand_id]
            for demand_id in normalized_selected_ids
        ),
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
        ),
    )

    selected_demand_ids_by_order_id: dict[
        str,
        list[str],
    ] = defaultdict(list)

    for row in selected_catalog_rows:
        selected_demand_ids_by_order_id[
            row["order_id"]
        ].append(row["demand_id"])

    order_catalog_row_by_id: dict[str, dict] = {}

    for row in selected_catalog_rows:
        order_id = row["order_id"]

        existing = order_catalog_row_by_id.get(order_id)

        if existing is None:
            order_catalog_row_by_id[order_id] = row
            continue

        if existing["marketplace"] != row["marketplace"]:
            raise MySkladApiError(
                "Один заказ связан с отгрузками, отнесёнными "
                "к разным маркетплейсам. Диагностика остановлена."
            )

    aggregate_by_marketplace_and_article: dict[
        tuple[str, str],
        dict[str, Any],
    ] = {}

    detail_rows: list[dict] = []
    problem_rows: list[dict] = []
    demand_diagnostic_rows: list[dict] = []
    order_summary_rows: list[dict] = []

    total_status_mismatches = 0
    total_excluded_positions = 0
    total_finished_order_quantity = 0
    total_finished_demand_quantity = 0
    total_additional_linked_demands = 0

    for order_id, selected_ids_for_order in sorted(
        selected_demand_ids_by_order_id.items(),
        key=lambda item: (
            order_catalog_row_by_id[item[0]][
                "marketplace"
            ],
            order_catalog_row_by_id[item[0]][
                "counterparty_name"
            ].casefold(),
            order_catalog_row_by_id[item[0]][
                "order_number"
            ].casefold(),
            order_catalog_row_by_id[item[0]][
                "order_number"
            ],
        ),
    ):
        catalog_row = order_catalog_row_by_id[order_id]

        marketplace = catalog_row["marketplace"]

        order = client.customer_order_by_id(
            customer_order_id=order_id
        )

        actual_order_id = str(order.get("id") or "").strip()

        if actual_order_id != order_id:
            raise MySkladApiError(
                "МойСклад вернул заказ с ID, не совпадающим "
                "с запрошенным."
            )

        current_counterparty_name = _counterparty_name(order)

        current_marketplace = _marketplace_from_counterparty_name(
            counterparty_name=current_counterparty_name
        )

        if current_marketplace != marketplace:
            raise MySkladApiError(
                "Контрагент выбранного заказа был изменён или не может "
                "быть однозначно отнесён к FBO WB / FBO Ozon. "
                "Обновите список отгрузок и повторите диагностику."
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

        (
            ordered_by_article,
            order_stats,
        ) = _positions_by_article(
            client=client,
            scope=scope,
            positions=order_positions,
            document_number=order_number,
            document_label="Заказ покупателя",
        )

        all_linked_demand_ids = _linked_demand_ids(order)

        selected_demand_id_set = set(selected_ids_for_order)

        additional_demand_ids = (
            set(all_linked_demand_ids)
            - selected_demand_id_set
        )

        total_additional_linked_demands += len(
            additional_demand_ids
        )

        shipped_by_article: dict[str, int] = {}
        selected_demand_numbers: list[str] = []

        order_demand_quantity = 0
        order_excluded_positions = order_stats[
            "excluded_positions"
        ]

        for demand_id in sorted(selected_demand_id_set):
            demand = client.demand_by_id(demand_id)

            actual_demand_id = str(
                demand.get("id") or ""
            ).strip()

            if actual_demand_id != demand_id:
                raise MySkladApiError(
                    "МойСклад вернул отгрузку с ID, не совпадающим "
                    "с запрошенным."
                )

            reverse_order_id = _entity_id(
                demand.get("customerOrder")
            )

            if reverse_order_id != order_id:
                raise MySkladApiError(
                    "Выбранная отгрузка больше не связана с ожидаемым "
                    "заказом. Обновите список и повторите диагностику."
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

            order_demand_quantity += demand_stats[
                "included_quantity"
            ]

            order_excluded_positions += demand_stats[
                "excluded_positions"
            ]

            selected_demand_numbers.append(demand_number)

            demand_diagnostic_rows.append(
                {
                    "_order_id": order_id,
                    "_marketplace": marketplace,
                    "Маркетплейс": marketplace,
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

        article_keys = sorted(
            set(ordered_by_article)
            | set(shipped_by_article),
            key=lambda value: (
                value.casefold(),
                value,
            ),
        )

        selected_demands_text = (
            ", ".join(
                sorted(
                    selected_demand_numbers,
                    key=lambda value: (
                        value.casefold(),
                        value,
                    ),
                )
            )
            if selected_demand_numbers
            else "Нет выбранных отгрузок"
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
                "_marketplace": marketplace,
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

            aggregate_row = (
                aggregate_by_marketplace_and_article
                .setdefault(
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
            )

            aggregate_row["В заказах"] += ordered_quantity
            aggregate_row[
                "В выбранных отгрузках"
            ] += shipped_quantity

            aggregate_row["Заказов"].add(order_number)

            for demand_number in selected_demand_numbers:
                aggregate_row["Отгрузок"].add(demand_number)

        total_finished_order_quantity += order_stats[
            "included_quantity"
        ]

        total_finished_demand_quantity += order_demand_quantity

        total_excluded_positions += order_excluded_positions

        order_summary_rows.append(
            {
                "_order_id": order_id,
                "_marketplace": marketplace,
                "Маркетплейс": marketplace,
                "Заказ": order_number,
                "Статус заказа": order_status,
                "Позиций готовой продукции в заказе": (
                    order_stats["included_positions"]
                ),
                "Количество готовой продукции в заказе": (
                    order_stats["included_quantity"]
                ),
                "Выбранных отгрузок": len(
                    selected_demand_id_set
                ),
                "Выбранные отгрузки": selected_demands_text,
                "Количество готовой продукции в отгрузках": (
                    order_demand_quantity
                ),
                "Есть невыбранные связанные отгрузки": (
                    "Да" if additional_demand_ids else "Нет"
                ),
                "Исключено позиций": order_excluded_positions,
            }
        )

    aggregate_rows: list[dict] = []

    for aggregate_row in (
        aggregate_by_marketplace_and_article.values()
    ):
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

    def _row_sort_key(row: dict) -> tuple:
        return (
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

    order_summary_rows.sort(
        key=lambda row: (
            MARKETPLACE_SORT_ORDER.get(
                row["_marketplace"],
                99,
            ),
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
            row["Заказ"].casefold(),
            row["Заказ"],
            row["Отгрузка"].casefold(),
            row["Отгрузка"],
        )
    )

    detail_rows.sort(key=_row_sort_key)
    problem_rows.sort(key=_row_sort_key)

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
            selected_demand_ids_by_order_id
        ),
        "total_status_mismatches": (
            total_status_mismatches
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
        "total_additional_linked_demands": (
            total_additional_linked_demands
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
    """Показывает сводку по связанным заказам."""
    columns = [
        "Маркетплейс",
        "Заказ",
        "Статус заказа",
        "Позиций готовой продукции в заказе",
        "Количество готовой продукции в заказе",
        "Выбранных отгрузок",
        "Выбранные отгрузки",
        "Количество готовой продукции в отгрузках",
        "Есть невыбранные связанные отгрузки",
        "Исключено позиций",
    ]

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Маркетплейс": st.column_config.TextColumn(
                "Маркетплейс",
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
            "Позиций готовой продукции в заказе": (
                st.column_config.NumberColumn(
                    "Позиций готовой продукции в заказе",
                    format="%d",
                )
            ),
            "Количество готовой продукции в заказе": (
                st.column_config.NumberColumn(
                    "Количество готовой продукции в заказе",
                    format="%d",
                )
            ),
            "Выбранных отгрузок": st.column_config.NumberColumn(
                "Выбранных отгрузок",
                format="%d",
            ),
            "Выбранные отгрузки": st.column_config.TextColumn(
                "Выбранные отгрузки",
                width="large",
            ),
            "Количество готовой продукции в отгрузках": (
                st.column_config.NumberColumn(
                    "Количество готовой продукции в отгрузках",
                    format="%d",
                )
            ),
            "Есть невыбранные связанные отгрузки": (
                st.column_config.TextColumn(
                    "Есть невыбранные связанные отгрузки",
                    width="medium",
                )
            ),
            "Исключено позиций": st.column_config.NumberColumn(
                "Исключено позиций",
                format="%d",
            ),
        },
    )


def _render_demand_diagnostic_table(
    rows: list[dict],
) -> None:
    """Показывает выбранные отгрузки."""
    columns = [
        "Маркетплейс",
        "Заказ",
        "Статус заказа",
        "Отгрузка",
        "Статус отгрузки",
        "Статус совпадает",
        "Позиций готовой продукции",
        "Исключено позиций",
        "Количество готовой продукции",
    ]

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Маркетплейс": st.column_config.TextColumn(
                "Маркетплейс",
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
            "Позиций готовой продукции": (
                st.column_config.NumberColumn(
                    "Позиций готовой продукции",
                    format="%d",
                )
            ),
            "Исключено позиций": st.column_config.NumberColumn(
                "Исключено позиций",
                format="%d",
            ),
            "Количество готовой продукции": (
                st.column_config.NumberColumn(
                    "Количество готовой продукции",
                    format="%d",
                )
            ),
        },
    )


def _render_detail_table(
    rows: list[dict],
) -> None:
    """Показывает построчное сравнение артикулов."""
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

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Маркетплейс": st.column_config.TextColumn(
                "Маркетплейс",
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
            "Выбранные отгрузки": st.column_config.TextColumn(
                "Выбранные отгрузки",
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
            "В выбранных отгрузках": (
                st.column_config.NumberColumn(
                    "В выбранных отгрузках",
                    format="%d",
                )
            ),
            "Отклонение": st.column_config.NumberColumn(
                "Отклонение",
                format="%+d",
            ),
            "Результат": st.column_config.TextColumn(
                "Результат",
                width="medium",
            ),
        },
    )


def _render_aggregate_table(
    rows: list[dict],
) -> None:
    """Показывает итог по артикулам."""
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

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Маркетплейс": st.column_config.TextColumn(
                "Маркетплейс",
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
            "В выбранных отгрузках": (
                st.column_config.NumberColumn(
                    "В выбранных отгрузках",
                    format="%d",
                )
            ),
            "Отклонение": st.column_config.NumberColumn(
                "Отклонение",
                format="%+d",
            ),
            "Результат": st.column_config.TextColumn(
                "Результат",
                width="medium",
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
    """Отображает итоговую диагностику."""
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
        result["total_additional_linked_demands"],
    )

    st.caption(
        "Период отбирает именно отгрузки по полю `demand.moment`: "
        f"**{_format_date_range(result['date_from'], result['date_to'])}**."
    )

    st.success(
        "Сверка выполняется только по товарам дерева "
        f"«{result['finished_products_folder_name']}». "
        "Расходники, этикет-лента, гофрокороба, услуги, комплекты "
        "и товары вне готовой продукции не участвуют."
    )

    st.caption(
        "В дерево готовой продукции входит товарных групп: "
        f"**{result['finished_products_folder_count']}**."
    )

    if result["total_additional_linked_demands"] > 0:
        st.warning(
            "У части связанных заказов есть отгрузки вне выбранного "
            "периода. Они не прибавляются к количеству текущего "
            "отчёта, чтобы не смешивать разные даты фактической "
            "отгрузки."
        )

    if result["total_status_mismatches"] > 0:
        st.info(
            "У выбранных документов есть несовпадения статусов "
            "заказа и отгрузки: "
            f"**{result['total_status_mismatches']}**. "
            "Они не исключаются из диагностики автоматически."
        )

    st.divider()
    st.subheader("📦 Связанные заказы")

    _render_order_summary_table(
        result["order_summary_rows"]
    )

    st.divider()
    st.subheader("🚚 Выбранные отгрузки")

    _render_demand_diagnostic_table(
        result["demand_diagnostic_rows"]
    )

    st.divider()
    st.subheader("📋 Данные для менеджеров")

    st.caption(
        "В каждом поле есть детализация по отдельным заказам "
        "и общая сводка недобора / перебора по всем выбранным "
        "заказам соответствующего маркетплейса."
    )

    wb_col, ozon_col = st.columns(2)

    with wb_col:
        st.text_area(
            "WB — текст для копирования",
            value=result["wb_manager_text"],
            height=520,
            key="mysklad_reconciliation_wb_manager_text",
        )

    with ozon_col:
        st.text_area(
            "Ozon — текст для копирования",
            value=result["ozon_manager_text"],
            height=520,
            key="mysklad_reconciliation_ozon_manager_text",
        )

    st.divider()
    st.subheader("⚠️ Отклонения по заказам")

    if result["problem_rows"]:
        _render_detail_table(result["problem_rows"])
    else:
        st.success(
            "По выбранным отгрузкам количество готовой продукции "
            "совпало с количеством в связанных заказах."
        )

    st.divider()
    st.subheader("📌 Итог по артикулам")

    if result["problem_aggregate_rows"]:
        _render_aggregate_table(
            result["problem_aggregate_rows"]
        )
    else:
        st.info(
            "По выбранным WB FBO и Ozon FBO-отгрузкам нет "
            "артикулов с недобором или перебором."
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
        if result["detail_rows"]:
            _render_detail_table(
                result["detail_rows"]
            )
        else:
            st.info(
                "В выбранных документах не найдено "
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
    """
    Отображает диагностику FBO-заказов и отгрузок.

    Все запросы к API МоегоСклада выполняются только методом GET.
    Приложение не изменяет документы, связи, статусы, позиции
    и остатки.
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
            Выберите период по дате фактической отгрузки, затем
            отметьте нужные FBO-отгрузки WB и Ozon. Приложение
            определит связанные заказы, отсеет расходники и подготовит
            отдельные тексты для менеджеров WB и Ozon.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "В выбор попадают только отгрузки, связанные с заказами "
        "контрагентов FBO WB и FBO Ozon. Фильтр периода применяется "
        "к `demand.moment`, а не к дате создания заказа."
    )

    st.caption(
        "Все обращения к API read-only: приложение только получает "
        "документы и позиции, не меняет статусы, связи, остатки "
        "или состав отгрузок."
    )

    mysklad_token = _get_mysklad_token()

    if not mysklad_token:
        st.error(
            "Для диагностики не настроен `MYSKLAD_API_TOKEN` "
            "в Streamlit Secrets."
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
                "Фильтруется поле `demand.moment`: дата документа "
                "отгрузки МоегоСклада. Заказ может быть создан "
                "значительно раньше и определяется по связи "
                "`demand.customerOrder`."
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
                "Получаю отгрузки периода, связанные заказы "
                "и определяю FBO WB / FBO Ozon…"
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
        "Не отнесено к FBO WB / FBO Ozon: "
        f"**{catalog['non_fbo_or_unknown_count']}**. "
        "Доступно для выбора: "
        f"**{len(demand_rows)}**."
    )

    if not demand_rows:
        st.warning(
            "За выбранный период не найдено отгрузок, связанных "
            "с заказами FBO WB или FBO Ozon."
        )

        st.info(
            "Проверьте, что у заказов покупателей заполнен контрагент "
            "с понятным названием, например `FBO Ozon` или `FBO WB`."
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
        format_func=lambda demand_id: _catalog_demand_label(
            demand_id=demand_id,
            catalog_by_demand_id=catalog_by_demand_id,
        ),
        key=widget_key,
        placeholder=(
            "Выберите одну или несколько FBO-отгрузок"
        ),
        help=(
            "Отгрузки отсортированы по маркетплейсу, контрагенту, "
            "номеру заказа и номеру отгрузки."
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

    unique_selected_demand_ids = list(
        dict.fromkeys(selected_demand_ids)
    )

    if unique_selected_demand_ids:
        selected_wb_count = sum(
            1
            for demand_id in unique_selected_demand_ids
            if catalog_by_demand_id[demand_id][
                "marketplace"
            ]
            == MARKETPLACE_WB
        )

        selected_ozon_count = sum(
            1
            for demand_id in unique_selected_demand_ids
            if catalog_by_demand_id[demand_id][
                "marketplace"
            ]
            == MARKETPLACE_OZON
        )

        metric_1, metric_2, metric_3 = st.columns(3)

        metric_1.metric(
            "Выбрано отгрузок",
            len(unique_selected_demand_ids),
        )

        metric_2.metric(
            "WB FBO",
            selected_wb_count,
        )

        metric_3.metric(
            "Ozon FBO",
            selected_ozon_count,
        )

    result_signature = (
        catalog_signature,
        tuple(sorted(unique_selected_demand_ids)),
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
        "🧪 Сверить заказы и отгрузки",
        type="primary",
        use_container_width=True,
        key="run_order_shipment_reconciliation",
        disabled=not unique_selected_demand_ids,
        help=(
            "Для каждой выбранной отгрузки определяется связанный "
            "заказ. В расчёт попадёт только готовая продукция "
            "из раздела «Готовая продукция»."
        ),
    )

    if diagnose_clicked:
        st.session_state[
            "mysklad_reconciliation_result"
        ] = None

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
                        unique_selected_demand_ids
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