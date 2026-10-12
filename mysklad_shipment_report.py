from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st

from mysklad_api import MySkladApiError, MySkladClient


NO_STATUS_LABEL = "Без статуса"
UNKNOWN_STATUS_LABEL = "Статус не определён"


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

    В JSON API поле обычно возвращается в формате:

    `YYYY-MM-DD HH:MM:SS.mmm`

    Не интерпретируем его как UTC: `moment` является датой документа
    в учётном контуре МоегоСклада.
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
    """Форматирует дату документа для таблицы без показа полного JSON."""
    parsed = _parse_moment(value)

    if parsed is None:
        return str(value or "Дата не определена").strip()

    return parsed.strftime("%d.%m.%Y %H:%M")


def _state_id_from_meta(state: object) -> str:
    """Извлекает ID статуса из `state.meta.href`."""
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
    """Строит безопасное сопоставление UUID статуса и его названия."""
    result: dict[str, str] = {}

    for state in states:
        if not isinstance(state, dict):
            continue

        state_id = _state_id_from_meta(state)

        if not state_id:
            continue

        name = str(state.get("name") or "").strip()

        result[state_id] = name or UNKNOWN_STATUS_LABEL

    return result


def _demand_status_name(
    demand: dict,
    state_names: dict[str, str],
) -> str:
    """Определяет отображаемое название статуса отгрузки."""
    state = demand.get("state")

    if not isinstance(state, dict):
        return NO_STATUS_LABEL

    embedded_name = str(state.get("name") or "").strip()

    if embedded_name:
        return embedded_name

    state_id = _state_id_from_meta(state)

    if state_id and state_id in state_names:
        return state_names[state_id]

    if state_id:
        return UNKNOWN_STATUS_LABEL

    return NO_STATUS_LABEL


def _applicable_text(value: object) -> str:
    """Возвращает отображаемый признак проведения документа."""
    if value is True:
        return "Да"

    if value is False:
        return "Нет"

    return "Не определено"


def _require_demand_id(demand: dict) -> str:
    """Получает UUID отгрузки из минимальных нужных полей ответа."""
    demand_id = str(demand.get("id") or "").strip()

    if not demand_id:
        raise MySkladApiError(
            "МойСклад вернул отгрузку без корректного ID. "
            "Отчёт не сформирован, чтобы не потерять позиции документа."
        )

    return demand_id


def _demand_number(demand: dict) -> str:
    """Возвращает номер отгрузки без обращения к данным контрагента."""
    number = str(demand.get("name") or "").strip()

    if number:
        return number

    return "Без номера"


def _product_quantity(
    position: dict,
    demand_number: str,
) -> int:
    """
    Проверяет и возвращает целое количество товарной позиции.

    На первом этапе бизнес подтвердил, что отгрузки содержат только
    номенклатуру типа `product`, а её количество является целым.
    Если API вернёт иной тип либо дробное количество, отчёт блокируется:
    нельзя смешивать штуки товаров, услуги, комплекты или метры.
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

    if assortment_type != "product":
        raise MySkladApiError(
            "В отгрузке найден тип номенклатуры, отличный от `product`: "
            f"`{assortment_type or 'не определён'}` "
            f"в документе `{demand_number}`. "
            "Отчёт не сформирован, чтобы не смешать штуки товара "
            "с услугами, комплектами или модификациями."
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
            "Первая версия отчёта поддерживает только целые штуки."
        )

    return int(quantity)


def build_mysklad_shipment_report(
    client: MySkladClient,
    date_from: date,
    date_to: date,
) -> dict:
    """
    Формирует read-only отчёт по товарным позициям отгрузок.

    В отчёт входят все отгрузки с `moment` в выбранном периоде,
    независимо от:

    - статуса;
    - признака проведения `applicable`.

    Это сделано для первого этапа аудита статусов. После согласования
    допустимых статусов можно будет добавить точный фильтр включения.
    """
    if date_from > date_to:
        raise ValueError(
            "Дата начала периода не может быть позже даты окончания."
        )

    demand_states = client.demand_states()
    state_names = _state_name_map(demand_states)

    demands = client.demands_by_moment(
        date_from=date_from,
        date_to=date_to,
    )

    seen_demand_ids: set[str] = set()
    document_rows: list[dict] = []
    status_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "Документов": 0,
            "Проведено документов": 0,
            "Непроведено документов": 0,
            "Товарных позиций": 0,
            "Количество товара": 0,
        }
    )

    total_positions = 0
    total_quantity = 0
    total_applicable_documents = 0
    total_not_applicable_documents = 0
    total_unknown_applicable_documents = 0

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
        document_quantity = 0

        for position in positions:
            if not isinstance(position, dict):
                raise MySkladApiError(
                    "МойСклад вернул некорректную позицию "
                    f"в отгрузке `{demand_number}`."
                )

            quantity = _product_quantity(
                position=position,
                demand_number=demand_number,
            )

            document_position_count += 1
            document_quantity += quantity

        status_total = status_totals[status_name]

        status_total["Документов"] += 1
        status_total["Товарных позиций"] += (
            document_position_count
        )
        status_total["Количество товара"] += (
            document_quantity
        )

        if applicable_value is True:
            status_total["Проведено документов"] += 1
            total_applicable_documents += 1

        elif applicable_value is False:
            status_total["Непроведено документов"] += 1
            total_not_applicable_documents += 1

        else:
            total_unknown_applicable_documents += 1

        total_positions += document_position_count
        total_quantity += document_quantity

        document_rows.append(
            {
                "Дата документа": _format_moment(
                    demand.get("moment")
                ),
                "Отгрузка": demand_number,
                "ID отгрузки": demand_id,
                "Статус": status_name,
                "Проведена": applicable_text,
                "Товарных позиций": document_position_count,
                "Количество товара": document_quantity,
            }
        )

    status_rows = [
        {
            "Статус отгрузки": status_name,
            **totals,
        }
        for status_name, totals in status_totals.items()
    ]

    status_rows.sort(
        key=lambda row: (
            -row["Количество товара"],
            row["Статус отгрузки"].casefold(),
            row["Статус отгрузки"],
        )
    )

    document_rows.sort(
        key=lambda row: (
            row["Дата документа"],
            row["Отгрузка"].casefold(),
            row["Отгрузка"],
            row["ID отгрузки"],
        ),
        reverse=True,
    )

    return {
        "date_from": date_from,
        "date_to": date_to,
        "generated_at": datetime.now(),
        "total_documents": len(document_rows),
        "total_applicable_documents": total_applicable_documents,
        "total_not_applicable_documents": (
            total_not_applicable_documents
        ),
        "total_unknown_applicable_documents": (
            total_unknown_applicable_documents
        ),
        "total_positions": total_positions,
        "total_quantity": total_quantity,
        "status_rows": status_rows,
        "document_rows": document_rows,
    }


def _render_status_table(
    rows: list[dict],
) -> None:
    """Показывает сводку по всем фактически найденным статусам."""
    columns = [
        "Статус отгрузки",
        "Документов",
        "Проведено документов",
        "Непроведено документов",
        "Товарных позиций",
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
            "Товарных позиций": st.column_config.NumberColumn(
                "Товарных позиций",
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
    """Показывает отгрузки без данных покупателей и контрагентов."""
    columns = [
        "Дата документа",
        "Отгрузка",
        "ID отгрузки",
        "Статус",
        "Проведена",
        "Товарных позиций",
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
            "Количество товара": st.column_config.NumberColumn(
                "Количество товара",
                format="%d",
            ),
        },
    )


def _render_result(
    result: dict,
) -> None:
    """Отображает отчёт после успешного полного чтения данных."""
    st.divider()
    st.subheader("📊 Общая сводка")

    metric_1, metric_2, metric_3 = st.columns(3)

    metric_1.metric(
        "Отгрузок за период",
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

    metric_4, metric_5, metric_6 = st.columns(3)

    metric_4.metric(
        "Проведено документов",
        result["total_applicable_documents"],
    )

    metric_5.metric(
        "Непроведено документов",
        result["total_not_applicable_documents"],
    )

    metric_6.metric(
        "Не определено проведение",
        result["total_unknown_applicable_documents"],
    )

    st.caption(
        "Период по дате документа `moment`: "
        f"**{_format_date_range(result['date_from'], result['date_to'])}**."
    )

    st.warning(
        "На первом этапе в сумму включены все отгрузки за период "
        "независимо от их статуса и признака проведения. "
        "Используйте таблицу ниже, чтобы подтвердить статусы, "
        "которые должны считаться фактически отправленными."
    )

    st.caption(
        "Количество считается только по позициям номенклатуры "
        "типа `product` с целым количеством. Услуги, комплекты, "
        "модификации и дробные единицы не смешиваются с товарами."
    )

    st.divider()
    st.subheader("📌 Количество товара по статусам отгрузок")

    if not result["status_rows"]:
        st.info(
            "В выбранном периоде не найдено отгрузок МоегоСклада."
        )
        return

    _render_status_table(result["status_rows"])

    st.divider()
    st.subheader("📄 Отгрузки за выбранный период")

    st.caption(
        "Таблица содержит только служебные поля документов и агрегаты "
        "товарных позиций. Данные покупателей, контрагентов, адресов "
        "и полные ответы API не выводятся."
    )

    _render_document_table(result["document_rows"])


def render_mysklad_shipments_tab() -> None:
    """
    Отображает новую вкладку аудита количества товара в отгрузках.

    Все запросы к МоемСкладу выполняются только после нажатия кнопки
    обновления. Вкладка не изменяет документы, статусы и позиции.
    """
    st.title("📊 Отгрузки МоегоСклада")

    st.markdown(
        """
        <div class="description-box">
            Выберите период по дате документа отгрузки <code>moment</code>.
            Приложение посчитает количество товаров во всех найденных
            отгрузках и покажет разбивку по фактическим статусам.
            На первом этапе статусы не исключаются из расчёта.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Источник: документы «Отгрузка» МоегоСклада. "
        "Учитываются только позиции типа `product` с целым количеством."
    )

    mysklad_token = ""

    try:
        mysklad_token = str(
            st.secrets["MYSKLAD_API_TOKEN"]
        ).strip()
    except Exception:
        mysklad_token = ""

    if not mysklad_token:
        st.error(
            "Для отчёта по отгрузкам не настроен "
            "`MYSKLAD_API_TOKEN` в Streamlit Secrets."
        )
        return

    today = datetime.now().date()

    selected_date_range = st.date_input(
        "Период по дате документа отгрузки",
        value=(today, today),
        format="DD.MM.YYYY",
        key="mysklad_shipment_report_date_range",
        help=(
            "Фильтруется именно поле `moment`: дата документа "
            "отгрузки МоегоСклада, а не дата создания или обновления."
        ),
    )

    date_from, date_to = _parse_date_range(
        selected_date_range
    )

    if date_from is None or date_to is None:
        st.warning(
            "Выберите дату или полный диапазон дат."
        )
        return

    st.caption(
        "Выбранный период: "
        f"**{_format_date_range(date_from, date_to)}**."
    )

    result_signature = (
        date_from.isoformat(),
        date_to.isoformat(),
    )

    saved = st.session_state.get(
        "mysklad_shipment_report_result"
    )

    if (
        saved is not None
        and saved.get("signature") != result_signature
    ):
        st.session_state["mysklad_shipment_report_result"] = None
        saved = None

    update_clicked = st.button(
        "🔄 Посчитать отгрузки и статусы",
        type="primary",
        use_container_width=True,
        key="update_mysklad_shipment_report",
    )

    if update_clicked:
        st.session_state["mysklad_shipment_report_result"] = None

        client = MySkladClient(
            token=mysklad_token
        )

        try:
            with st.spinner(
                "Получаю статусы, отгрузки и товарные позиции "
                "МоегоСклада…"
            ):
                result = build_mysklad_shipment_report(
                    client=client,
                    date_from=date_from,
                    date_to=date_to,
                )

            st.session_state[
                "mysklad_shipment_report_result"
            ] = {
                "signature": result_signature,
                "result": result,
            }

            st.success(
                "Отгрузки и статусы МоегоСклада успешно обновлены."
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
            "Выберите период и нажмите "
            "«Посчитать отгрузки и статусы»."
        )
        return

    _render_result(saved["result"])