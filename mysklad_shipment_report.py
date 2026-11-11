from __future__ import annotations

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


DEFAULT_INCLUDED_STATUS_NAMES = (
    "Отгружено",
    "Принято на МП",
    "Доставлено",
)

FINISHED_PRODUCTS_FOLDER_NAME = "Готовая продукция"

PHYSICAL_ASSORTMENT_TYPES = {
    "product",
    "variant",
}


def _value_to_date(value: object) -> date | None:
    """Преобразует значение Streamlit в `date`."""
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
    """Преобразует `moment` МоегоСклада в datetime."""
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
    """Форматирует дату документа для таблицы."""
    parsed = _parse_moment(value)

    if parsed is None:
        return str(value or "Дата не определена").strip()

    return parsed.strftime("%d.%m.%Y %H:%M")


def _moment_sort_key(value: object) -> datetime:
    """Возвращает безопасный ключ сортировки даты."""
    return _parse_moment(value) or datetime.min


def _state_id_from_meta(state: object) -> str:
    """Извлекает ID статуса из `id` либо `meta.href`."""
    if not isinstance(state, dict):
        return ""

    direct_id = str(state.get("id") or "").strip()

    if direct_id:
        return direct_id

    meta = state.get("meta")

    if not isinstance(meta, dict):
        return ""

    meta_id = str(meta.get("id") or "").strip()

    if meta_id:
        return meta_id

    href = str(meta.get("href") or "").strip().rstrip("/")

    return href.rsplit("/", maxsplit=1)[-1] if href else ""


def _state_name_map(
    states: list[dict],
) -> dict[str, str]:
    """Строит соответствие ID статуса и его названия."""
    result: dict[str, str] = {}

    for state in states:
        if not isinstance(state, dict):
            continue

        state_id = _state_id_from_meta(state)
        state_name = str(state.get("name") or "").strip()

        if not state_id or not state_name:
            raise MySkladApiError(
                "МойСклад вернул некорректный статус отгрузки."
            )

        previous_name = result.get(state_id)

        if previous_name and previous_name != state_name:
            raise MySkladApiError(
                "МойСклад вернул один ID статуса отгрузки "
                "с разными названиями."
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
    """Подготавливает статусы для `st.multiselect`."""
    state_names = _state_name_map(states)

    return sorted(
        (
            {
                "id": state_id,
                "name": state_name,
            }
            for state_id, state_name in state_names.items()
        ),
        key=lambda row: (
            row["name"].casefold(),
            row["name"],
            row["id"],
        ),
    )


def _demand_state_id(demand: dict) -> str:
    """Возвращает ID статуса отгрузки."""
    return _state_id_from_meta(demand.get("state"))


def _demand_status_name(
    demand: dict,
    state_names: dict[str, str],
) -> str:
    """Возвращает отображаемое название статуса."""
    state = demand.get("state")

    if not isinstance(state, dict):
        return "Без статуса"

    embedded_name = str(state.get("name") or "").strip()

    if embedded_name:
        return embedded_name

    state_id = _demand_state_id(demand)

    return state_names.get(
        state_id,
        "Статус не определён",
    )


def _applicable_text(value: object) -> str:
    """Возвращает отображаемый признак проведения документа."""
    if value is True:
        return "Да"

    if value is False:
        return "Нет"

    return "Не определено"


def _require_demand_id(demand: dict) -> str:
    """Получает UUID отгрузки."""
    demand_id = str(demand.get("id") or "").strip()

    if not demand_id:
        raise MySkladApiError(
            "МойСклад вернул отгрузку без корректного ID. "
            "Отчёт не сформирован, чтобы не потерять позиции."
        )

    return demand_id


def _demand_number(demand: dict) -> str:
    """Возвращает безопасный номер отгрузки."""
    number = str(demand.get("name") or "").strip()

    return number or "Без номера"


def _as_piece_quantity(
    value: object,
    demand_number: str,
) -> int:
    """Проверяет и возвращает целое неотрицательное количество."""
    if isinstance(value, bool):
        raise MySkladApiError(
            "МойСклад вернул некорректное количество "
            f"в отгрузке `{demand_number}`."
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
            f"в отгрузке `{demand_number}`."
        ) from error

    if not quantity.is_finite() or quantity < 0:
        raise MySkladApiError(
            "МойСклад вернул некорректное количество "
            f"в отгрузке `{demand_number}`."
        )

    if quantity != quantity.to_integral_value():
        raise MySkladApiError(
            "МойСклад вернул дробное количество "
            f"в отгрузке `{demand_number}`. "
            "Отчёт учитывает только штучную готовую продукцию."
        )

    return int(quantity)


def _position_kind_and_quantity(
    client: MySkladClient,
    scope: FinishedProductsScope,
    position: dict,
    demand_number: str,
) -> tuple[str, int]:
    """
    Возвращает результат фильтрации позиции.

    Возможные значения `kind`:

    - `included_product`;
    - `included_variant`;
    - `outside_finished_products`;
    - `unsupported_assortment_type`;
    - `without_product_folder`.

    В итоговое количество попадают только первые два варианта.
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
            "МойСклад вернул номенклатуру без `meta` "
            f"в отгрузке `{demand_number}`."
        )

    assortment_type = str(meta.get("type") or "").strip()
    quantity = _as_piece_quantity(
        value=position.get("quantity"),
        demand_number=demand_number,
    )

    if assortment_type not in PHYSICAL_ASSORTMENT_TYPES:
        return "unsupported_assortment_type", quantity

    folder_id = client.assortment_product_folder_id(
        assortment=assortment
    )

    if not folder_id:
        return "without_product_folder", quantity

    if folder_id not in scope.included_folder_ids:
        return "outside_finished_products", quantity

    if assortment_type == "product":
        return "included_product", quantity

    return "included_variant", quantity


def _validate_selected_state_ids(
    selected_state_ids: set[str],
    state_names: dict[str, str],
) -> set[str]:
    """Проверяет выбранные статусы."""
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
            "Выбран один или несколько неактуальных статусов. "
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
    Формирует read-only отчёт по отгрузкам.

    В расчёт включаются только `product` и `variant`, относящиеся
    к разделу «Готовая продукция» либо к его дочерним разделам.

    Расходники, гофрокороба, этикет-лента, услуги, комплекты и позиции
    вне дерева готовой продукции не участвуют в итоговом количестве.
    """
    if date_from > date_to:
        raise ValueError(
            "Дата начала периода не может быть позже даты окончания."
        )

    scope = client.finished_products_scope(
        root_folder_name=FINISHED_PRODUCTS_FOLDER_NAME
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

    status_totals: dict[str, dict[str, int | str]] = {
        state_id: {
            "Статус отгрузки": state_names[state_id],
            "Документов": 0,
            "Отгрузок с готовой продукцией": 0,
            "Проведено документов": 0,
            "Непроведено документов": 0,
            "Не определено проведение": 0,
            "Позиций готовой продукции": 0,
            "Позиций product": 0,
            "Позиций variant": 0,
            "Исключено позиций": 0,
            "Количество готовой продукции": 0,
        }
        for state_id in normalized_selected_ids
    }

    seen_demand_ids: set[str] = set()
    document_rows: list[dict] = []

    total_documents_in_period = 0
    excluded_by_status_count = 0
    documents_without_status_count = 0

    total_documents = 0
    documents_with_finished_products = 0
    documents_without_finished_products = 0

    total_applicable_documents = 0
    total_not_applicable_documents = 0
    total_unknown_applicable_documents = 0

    total_source_positions = 0
    total_included_positions = 0
    total_product_positions = 0
    total_variant_positions = 0
    total_excluded_positions = 0
    total_outside_finished_positions = 0
    total_without_folder_positions = 0
    total_unsupported_positions = 0

    total_quantity = 0
    total_excluded_quantity = 0

    for demand in demands:
        if not isinstance(demand, dict):
            continue

        demand_id = _require_demand_id(demand)

        if demand_id in seen_demand_ids:
            raise MySkladApiError(
                "МойСклад вернул одну отгрузку повторно. "
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

        positions = client.demand_positions(
            demand_id=demand_id,
            expand_assortment=True,
        )

        document_source_positions = 0
        document_included_positions = 0
        document_product_positions = 0
        document_variant_positions = 0
        document_excluded_positions = 0
        document_quantity = 0
        document_excluded_quantity = 0

        for position in positions:
            if not isinstance(position, dict):
                raise MySkladApiError(
                    "МойСклад вернул некорректную позицию "
                    f"в отгрузке `{demand_number}`."
                )

            kind, quantity = _position_kind_and_quantity(
                client=client,
                scope=scope,
                position=position,
                demand_number=demand_number,
            )

            document_source_positions += 1

            if kind == "included_product":
                document_included_positions += 1
                document_product_positions += 1
                document_quantity += quantity
                continue

            if kind == "included_variant":
                document_included_positions += 1
                document_variant_positions += 1
                document_quantity += quantity
                continue

            document_excluded_positions += 1
            document_excluded_quantity += quantity

            if kind == "outside_finished_products":
                total_outside_finished_positions += 1
            elif kind == "without_product_folder":
                total_without_folder_positions += 1
            else:
                total_unsupported_positions += 1

        status_total = status_totals[state_id]

        status_total["Документов"] = (
            int(status_total["Документов"]) + 1
        )

        status_total["Позиций готовой продукции"] = (
            int(status_total["Позиций готовой продукции"])
            + document_included_positions
        )

        status_total["Позиций product"] = (
            int(status_total["Позиций product"])
            + document_product_positions
        )

        status_total["Позиций variant"] = (
            int(status_total["Позиций variant"])
            + document_variant_positions
        )

        status_total["Исключено позиций"] = (
            int(status_total["Исключено позиций"])
            + document_excluded_positions
        )

        status_total["Количество готовой продукции"] = (
            int(status_total["Количество готовой продукции"])
            + document_quantity
        )

        if document_included_positions > 0:
            status_total["Отгрузок с готовой продукцией"] = (
                int(
                    status_total[
                        "Отгрузок с готовой продукцией"
                    ]
                )
                + 1
            )
            documents_with_finished_products += 1
        else:
            documents_without_finished_products += 1

        applicable_value = demand.get("applicable")

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

        total_documents += 1
        total_source_positions += document_source_positions
        total_included_positions += document_included_positions
        total_product_positions += document_product_positions
        total_variant_positions += document_variant_positions
        total_excluded_positions += document_excluded_positions
        total_quantity += document_quantity
        total_excluded_quantity += document_excluded_quantity

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
                "Проведена": _applicable_text(
                    applicable_value
                ),
                "Позиций готовой продукции": (
                    document_included_positions
                ),
                "Позиций product": document_product_positions,
                "Позиций variant": document_variant_positions,
                "Исключено позиций": document_excluded_positions,
                "Количество готовой продукции": document_quantity,
            }
        )

    status_rows = sorted(
        status_totals.values(),
        key=lambda row: (
            -int(row["Количество готовой продукции"]),
            str(row["Статус отгрузки"]).casefold(),
            str(row["Статус отгрузки"]),
        ),
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
        "finished_products_folder_name": scope.root_folder_name,
        "finished_products_folder_count": scope.folder_count,
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
        "total_documents": total_documents,
        "documents_with_finished_products": (
            documents_with_finished_products
        ),
        "documents_without_finished_products": (
            documents_without_finished_products
        ),
        "total_applicable_documents": total_applicable_documents,
        "total_not_applicable_documents": (
            total_not_applicable_documents
        ),
        "total_unknown_applicable_documents": (
            total_unknown_applicable_documents
        ),
        "total_source_positions": total_source_positions,
        "total_included_positions": total_included_positions,
        "total_product_positions": total_product_positions,
        "total_variant_positions": total_variant_positions,
        "total_excluded_positions": total_excluded_positions,
        "total_outside_finished_positions": (
            total_outside_finished_positions
        ),
        "total_without_folder_positions": (
            total_without_folder_positions
        ),
        "total_unsupported_positions": total_unsupported_positions,
        "total_quantity": total_quantity,
        "total_excluded_quantity": total_excluded_quantity,
        "status_rows": status_rows,
        "document_rows": document_rows,
    }


def _render_status_table(rows: list[dict]) -> None:
    """Показывает сводку по выбранным статусам."""
    columns = [
        "Статус отгрузки",
        "Документов",
        "Отгрузок с готовой продукцией",
        "Проведено документов",
        "Непроведено документов",
        "Не определено проведение",
        "Позиций готовой продукции",
        "Позиций product",
        "Позиций variant",
        "Исключено позиций",
        "Количество готовой продукции",
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_document_table(rows: list[dict]) -> None:
    """Показывает отгрузки выбранных статусов."""
    columns = [
        "Дата документа",
        "Отгрузка",
        "ID отгрузки",
        "Статус",
        "Проведена",
        "Позиций готовой продукции",
        "Позиций product",
        "Позиций variant",
        "Исключено позиций",
        "Количество готовой продукции",
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=columns),
        use_container_width=True,
        hide_index=True,
    )


def _render_result(result: dict[str, Any]) -> None:
    """Отображает результат отчёта."""
    st.divider()
    st.subheader("📊 Готовая продукция в отгрузках")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric(
        "Отгрузок по статусам",
        result["total_documents"],
    )

    metric_2.metric(
        "Отгрузок с готовой продукцией",
        result["documents_with_finished_products"],
    )

    metric_3.metric(
        "Количество готовой продукции",
        result["total_quantity"],
    )

    metric_4.metric(
        "Исключено позиций",
        result["total_excluded_positions"],
    )

    metric_5, metric_6, metric_7, metric_8 = st.columns(4)

    metric_5.metric(
        "Проведено документов",
        result["total_applicable_documents"],
    )

    metric_6.metric(
        "Позиций product",
        result["total_product_positions"],
    )

    metric_7.metric(
        "Позиций variant",
        result["total_variant_positions"],
    )

    metric_8.metric(
        "Без готовой продукции",
        result["documents_without_finished_products"],
    )

    st.caption(
        "Период по дате документа `moment`: "
        f"**{_format_date_range(result['date_from'], result['date_to'])}**."
    )

    st.caption(
        "Учитывается только дерево товарных групп "
        f"`{result['finished_products_folder_name']}`: "
        f"корневая группа и вложенные разделы — "
        f"**{result['finished_products_folder_count']}** групп."
    )

    st.success(
        "Расходники, этикет-лента, гофрокороба, позиции вне "
        "«Готовой продукции», услуги и комплекты не входят "
        "в количество готовой продукции."
    )

    if result["total_without_folder_positions"] > 0:
        st.warning(
            "Найдено позиций без товарной группы: "
            f"**{result['total_without_folder_positions']}**. "
            "Они исключены из расчёта, чтобы не включить "
            "неопределённую номенклатуру."
        )

    st.caption(
        "Причины исключения позиций: вне дерева готовой продукции — "
        f"**{result['total_outside_finished_positions']}**; "
        "без товарной группы — "
        f"**{result['total_without_folder_positions']}**; "
        "неподдерживаемый тип номенклатуры — "
        f"**{result['total_unsupported_positions']}**."
    )

    st.divider()
    st.subheader("📌 Сводка по статусам")

    _render_status_table(result["status_rows"])

    st.divider()
    st.subheader("📄 Отгрузки выбранных статусов")

    if not result["document_rows"]:
        st.info(
            "За выбранный период не найдено отгрузок "
            "с выбранными статусами."
        )
        return

    _render_document_table(result["document_rows"])


def _get_mysklad_token() -> str:
    """Безопасно читает токен МоегоСклада."""
    try:
        return str(
            st.secrets["MYSKLAD_API_TOKEN"]
        ).strip()
    except Exception:
        return ""


def _get_cached_status_options() -> list[dict[str, str]]:
    """Возвращает ранее загруженные статусы."""
    raw_value = st.session_state.get(
        "mysklad_shipment_status_options"
    )

    if not isinstance(raw_value, list):
        return []

    result: list[dict[str, str]] = []

    for item in raw_value:
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
    """Возвращает ID статусов по умолчанию."""
    target_names = {
        value.casefold()
        for value in DEFAULT_INCLUDED_STATUS_NAMES
    }

    return [
        option["id"]
        for option in options
        if option["name"].casefold() in target_names
    ]


def _prepare_status_selection_state(
    options: list[dict[str, str]],
) -> None:
    """Инициализирует либо обновляет сохранённый выбор статусов."""
    option_ids = {
        option["id"]
        for option in options
    }

    selection_key = "mysklad_shipment_selected_state_ids"
    initialized_key = (
        "mysklad_shipment_status_selection_initialized"
    )

    raw_selected = st.session_state.get(selection_key, [])

    if not isinstance(raw_selected, list):
        raw_selected = []

    valid_selected = [
        str(value).strip()
        for value in raw_selected
        if str(value).strip() in option_ids
    ]

    if not st.session_state.get(initialized_key, False):
        valid_selected = _default_selected_status_ids(options)
        st.session_state[initialized_key] = True

    st.session_state[selection_key] = valid_selected


def render_mysklad_shipments_tab() -> None:
    """Отображает вкладку отгрузок МоегоСклада."""
    st.title("📊 Отгрузки МоегоСклада")

    st.markdown(
        """
        <div class="description-box">
            Выберите период и статусы отгрузок. В итоговое количество
            попадут только товары из раздела <b>«Готовая продукция»</b>
            и его вложенных групп.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Расходники, этикет-лента, гофрокороба и прочие товары вне "
        "раздела «Готовая продукция» исключаются автоматически."
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

    date_col, status_col = st.columns([2, 1])

    with date_col:
        selected_date_range = st.date_input(
            "Период по дате документа отгрузки",
            value=(today, today),
            format="DD.MM.YYYY",
            key="mysklad_shipment_report_date_range",
        )

    with status_col:
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
                "Загружаю доступные статусы отгрузок…"
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
        st.warning("Выберите дату или полный диапазон дат.")
        return

    status_options = _get_cached_status_options()

    if not status_options:
        st.info(
            "Нажмите «Загрузить статусы», затем выберите статусы."
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
        placeholder="Выберите статусы",
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

    update_clicked = st.button(
        "🔄 Посчитать готовую продукцию",
        type="primary",
        use_container_width=True,
        key="update_mysklad_shipment_report",
        disabled=not selected_state_ids,
    )

    if update_clicked:
        client = MySkladClient(token=mysklad_token)

        try:
            with st.spinner(
                "Получаю отгрузки, товарные группы и позиции "
                "готовой продукции…"
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
                "Отчёт по готовой продукции сформирован."
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
            "Выберите период и статусы, затем запустите расчёт."
        )
        return

    _render_result(saved["result"])