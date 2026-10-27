from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import streamlit as st

from mysklad_api import MySkladApiError, MySkladClient


DEFAULT_INCLUDED_STATUS_NAMES = (
    "Отгружено",
    "Принято на МП",
    "Доставлено",
)

# В МоемСкладе `variant` — модификация физического товара.
# Например: товар одного артикула с разным размером или цветом.
# Такие позиции должны учитываться в количестве отгруженных штук.
PHYSICAL_ASSORTMENT_TYPES = {
    "product",
    "variant",
}


def _value_to_date(value: object) -> date | None:
    """Преобразует одиночное значение Streamlit в `date`."""
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
    """
    Преобразует `moment` документа МоегоСклада в datetime.

    JSON API МоегоСклада обычно возвращает дату документа в формате:

    `YYYY-MM-DD HH:MM:SS.mmm`
    """
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
    """Форматирует дату документа для таблицы."""
    parsed = _parse_moment(value)

    if parsed is None:
        return str(value or "Дата не определена").strip()

    return parsed.strftime("%d.%m.%Y %H:%M")


def _moment_sort_key(value: object) -> datetime:
    """Возвращает безопасный ключ сортировки по дате документа."""
    parsed = _parse_moment(value)

    return parsed or datetime.min


def _state_id_from_meta(state: object) -> str:
    """
    Извлекает ID статуса из объекта статуса либо `meta.href`.

    В разных ответах МоегоСклада статус может содержать явный `id`
    или только ссылку в `meta.href`.
    """
    if not isinstance(state, dict):
        return ""

    direct_id = str(state.get("id") or "").strip()

    if direct_id:
        return direct_id

    meta = state.get("meta")

    if not isinstance(meta, dict):
        return ""

    href = str(meta.get("href") or "").strip().rstrip("/")

    if not href:
        return ""

    return href.rsplit("/", maxsplit=1)[-1]


def _state_name_map(
    states: list[dict],
) -> dict[str, str]:
    """Строит соответствие ID статуса и отображаемого названия."""
    result: dict[str, str] = {}

    for state in states:
        if not isinstance(state, dict):
            continue

        state_id = _state_id_from_meta(state)

        if not state_id:
            continue

        state_name = str(state.get("name") or "").strip()

        if not state_name:
            raise MySkladApiError(
                "МойСклад вернул статус отгрузки без названия."
            )

        previous_name = result.get(state_id)

        if (
            previous_name is not None
            and previous_name != state_name
        ):
            raise MySkladApiError(
                "МойСклад вернул один ID статуса отгрузки "
                "с разными названиями. Отчёт не сформирован."
            )

        result[state_id] = state_name

    if not result:
        raise MySkladApiError(
            "МойСклад не вернул доступных статусов отгрузок."
        )

    return result


def _state_options_from_states(
    states: list[dict],
) -> list[dict[str, str]]:
    """
    Подготавливает статусы для `st.multiselect`.

    Выбор хранит ID статуса, а не его название. Это защищает фильтр
    от случайного совпадения или переименования статуса.
    """
    state_names = _state_name_map(states)

    options = [
        {
            "id": state_id,
            "name": state_name,
        }
        for state_id, state_name in state_names.items()
    ]

    return sorted(
        options,
        key=lambda row: (
            row["name"].casefold(),
            row["name"],
            row["id"],
        ),
    )


def _demand_state_id(demand: dict) -> str:
    """Возвращает ID статуса из ответа документа «Отгрузка»."""
    return _state_id_from_meta(demand.get("state"))


def _demand_status_name(
    demand: dict,
    state_names: dict[str, str],
) -> str:
    """Возвращает отображаемое название статуса отгрузки."""
    state = demand.get("state")

    if not isinstance(state, dict):
        return "Без статуса"

    embedded_name = str(state.get("name") or "").strip()

    if embedded_name:
        return embedded_name

    state_id = _demand_state_id(demand)

    if state_id:
        return state_names.get(
            state_id,
            "Статус не определён",
        )

    return "Без статуса"


def _applicable_text(value: object) -> str:
    """Возвращает отображаемый признак проведения документа."""
    if value is True:
        return "Да"

    if value is False:
        return "Нет"

    return "Не определено"


def _require_demand_id(demand: dict) -> str:
    """Получает UUID отгрузки из минимально нужных полей ответа."""
    demand_id = str(demand.get("id") or "").strip()

    if not demand_id:
        raise MySkladApiError(
            "МойСклад вернул отгрузку без корректного ID. "
            "Отчёт не сформирован, чтобы не потерять позиции документа."
        )

    return demand_id


def _demand_number(demand: dict) -> str:
    """Возвращает номер отгрузки без данных покупателя."""
    number = str(demand.get("name") or "").strip()

    return number or "Без номера"


def _physical_item_quantity(
    position: dict,
    demand_number: str,
) -> tuple[int, str]:
    """
    Проверяет позицию и возвращает количество физического товара.

    В расчёт включаются:

    - `product` — товар;
    - `variant` — модификация товара.

    В МоемСкладе модификация (`variant`) является физической
    номенклатурой с собственным артикулом, поэтому её quantity нужно
    считать наравне с обычным товаром.

    Расчёт останавливается для:

    - `service`;
    - `bundle`;
    - `consignment`;
    - неизвестного типа;
    - дробного или отрицательного количества.

    Это исключает смешивание штук товаров с услугами, комплектами
    и другими несопоставимыми сущностями.
    """
    assortment = position.get("assortment")

    if not isinstance(assortment, dict):
        raise MySkladApiError(
            "МойСклад вернул позицию отгрузки без номенклатуры "
            f"в документе `{demand_number}`."
        )

    meta = assortment.get("meta")

    if not isinstance(meta, dict):
        raise MySkladApiError(
            "МойСклад вернул номенклатуру позиции без `meta` "
            f"в документе `{demand_number}`."
        )

    assortment_type = str(meta.get("type") or "").strip()

    if assortment_type not in PHYSICAL_ASSORTMENT_TYPES:
        shown_type = assortment_type or "не определён"

        raise MySkladApiError(
            "В отгрузке найден тип номенклатуры, который нельзя "
            "считать как штучный физический товар: "
            f"`{shown_type}` в документе `{demand_number}`. "
            "Отчёт не сформирован, чтобы не смешать товары и "
            "модификации с услугами, комплектами или партиями."
        )

    quantity_value = position.get("quantity")

    if isinstance(quantity_value, bool):
        raise MySkladApiError(
            "МойСклад вернул некорректное количество товара "
            f"в отгрузке `{demand_number}`."
        )

    try:
        quantity = Decimal(str(quantity_value))
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ) as error:
        raise MySkladApiError(
            "МойСклад вернул некорректное количество товара "
            f"в отгрузке `{demand_number}`."
        ) from error

    if not quantity.is_finite():
        raise MySkladApiError(
            "МойСклад вернул некорректное количество товара "
            f"в отгрузке `{demand_number}`."
        )

    if quantity < 0:
        raise MySkladApiError(
            "МойСклад вернул отрицательное количество товара "
            f"в отгрузке `{demand_number}`."
        )

    if quantity != quantity.to_integral_value():
        raise MySkladApiError(
            "МойСклад вернул дробное количество товара "
            f"в отгрузке `{demand_number}`. "
            "Отчёт поддерживает только целые штуки."
        )

    return int(quantity), assortment_type


def _validate_selected_state_ids(
    selected_state_ids: set[str],
    state_names: dict[str, str],
) -> set[str]:
    """Проверяет, что выбранные статусы существуют в метаданных."""
    normalized_ids = {
        str(state_id).strip()
        for state_id in selected_state_ids
        if str(state_id).strip()
    }

    if not normalized_ids:
        raise ValueError(
            "Выберите хотя бы один статус отгрузки."
        )

    unknown_ids = normalized_ids - set(state_names)

    if unknown_ids:
        raise MySkladApiError(
            "Выбран один или несколько статусов, которых больше нет "
            "в актуальных метаданных МоегоСклада. "
            "Обновите список статусов и повторите расчёт."
        )

    return normalized_ids


def build_mysklad_shipment_report(
    client: MySkladClient,
    date_from: date,
    date_to: date,
    selected_state_ids: set[str],
) -> dict[str, Any]:
    """
    Формирует read-only отчёт по товарам выбранных статусов отгрузок.

    Алгоритм:

    1. Получает актуальные статусы отгрузок.
    2. Получает отгрузки с `moment` в выбранном периоде.
    3. Оставляет только документы выбранных статусов.
    4. Загружает позиции только включённых документов.
    5. Считает quantity физической номенклатуры типов:
       `product` и `variant`.

    Отгрузки иных статусов не запрашиваются по позициям, поэтому
    лишние обращения к API не выполняются.
    """
    if date_from > date_to:
        raise ValueError(
            "Дата начала периода не может быть позже даты окончания."
        )

    demand_states = client.demand_states()
    state_names = _state_name_map(demand_states)

    normalized_selected_ids = _validate_selected_state_ids(
        selected_state_ids=selected_state_ids,
        state_names=state_names,
    )

    demands = client.demands_by_moment(
        date_from=date_from,
        date_to=date_to,
    )

    status_totals: dict[str, dict[str, int | str]] = {}

    for state_id in normalized_selected_ids:
        status_totals[state_id] = {
            "Статус отгрузки": state_names[state_id],
            "Документов": 0,
            "Проведено документов": 0,
            "Непроведено документов": 0,
            "Не определено проведение": 0,
            "Товарных позиций": 0,
            "Позиций product": 0,
            "Позиций variant": 0,
            "Количество товара": 0,
        }

    seen_demand_ids: set[str] = set()
    document_rows: list[dict] = []

    total_positions = 0
    total_product_positions = 0
    total_variant_positions = 0
    total_quantity = 0

    total_applicable_documents = 0
    total_not_applicable_documents = 0
    total_unknown_applicable_documents = 0

    total_documents_in_period = 0
    excluded_by_status_count = 0
    documents_without_status_count = 0

    for demand in demands:
        if not isinstance(demand, dict):
            continue

        demand_id = _require_demand_id(demand)

        if demand_id in seen_demand_ids:
            raise MySkladApiError(
                "МойСклад вернул одну и ту же отгрузку повторно. "
                "Отчёт не сформирован, чтобы исключить двойной учёт."
            )

        seen_demand_ids.add(demand_id)
        total_documents_in_period += 1

        state_id = _demand_state_id(demand)

        if not state_id:
            documents_without_status_count += 1
            excluded_by_status_count += 1
            continue

        if state_id not in normalized_selected_ids:
            excluded_by_status_count += 1
            continue

        demand_number = _demand_number(demand)
        status_name = _demand_status_name(
            demand=demand,
            state_names=state_names,
        )

        applicable_value = demand.get("applicable")
        applicable_text = _applicable_text(applicable_value)

        positions = client.demand_positions(
            demand_id=demand_id
        )

        document_position_count = 0
        document_product_positions = 0
        document_variant_positions = 0
        document_quantity = 0

        for position in positions:
            if not isinstance(position, dict):
                raise MySkladApiError(
                    "МойСклад вернул некорректную позицию "
                    f"в отгрузке `{demand_number}`."
                )

            quantity, assortment_type = _physical_item_quantity(
                position=position,
                demand_number=demand_number,
            )

            document_position_count += 1
            document_quantity += quantity

            if assortment_type == "product":
                document_product_positions += 1
            else:
                document_variant_positions += 1

        status_total = status_totals[state_id]

        status_total["Документов"] = (
            int(status_total["Документов"]) + 1
        )

        status_total["Товарных позиций"] = (
            int(status_total["Товарных позиций"])
            + document_position_count
        )

        status_total["Позиций product"] = (
            int(status_total["Позиций product"])
            + document_product_positions
        )

        status_total["Позиций variant"] = (
            int(status_total["Позиций variant"])
            + document_variant_positions
        )

        status_total["Количество товара"] = (
            int(status_total["Количество товара"])
            + document_quantity
        )

        if applicable_value is True:
            status_total["Проведено документов"] = (
                int(status_total["Проведено документов"]) + 1
            )
            total_applicable_documents += 1

        elif applicable_value is False:
            status_total["Непроведено документов"] = (
                int(status_total["Непроведено документов"]) + 1
            )
            total_not_applicable_documents += 1

        else:
            status_total["Не определено проведение"] = (
                int(status_total["Не определено проведение"]) + 1
            )
            total_unknown_applicable_documents += 1

        total_positions += document_position_count
        total_product_positions += document_product_positions
        total_variant_positions += document_variant_positions
        total_quantity += document_quantity

        document_rows.append(
            {
                "_moment_sort": _moment_sort_key(
                    demand.get("moment")
                ),
                "Дата документа": _format_moment(
                    demand.get("moment")
                ),
                "Отгрузка": demand_number,
                "ID отгрузки": demand_id,
                "Статус": status_name,
                "Проведена": applicable_text,
                "Товарных позиций": document_position_count,
                "Позиций product": document_product_positions,
                "Позиций variant": document_variant_positions,
                "Количество товара": document_quantity,
            }
        )

    status_rows = list(status_totals.values())

    status_rows.sort(
        key=lambda row: (
            -int(row["Количество товара"]),
            str(row["Статус отгрузки"]).casefold(),
            str(row["Статус отгрузки"]),
        )
    )

    document_rows.sort(
        key=lambda row: (
            row["_moment_sort"],
            row["Отгрузка"].casefold(),
            row["Отгрузка"],
            row["ID отгрузки"],
        ),
        reverse=True,
    )

    for row in document_rows:
        row.pop("_moment_sort", None)

    selected_status_names = [
        state_names[state_id]
        for state_id in sorted(
            normalized_selected_ids,
            key=lambda state_id: (
                state_names[state_id].casefold(),
                state_names[state_id],
                state_id,
            ),
        )
    ]

    return {
        "date_from": date_from,
        "date_to": date_to,
        "generated_at": datetime.now(),
        "selected_state_ids": sorted(normalized_selected_ids),
        "selected_status_names": selected_status_names,
        "available_state_options": _state_options_from_states(
            demand_states
        ),
        "total_documents_in_period": total_documents_in_period,
        "excluded_by_status_count": excluded_by_status_count,
        "documents_without_status_count": (
            documents_without_status_count
        ),
        "total_documents": len(document_rows),
        "total_applicable_documents": total_applicable_documents,
        "total_not_applicable_documents": (
            total_not_applicable_documents
        ),
        "total_unknown_applicable_documents": (
            total_unknown_applicable_documents
        ),
        "total_positions": total_positions,
        "total_product_positions": total_product_positions,
        "total_variant_positions": total_variant_positions,
        "total_quantity": total_quantity,
        "status_rows": status_rows,
        "document_rows": document_rows,
    }


def _render_status_table(
    rows: list[dict],
) -> None:
    """Показывает сводку по выбранным статусам."""
    columns = [
        "Статус отгрузки",
        "Документов",
        "Проведено документов",
        "Непроведено документов",
        "Не определено проведение",
        "Товарных позиций",
        "Позиций product",
        "Позиций variant",
        "Количество товара",
    ]

    table = pd.DataFrame(
        rows,
        columns=columns,
    )

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Статус отгрузки": st.column_config.TextColumn(
                "Статус отгрузки",
                width="large",
            ),
            "Документов": st.column_config.NumberColumn(
                "Документов",
                format="%d",
            ),
            "Проведено документов": st.column_config.NumberColumn(
                "Проведено документов",
                format="%d",
            ),
            "Непроведено документов": st.column_config.NumberColumn(
                "Непроведено документов",
                format="%d",
            ),
            "Не определено проведение": st.column_config.NumberColumn(
                "Не определено проведение",
                format="%d",
            ),
            "Товарных позиций": st.column_config.NumberColumn(
                "Товарных позиций",
                format="%d",
            ),
            "Позиций product": st.column_config.NumberColumn(
                "Позиций product",
                format="%d",
            ),
            "Позиций variant": st.column_config.NumberColumn(
                "Позиций variant",
                format="%d",
            ),
            "Количество товара": st.column_config.NumberColumn(
                "Количество товара",
                format="%d",
            ),
        },
    )


def _render_document_table(
    rows: list[dict],
) -> None:
    """
    Показывает отгрузки, вошедшие в расчёт.

    Таблица не содержит покупателей, контрагентов, адресов, телефонов
    и иных персональных данных.
    """
    columns = [
        "Дата документа",
        "Отгрузка",
        "ID отгрузки",
        "Статус",
        "Проведена",
        "Товарных позиций",
        "Позиций product",
        "Позиций variant",
        "Количество товара",
    ]

    table = pd.DataFrame(
        rows,
        columns=columns,
    )

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Дата документа": st.column_config.TextColumn(
                "Дата документа",
                width="medium",
            ),
            "Отгрузка": st.column_config.TextColumn(
                "Отгрузка",
                width="medium",
            ),
            "ID отгрузки": st.column_config.TextColumn(
                "ID отгрузки",
                width="large",
            ),
            "Статус": st.column_config.TextColumn(
                "Статус",
                width="large",
            ),
            "Проведена": st.column_config.TextColumn(
                "Проведена",
                width="small",
            ),
            "Товарных позиций": st.column_config.NumberColumn(
                "Товарных позиций",
                format="%d",
            ),
            "Позиций product": st.column_config.NumberColumn(
                "Позиций product",
                format="%d",
            ),
            "Позиций variant": st.column_config.NumberColumn(
                "Позиций variant",
                format="%d",
            ),
            "Количество товара": st.column_config.NumberColumn(
                "Количество товара",
                format="%d",
            ),
        },
    )


def _render_result(
    result: dict[str, Any],
) -> None:
    """Отображает результат расчёта по выбранным статусам."""
    st.divider()
    st.subheader("📊 Количество товара по выбранным статусам")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric(
        "Отгрузок по фильтру",
        result["total_documents"],
    )

    metric_2.metric(
        "Товарных позиций",
        result["total_positions"],
    )

    metric_3.metric(
        "Количество товара",
        result["total_quantity"],
    )

    metric_4.metric(
        "Позиций variant",
        result["total_variant_positions"],
    )

    metric_5, metric_6, metric_7, metric_8 = st.columns(4)

    metric_5.metric(
        "Проведено документов",
        result["total_applicable_documents"],
    )

    metric_6.metric(
        "Непроведено документов",
        result["total_not_applicable_documents"],
    )

    metric_7.metric(
        "Позиций product",
        result["total_product_positions"],
    )

    metric_8.metric(
        "Исключено по статусу",
        result["excluded_by_status_count"],
    )

    st.caption(
        "Период по дате документа `moment`: "
        f"**{_format_date_range(result['date_from'], result['date_to'])}**."
    )

    st.caption(
        "Выбранные статусы: "
        + ", ".join(
            f"`{name}`"
            for name in result["selected_status_names"]
        )
        + "."
    )

    st.caption(
        "В расчёт входят физические позиции номенклатуры типов "
        "`product` и `variant`. `variant` — модификация товара "
        "в МоемСкладе, например товар другого размера, цвета или версии."
    )

    st.caption(
        "Всего документов с `moment` в периоде: "
        f"**{result['total_documents_in_period']}**. "
        "Позиции документов других статусов не запрашиваются."
    )

    if result["documents_without_status_count"] > 0:
        st.info(
            "В периоде найдено документов без статуса: "
            f"**{result['documents_without_status_count']}**. "
            "Они не входят в расчёт, пока не будет назначен статус."
        )

    st.divider()
    st.subheader("📌 Сводка по статусам")

    _render_status_table(result["status_rows"])

    st.divider()
    st.subheader("📄 Отгрузки, вошедшие в расчёт")

    if not result["document_rows"]:
        st.info(
            "За выбранный период не найдено отгрузок "
            "с выбранными статусами."
        )
        return

    st.caption(
        "Таблица содержит только безопасные служебные поля документов "
        "и агрегаты товарных позиций."
    )

    _render_document_table(result["document_rows"])


def _get_mysklad_token() -> str:
    """Безопасно читает токен МоегоСклада из Streamlit Secrets."""
    try:
        return str(
            st.secrets["MYSKLAD_API_TOKEN"]
        ).strip()
    except Exception:
        return ""


def _get_cached_status_options() -> list[dict[str, str]]:
    """Возвращает ранее загруженный список статусов отгрузок."""
    value = st.session_state.get(
        "mysklad_shipment_status_options"
    )

    if not isinstance(value, list):
        return []

    result: list[dict[str, str]] = []

    for item in value:
        if not isinstance(item, dict):
            continue

        state_id = str(item.get("id") or "").strip()
        state_name = str(item.get("name") or "").strip()

        if state_id and state_name:
            result.append(
                {
                    "id": state_id,
                    "name": state_name,
                }
            )

    return result


def _default_selected_status_ids(
    options: list[dict[str, str]],
) -> list[str]:
    """
    Возвращает ID важных статусов по умолчанию.

    По текущему согласованному процессу включаются:

    - Отгружено;
    - Принято на МП;
    - Доставлено.
    """
    default_names = {
        name.casefold()
        for name in DEFAULT_INCLUDED_STATUS_NAMES
    }

    return [
        option["id"]
        for option in options
        if option["name"].casefold() in default_names
    ]


def _prepare_status_selection_state(
    options: list[dict[str, str]],
) -> None:
    """
    Инициализирует или обновляет пользовательский выбор статусов.

    При первой загрузке выбираются важные статусы по умолчанию.
    При последующем обновлении сохраняется существующий выбор,
    если статусы всё ещё доступны.
    """
    option_ids = {
        option["id"]
        for option in options
    }

    selection_key = (
        "mysklad_shipment_selected_state_ids"
    )

    initialized_key = (
        "mysklad_shipment_status_selection_initialized"
    )

    raw_selected = st.session_state.get(
        selection_key,
        [],
    )

    if not isinstance(raw_selected, list):
        raw_selected = []

    valid_selected = [
        str(state_id).strip()
        for state_id in raw_selected
        if str(state_id).strip() in option_ids
    ]

    is_initialized = bool(
        st.session_state.get(initialized_key, False)
    )

    if not is_initialized:
        valid_selected = _default_selected_status_ids(
            options
        )
        st.session_state[initialized_key] = True

    st.session_state[selection_key] = valid_selected


def render_mysklad_shipments_tab() -> None:
    """
    Отображает вкладку количества товара в отгрузках МоегоСклада.

    Все обращения к API выполняются только через GET-запросы.
    Приложение не создаёт, не проводит, не изменяет и не удаляет
    документы, статусы или позиции.
    """
    st.title("📊 Отгрузки МоегоСклада")

    st.markdown(
        """
        <div class="description-box">
            Выберите период по дате документа отгрузки <code>moment</code>
            и один или несколько статусов. Приложение посчитает
            количество физического товара только в отгрузках выбранных
            статусов.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "По умолчанию выбраны статусы: `Отгружено`, "
        "`Принято на МП`, `Доставлено`."
    )

    st.caption(
        "Учитываются физические позиции типов `product` и `variant`. "
        "Модификация `variant` считается товаром, а не ошибкой."
    )

    mysklad_token = _get_mysklad_token()

    if not mysklad_token:
        st.error(
            "Для отчёта по отгрузкам не настроен "
            "`MYSKLAD_API_TOKEN` в Streamlit Secrets."
        )
        return

    st.subheader("Фильтры")

    today = datetime.now().date()

    date_col, status_load_col = st.columns([2, 1])

    with date_col:
        selected_date_range = st.date_input(
            "Период по дате документа отгрузки",
            value=(today, today),
            format="DD.MM.YYYY",
            key="mysklad_shipment_report_date_range",
            help=(
                "Фильтруется поле `moment`: дата документа отгрузки "
                "МоегоСклада, а не дата создания или обновления."
            ),
        )

    with status_load_col:
        st.markdown("<br>", unsafe_allow_html=True)

        load_statuses_clicked = st.button(
            "⟳ Загрузить статусы",
            use_container_width=True,
            key="load_mysklad_shipment_statuses",
        )

    if load_statuses_clicked:
        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Загружаю доступные статусы отгрузок "
                "МоегоСклада…"
            ):
                options = _state_options_from_states(
                    client.demand_states()
                )

            st.session_state[
                "mysklad_shipment_status_options"
            ] = options

            _prepare_status_selection_state(options)

            st.session_state[
                "mysklad_shipment_report_result"
            ] = None

            st.rerun()

        except MySkladApiError as error:
            st.error(str(error))

    date_from, date_to = _parse_date_range(
        selected_date_range
    )

    if date_from is None or date_to is None:
        st.warning(
            "Выберите одну дату или полный диапазон дат."
        )
        return

    status_options = _get_cached_status_options()

    if not status_options:
        st.info(
            "Нажмите «Загрузить статусы», затем выберите статусы "
            "отгрузок для расчёта."
        )
        return

    option_name_by_id = {
        option["id"]: option["name"]
        for option in status_options
    }

    option_ids = [
        option["id"]
        for option in status_options
    ]

    selected_state_ids = st.multiselect(
        "Статусы отгрузок для включения в расчёт",
        options=option_ids,
        default=st.session_state.get(
            "mysklad_shipment_selected_state_ids",
            _default_selected_status_ids(status_options),
        ),
        format_func=lambda state_id: option_name_by_id[
            state_id
        ],
        key="mysklad_shipment_selected_state_ids",
        placeholder="Выберите один или несколько статусов",
        help=(
            "Можно выбрать одновременно несколько статусов. "
            "В расчёт войдут только документы с выбранными статусами."
        ),
    )

    if not selected_state_ids:
        st.warning(
            "Выберите хотя бы один статус отгрузки."
        )

    st.caption(
        "Период: "
        f"**{_format_date_range(date_from, date_to)}**. "
        "Выбор статусов применяется к документам отгрузки, "
        "найденным по `moment`."
    )

    result_signature = (
        date_from.isoformat(),
        date_to.isoformat(),
        tuple(sorted(selected_state_ids)),
    )

    saved = st.session_state.get(
        "mysklad_shipment_report_result"
    )

    if (
        saved is not None
        and saved.get("signature") != result_signature
    ):
        st.session_state[
            "mysklad_shipment_report_result"
        ] = None
        saved = None

    update_clicked = st.button(
        "🔄 Посчитать количество по выбранным статусам",
        type="primary",
        use_container_width=True,
        key="update_mysklad_shipment_report",
        disabled=not selected_state_ids,
    )

    if update_clicked:
        st.session_state[
            "mysklad_shipment_report_result"
        ] = None

        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Получаю отгрузки за период и товарные позиции "
                "выбранных статусов…"
            ):
                result = build_mysklad_shipment_report(
                    client=client,
                    date_from=date_from,
                    date_to=date_to,
                    selected_state_ids=set(selected_state_ids),
                )

            st.session_state[
                "mysklad_shipment_status_options"
            ] = result["available_state_options"]

            st.session_state[
                "mysklad_shipment_report_result"
            ] = {
                "signature": result_signature,
                "result": result,
            }

            st.success(
                "Количество товара по выбранным статусам обновлено."
            )

        except (MySkladApiError, ValueError) as error:
            st.error(str(error))

    saved = st.session_state.get(
        "mysklad_shipment_report_result"
    )

    if (
        saved is None
        or saved.get("signature") != result_signature
    ):
        st.info(
            "Выберите период и статусы, затем нажмите "
            "«Посчитать количество по выбранным статусам»."
        )
        return

    _render_result(saved["result"])