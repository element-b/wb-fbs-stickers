from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd
import streamlit as st

from pipeline import extract_distribution_center
from wb_api import (
    MOSCOW_TZ,
    WBApiError,
    WBClient,
    parse_wb_datetime,
    supply_is_done,
)


STATUS_SORTED = "sorted"
STATUS_CANCELLED = "cancelled"
STATUS_WAITING = "waiting"
STATUS_OTHER = "other"

CANCELLED_SUPPLIER_STATUSES = {
    "cancel",
    "cancel_carrier",
}

CANCELLED_WB_STATUSES = {
    "canceled",
    "canceled_by_client",
    "declined_by_client",
    "defect",
    "canceled_by_carrier",
}

STATUS_LABELS = {
    STATUS_SORTED: "Отсортировано",
    STATUS_CANCELLED: "Отменено",
    STATUS_WAITING: "Ждёт сортировки",
    STATUS_OTHER: "Другой статус",
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
    """Разбирает одиночную дату или диапазон `st.date_input`."""
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
    date_from: date | None,
    date_to: date | None,
) -> str:
    """Форматирует выбранный период для подписи интерфейса."""
    if date_from is None or date_to is None:
        return "Дата не выбрана"

    if date_from == date_to:
        return date_from.strftime("%d.%m.%Y")

    return (
        f"{date_from.strftime('%d.%m.%Y')} — "
        f"{date_to.strftime('%d.%m.%Y')}"
    )


def _format_supply_label(supply: dict) -> str:
    """Формирует безопасную подпись поставки в списке выбора."""
    supply_id = str(supply.get("id") or "").strip()
    name = str(supply.get("name") or "Без названия").strip()

    created_at = parse_wb_datetime(supply.get("createdAt"))

    created_text = (
        created_at.strftime("%d.%m.%Y %H:%M")
        if created_at is not None
        else "дата неизвестна"
    )

    return (
        f"{name}  |  {supply_id}  |  "
        f"{created_text}  |  завершена"
    )


def _matches_completed_supply_filters(
    supply: dict,
    search_text: str,
    date_from: date | None,
    date_to: date | None,
) -> bool:
    """
    Проверяет поставку для вкладки статусов.

    В список попадают строго завершённые поставки WB: `done = true`.
    """
    if not supply_is_done(supply):
        return False

    created_at = parse_wb_datetime(supply.get("createdAt"))

    if created_at is None:
        return False

    created_date = created_at.date()

    if date_from is not None and created_date < date_from:
        return False

    if date_to is not None and created_date > date_to:
        return False

    normalized_search = search_text.strip().casefold()

    if not normalized_search:
        return True

    supply_id = str(supply.get("id") or "").casefold()
    supply_name = str(supply.get("name") or "").casefold()

    return (
        normalized_search in supply_id
        or normalized_search in supply_name
    )


def _sort_supply_ids(
    supply_ids: list[str],
    supply_by_id: dict[str, dict],
) -> list[str]:
    """Сортирует поставки от новых к старым."""

    def sort_key(supply_id: str) -> tuple[float, str, str]:
        supply = supply_by_id[supply_id]
        created_at = parse_wb_datetime(supply.get("createdAt"))

        timestamp = (
            created_at.timestamp()
            if created_at is not None
            else 0.0
        )

        return (
            -timestamp,
            str(supply.get("name") or "").casefold(),
            supply_id.casefold(),
        )

    return sorted(supply_ids, key=sort_key)


def _classify_status(
    supplier_status: object,
    wb_status: object,
) -> str:
    """
    Сопоставляет официальные статусы WB с бизнес-категорией.

    Приоритет отмены выше остальных статусов: при противоречивом ответе
    WB задание не попадёт в «Ждёт сортировки» или «Отсортировано».
    """
    supplier_key = str(supplier_status or "").strip().casefold()
    wb_key = str(wb_status or "").strip().casefold()

    if (
        supplier_key in CANCELLED_SUPPLIER_STATUSES
        or wb_key in CANCELLED_WB_STATUSES
    ):
        return STATUS_CANCELLED

    if wb_key == "waiting":
        return STATUS_WAITING

    if wb_key == "sorted":
        return STATUS_SORTED

    return STATUS_OTHER


def _wb_status_text(value: object) -> str:
    """Возвращает безопасное отображение `wbStatus`."""
    text = str(value or "").strip()

    return text or "WB не вернул wbStatus"


def _supplier_status_text(value: object) -> str:
    """Возвращает безопасное отображение `supplierStatus`."""
    text = str(value or "").strip()

    return text or "WB не вернул supplierStatus"


def _waiting_comment(
    supplier_status: object,
) -> str:
    """Возвращает понятный комментарий для статуса `waiting`."""
    return (
        "WB ещё не получил задание после подтверждения продавцом. "
        f"supplierStatus: {_supplier_status_text(supplier_status)}."
    )


def _status_comment(
    category: str,
    supplier_status: object,
    wb_status: object,
) -> str:
    """Формирует комментарий для отменённых и других статусов."""
    supplier_text = _supplier_status_text(supplier_status)
    wb_text = _wb_status_text(wb_status)

    if category == STATUS_CANCELLED:
        return (
            "Задание отменено. "
            f"wbStatus: {wb_text}; "
            f"supplierStatus: {supplier_text}."
        )

    return (
        "Статус не относится к точным категориям "
        "«Отсортировано», «Отменено» или «Ждёт сортировки». "
        f"wbStatus: {wb_text}; "
        f"supplierStatus: {supplier_text}."
    )


def _make_supply_name(supply: dict) -> str:
    """Возвращает название поставки без выдумывания значений."""
    name = str(supply.get("name") or "").strip()

    return name or "Без названия"


def build_shipment_status_result(
    client: WBClient,
    selected_supplies: list[dict],
    article_lookback_days: int,
) -> dict:
    """
    Получает статусы заданий выбранных завершённых поставок.

    Статусы WB учитываются строго на уровне уникальных ID сборочных
    заданий. Количество товаров и товарные строки не рассчитываются:
    используемые read-only методы WB не возвращают подтверждённый состав
    многотоварного сборочного задания.

    Артикулы запрашиваются только для заданий `wbStatus = waiting`.
    Если WB не вернул артикул в выбранном периоде, сводка статусов
    остаётся полной, а детализация явно показывает отсутствие артикула.
    """
    if not selected_supplies:
        raise ValueError("Выберите хотя бы одну завершённую поставку.")

    if article_lookback_days not in (7, 14, 31):
        raise ValueError(
            "Допустимый период поиска артикулов: 7, 14 или 31 день."
        )

    supply_by_id: dict[str, dict] = {}

    for supply in selected_supplies:
        if not isinstance(supply, dict):
            raise ValueError(
                "В выбранном списке найдена некорректная поставка."
            )

        supply_id = str(supply.get("id") or "").strip()

        if not supply_id:
            raise ValueError(
                "В выбранном списке есть поставка без корректного ID."
            )

        if not supply_is_done(supply):
            raise ValueError(
                f"Поставка {supply_id} не завершена (`done = false`). "
                "Для контроля статусов можно выбрать только "
                "завершённые поставки."
            )

        if supply_id in supply_by_id:
            raise ValueError(
                "Одна и та же поставка выбрана повторно. "
                "Расчёт остановлен, чтобы исключить двойной учёт."
            )

        supply_by_id[supply_id] = supply

    order_to_supply: dict[int, str] = {}
    supply_order_ids: dict[str, set[int]] = {}
    empty_supply_ids: list[str] = []

    for supply_id, supply in supply_by_id.items():
        order_ids = client.supply_order_ids(supply_id)

        unique_order_ids = set(order_ids)

        if not unique_order_ids:
            empty_supply_ids.append(supply_id)
            supply_order_ids[supply_id] = set()
            continue

        supply_order_ids[supply_id] = unique_order_ids

        for order_id in unique_order_ids:
            previous_supply_id = order_to_supply.get(order_id)

            if (
                previous_supply_id is not None
                and previous_supply_id != supply_id
            ):
                raise WBApiError(
                    "WB вернул одно и то же сборочное задание "
                    "в двух выбранных поставках: "
                    f"{previous_supply_id} и {supply_id}. "
                    "Расчёт остановлен, чтобы исключить двойной учёт."
                )

            order_to_supply[order_id] = supply_id

    target_ids = set(order_to_supply)

    statuses = client.order_statuses_for_ids(target_ids)

    waiting_ids = {
        order_id
        for order_id, status in statuses.items()
        if _classify_status(
            supplier_status=status.get("supplierStatus"),
            wb_status=status.get("wbStatus"),
        ) == STATUS_WAITING
    }

    waiting_orders = client.orders_for_ids(
        target_ids=waiting_ids,
        lookback_days=article_lookback_days,
    )

    missing_article_ids: list[int] = []

    supply_rows: list[dict] = []
    waiting_rows: list[dict] = []
    secondary_rows: list[dict] = []

    total_sorted = 0
    total_cancelled = 0
    total_waiting = 0
    total_other = 0

    for supply_id, supply in supply_by_id.items():
        supply_name = _make_supply_name(supply)
        cluster = extract_distribution_center(
            supply_name=supply.get("name"),
            supply_id=supply_id,
        )

        counters = {
            STATUS_SORTED: 0,
            STATUS_CANCELLED: 0,
            STATUS_WAITING: 0,
            STATUS_OTHER: 0,
        }

        for order_id in sorted(supply_order_ids[supply_id]):
            status = statuses[order_id]
            supplier_status = status.get("supplierStatus")
            wb_status = status.get("wbStatus")

            category = _classify_status(
                supplier_status=supplier_status,
                wb_status=wb_status,
            )

            counters[category] += 1

            if category == STATUS_WAITING:
                order_data = waiting_orders.get(order_id, {})

                article = str(
                    order_data.get("article") or ""
                ).strip()

                if not article:
                    article = (
                        "Не найден за выбранный период"
                    )
                    missing_article_ids.append(order_id)

                waiting_rows.append(
                    {
                        "Поставка": supply_name,
                        "ID поставки": supply_id,
                        "Кластер": cluster,
                        "Артикул продавца WB": article,
                        "ID сборочного задания": order_id,
                        "Текущий статус WB": _wb_status_text(
                            wb_status
                        ),
                        "Комментарий": _waiting_comment(
                            supplier_status
                        ),
                    }
                )

            elif category in (STATUS_CANCELLED, STATUS_OTHER):
                secondary_rows.append(
                    {
                        "Поставка": supply_name,
                        "ID поставки": supply_id,
                        "Кластер": cluster,
                        "ID сборочного задания": order_id,
                        "Категория": STATUS_LABELS[category],
                        "Текущий статус WB": _wb_status_text(
                            wb_status
                        ),
                        "Статус продавца": _supplier_status_text(
                            supplier_status
                        ),
                        "Комментарий": _status_comment(
                            category=category,
                            supplier_status=supplier_status,
                            wb_status=wb_status,
                        ),
                    }
                )

        total_sorted += counters[STATUS_SORTED]
        total_cancelled += counters[STATUS_CANCELLED]
        total_waiting += counters[STATUS_WAITING]
        total_other += counters[STATUS_OTHER]

        supply_rows.append(
            {
                "Поставка": supply_name,
                "ID поставки": supply_id,
                "Кластер": cluster,
                "Всего сборочных заданий": len(
                    supply_order_ids[supply_id]
                ),
                "Отсортировано": counters[STATUS_SORTED],
                "Отменено": counters[STATUS_CANCELLED],
                "Ждёт сортировки": counters[STATUS_WAITING],
                "Другой статус": counters[STATUS_OTHER],
            }
        )

    supply_rows.sort(
        key=lambda row: (
            -row["Ждёт сортировки"],
            row["Кластер"].casefold(),
            row["Кластер"],
            row["Поставка"].casefold(),
            row["Поставка"],
            row["ID поставки"],
        )
    )

    waiting_rows.sort(
        key=lambda row: (
            row["Кластер"].casefold(),
            row["Кластер"],
            row["Поставка"].casefold(),
            row["Поставка"],
            row["ID сборочного задания"],
        )
    )

    secondary_rows.sort(
        key=lambda row: (
            row["Категория"],
            row["Кластер"].casefold(),
            row["Кластер"],
            row["Поставка"].casefold(),
            row["Поставка"],
            row["ID сборочного задания"],
        )
    )

    waiting_supply_count = sum(
        row["Ждёт сортировки"] > 0
        for row in supply_rows
    )

    waiting_cluster_count = len(
        {
            row["Кластер"]
            for row in supply_rows
            if row["Ждёт сортировки"] > 0
        }
    )

    check_rows = [
        row
        for row in supply_rows
        if row["Ждёт сортировки"] > 0
    ]

    return {
        "generated_at": datetime.now(MOSCOW_TZ),
        "article_lookback_days": article_lookback_days,
        "selected_supply_count": len(supply_by_id),
        "empty_supply_ids": empty_supply_ids,
        "total_orders": len(target_ids),
        "total_sorted": total_sorted,
        "total_cancelled": total_cancelled,
        "total_waiting": total_waiting,
        "total_other": total_other,
        "waiting_supply_count": waiting_supply_count,
        "waiting_cluster_count": waiting_cluster_count,
        "supply_rows": supply_rows,
        "waiting_rows": waiting_rows,
        "secondary_rows": secondary_rows,
        "missing_article_ids": sorted(missing_article_ids),
        "check_rows": check_rows,
    }


def _render_supply_status_table(
    rows: list[dict],
) -> None:
    """Показывает сводную таблицу статусов по поставкам."""
    columns = [
        "Поставка",
        "ID поставки",
        "Кластер",
        "Всего сборочных заданий",
        "Отсортировано",
        "Отменено",
        "Ждёт сортировки",
        "Другой статус",
    ]

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Поставка": st.column_config.TextColumn(
                "Поставка",
                width="large",
            ),
            "ID поставки": st.column_config.TextColumn(
                "ID поставки",
                width="medium",
            ),
            "Кластер": st.column_config.TextColumn(
                "Кластер",
                width="medium",
            ),
            "Всего сборочных заданий": st.column_config.NumberColumn(
                "Всего сборочных заданий",
                format="%d",
            ),
            "Отсортировано": st.column_config.NumberColumn(
                "Отсортировано",
                format="%d",
            ),
            "Отменено": st.column_config.NumberColumn(
                "Отменено",
                format="%d",
            ),
            "Ждёт сортировки": st.column_config.NumberColumn(
                "Ждёт сортировки",
                format="%d",
            ),
            "Другой статус": st.column_config.NumberColumn(
                "Другой статус",
                format="%d",
            ),
        },
    )


def _render_waiting_table(
    rows: list[dict],
) -> None:
    """Показывает детализацию заданий со статусом `waiting`."""
    columns = [
        "Поставка",
        "ID поставки",
        "Кластер",
        "Артикул продавца WB",
        "ID сборочного задания",
        "Текущий статус WB",
        "Комментарий",
    ]

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Поставка": st.column_config.TextColumn(
                "Поставка",
                width="large",
            ),
            "ID поставки": st.column_config.TextColumn(
                "ID поставки",
                width="medium",
            ),
            "Кластер": st.column_config.TextColumn(
                "Кластер",
                width="medium",
            ),
            "Артикул продавца WB": st.column_config.TextColumn(
                "Артикул продавца WB",
                width="large",
            ),
            "ID сборочного задания": st.column_config.NumberColumn(
                "ID сборочного задания",
                format="%d",
            ),
            "Текущий статус WB": st.column_config.TextColumn(
                "Текущий статус WB",
                width="medium",
            ),
            "Комментарий": st.column_config.TextColumn(
                "Комментарий",
                width="large",
            ),
        },
    )


def _render_secondary_status_table(
    rows: list[dict],
) -> None:
    """Показывает отменённые и остальные статусы."""
    columns = [
        "Поставка",
        "ID поставки",
        "Кластер",
        "ID сборочного задания",
        "Категория",
        "Текущий статус WB",
        "Статус продавца",
        "Комментарий",
    ]

    table = pd.DataFrame(rows, columns=columns)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Поставка": st.column_config.TextColumn(
                "Поставка",
                width="large",
            ),
            "ID поставки": st.column_config.TextColumn(
                "ID поставки",
                width="medium",
            ),
            "Кластер": st.column_config.TextColumn(
                "Кластер",
                width="medium",
            ),
            "ID сборочного задания": st.column_config.NumberColumn(
                "ID сборочного задания",
                format="%d",
            ),
            "Категория": st.column_config.TextColumn(
                "Категория",
                width="medium",
            ),
            "Текущий статус WB": st.column_config.TextColumn(
                "Текущий статус WB",
                width="medium",
            ),
            "Статус продавца": st.column_config.TextColumn(
                "Статус продавца",
                width="medium",
            ),
            "Комментарий": st.column_config.TextColumn(
                "Комментарий",
                width="large",
            ),
        },
    )


def _render_check_list(
    check_rows: list[dict],
) -> None:
    """Показывает короткий список поставок, требующих проверки."""
    st.subheader("⚠️ Проверить")

    for index, row in enumerate(check_rows[:20], start=1):
        count = row["Ждёт сортировки"]
        noun = "задание ждёт" if count == 1 else "заданий ждут"

        st.markdown(
            f"{index}. **{row['Кластер']}** — "
            f"`{row['Поставка']}` — "
            f"**{count} {noun} сортировки**."
        )

    if len(check_rows) > 20:
        st.caption(
            "Показаны первые 20 поставок с ожидающими сортировки "
            "заданиями. Полная детализация находится в таблице ниже."
        )


def _render_result(
    result: dict,
) -> None:
    """Отображает готовый результат проверки статусов."""
    st.divider()
    st.subheader("📊 Общая сводка")

    metric_1, metric_2, metric_3 = st.columns(3)

    metric_1.metric(
        "Выбрано поставок",
        result["selected_supply_count"],
    )

    metric_2.metric(
        "Всего сборочных заданий",
        result["total_orders"],
    )

    metric_3.metric(
        "Отсортировано",
        result["total_sorted"],
    )

    metric_4, metric_5, metric_6 = st.columns(3)

    metric_4.metric(
        "Отменено",
        result["total_cancelled"],
    )

    metric_5.metric(
        "Ждёт сортировки",
        result["total_waiting"],
    )

    metric_6.metric(
        "Поставок / кластеров с ожиданием",
        (
            f"{result['waiting_supply_count']} / "
            f"{result['waiting_cluster_count']}"
        ),
    )

    st.caption(
        "Статусы сформированы: "
        f"{result['generated_at'].strftime('%d.%m.%Y %H:%M')} "
        "по Москве."
    )

    st.caption(
        "WB предоставляет статусы на уровне сборочного задания. "
        "Показатели не являются количеством единиц товара: "
        "используемые read-only методы WB не возвращают подтверждённый "
        "состав и количество товарных строк задания."
    )

    if result["empty_supply_ids"]:
        st.warning(
            "В выбранных поставках не найдено сборочных заданий: "
            + ", ".join(result["empty_supply_ids"])
            + "."
        )

    if result["total_waiting"] > 0:
        st.error(
            "Найдены задания, которые WB ещё не получил после "
            "подтверждения продавцом. Проверьте физическую отгрузку, "
            "маршрут и факт приёмки на сортировке."
        )
    else:
        st.success(
            "Заданий со статусом `waiting` не найдено: среди "
            "проверенных поставок нет заданий, ожидающих сортировки."
        )

    st.divider()
    st.subheader("🚚 Статусы по поставкам и кластерам")

    _render_supply_status_table(result["supply_rows"])

    if result["total_waiting"] > 0:
        st.divider()
        _render_check_list(result["check_rows"])

        st.divider()
        st.subheader("🔎 Ждут сортировки")

        st.caption(
            "Одна строка — одно сборочное задание WB. "
            "Артикул запрашивается только для проблемных заданий "
            f"за последние {result['article_lookback_days']} дней."
        )

        if result["missing_article_ids"]:
            st.warning(
                "WB не вернул артикул для "
                f"{len(result['missing_article_ids'])} "
                "ожидающих сортировки заданий за выбранный период. "
                "Сводка статусов сохранена; в таблице такие строки "
                "помечены как «Не найден за выбранный период». "
                "При необходимости повторите проверку с периодом "
                "14 или 31 день."
            )

        _render_waiting_table(result["waiting_rows"])

    if result["secondary_rows"]:
        st.divider()

        with st.expander(
            "Отменённые и другие статусы WB",
            expanded=False,
        ):
            st.caption(
                "В «Другой статус» входят официальные статусы WB, "
                "которые не равны точному `sorted`, `waiting` "
                "и не относятся к отмене."
            )

            _render_secondary_status_table(
                result["secondary_rows"]
            )


def _supply_label_or_missing(
    supply_id: str,
    supply_by_id: dict[str, dict],
) -> str:
    """Формирует подпись, сохраняя ранее выбранную исчезнувшую поставку."""
    supply = supply_by_id.get(supply_id)

    if supply is None:
        return (
            f"Поставка {supply_id} | больше не найдена "
            "в последнем ответе WB"
        )

    return _format_supply_label(supply)


def render_wb_shipment_status_tab(
    client: WBClient,
) -> None:
    """
    Отображает вкладку контроля статусов завершённых WB FBS-поставок.

    Все запросы read-only. Поставка, задание, статус и документы
    не создаются, не изменяются, не отменяются и не закрываются.
    """
    st.title("🚚 Статус отгрузок WB по кластерам")

    st.markdown(
        """
        <div class="description-box">
            Выберите завершённые WB FBS-поставки и получите актуальные
            статусы их сборочных заданий. Направление определяется из
            названия поставки: например, «Накл Ростов от 07.10.2026»
            → «Ростов».
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Контроль выполняется по уникальным ID сборочных заданий. "
        "Статус «Ждёт сортировки» соответствует точному "
        "`wbStatus = waiting`: продавец подтвердил задание, "
        "но WB ещё не получил его."
    )

    load_col, info_col = st.columns([1.2, 2.8])

    with load_col:
        load_clicked = st.button(
            "⟳ Загрузить / обновить завершённые поставки",
            use_container_width=True,
            key="load_shipment_status_supplies",
        )

    with info_col:
        st.caption(
            "Загружается read-only список поставок WB. "
            "После обновления ранее выбранные поставки сохраняются "
            "в списке, даже если текущий фильтр их скрывает."
        )

    if load_clicked:
        st.session_state["shipment_status_supplies"] = None
        st.session_state["shipment_status_result"] = None
        st.session_state[
            "shipment_status_supply_widget_version"
        ] += 1

        try:
            with st.spinner(
                "Загружаю завершённые поставки WB…"
            ):
                st.session_state[
                    "shipment_status_supplies"
                ] = client.list_supplies()
        except WBApiError as error:
            st.error(str(error))

    supplies = st.session_state.get(
        "shipment_status_supplies"
    )

    if supplies is None:
        st.info(
            "Нажмите «Загрузить / обновить завершённые поставки»."
        )
        return

    if not supplies:
        st.warning("WB не вернул доступных поставок.")
        return

    supply_by_id: dict[str, dict] = {}

    for supply in supplies:
        if not isinstance(supply, dict):
            continue

        supply_id = str(supply.get("id") or "").strip()

        if not supply_id:
            continue

        supply_by_id[supply_id] = supply

    if not supply_by_id:
        st.error(
            "WB вернул список поставок без корректных идентификаторов."
        )
        return

    st.divider()
    st.subheader("Фильтры завершённых поставок")

    today = datetime.now(MOSCOW_TZ).date()
    default_date_from = today - timedelta(days=13)

    date_col, search_col = st.columns([1.4, 1.8])

    with date_col:
        selected_date_range = st.date_input(
            "Дата создания поставки",
            value=(default_date_from, today),
            format="DD.MM.YYYY",
            key="shipment_status_date_range",
            help=(
                "Фильтруется WB `createdAt` в часовом поясе "
                "Europe/Moscow. По умолчанию показаны последние "
                "14 календарных дней, включая сегодня."
            ),
        )

    with search_col:
        supply_search = st.text_input(
            "Поиск поставки",
            placeholder="Введите часть названия или ID",
            key="shipment_status_supply_search",
        )

    date_from, date_to = _parse_date_range(
        selected_date_range
    )

    visible_ids = [
        supply_id
        for supply_id, supply in supply_by_id.items()
        if _matches_completed_supply_filters(
            supply=supply,
            search_text=supply_search,
            date_from=date_from,
            date_to=date_to,
        )
    ]

    visible_ids = _sort_supply_ids(
        supply_ids=visible_ids,
        supply_by_id=supply_by_id,
    )

    st.caption(
        "Дата создания поставки: "
        f"{_format_date_range(date_from, date_to)}. "
        f"Найдено завершённых поставок: {len(visible_ids)}."
    )

    widget_key = (
        "shipment_status_supplies_"
        f"{st.session_state['shipment_status_supply_widget_version']}"
    )

    saved_selected_ids = st.session_state.get(widget_key, [])

    if not isinstance(saved_selected_ids, list):
        saved_selected_ids = []

    option_ids = list(
        dict.fromkeys(
            [
                *saved_selected_ids,
                *visible_ids,
            ]
        )
    )

    st.divider()
    st.subheader("Выбор поставок")

    selected_ids = st.multiselect(
        "Найдите завершённые поставки по названию или ID",
        options=option_ids,
        format_func=lambda supply_id: _supply_label_or_missing(
            supply_id=supply_id,
            supply_by_id=supply_by_id,
        ),
        key=widget_key,
        placeholder="Выберите одну или несколько поставок",
    )

    clear_col, _ = st.columns([1, 3])

    with clear_col:
        clear_clicked = st.button(
            "🧹 Очистить выбор",
            use_container_width=True,
            key="clear_shipment_status_selection",
        )

    if clear_clicked:
        st.session_state["shipment_status_result"] = None
        st.session_state[
            "shipment_status_supply_widget_version"
        ] += 1
        st.rerun()

    all_selected_ids = list(dict.fromkeys(selected_ids))

    unavailable_selected_ids = [
        supply_id
        for supply_id in all_selected_ids
        if supply_id not in supply_by_id
    ]

    if unavailable_selected_ids:
        st.warning(
            "Следующие ранее выбранные поставки отсутствуют "
            "в последнем ответе WB: "
            + ", ".join(unavailable_selected_ids)
            + ". Снимите их с выбора или обновите список позже."
        )

    if all_selected_ids:
        st.success(
            f"Выбрано поставок: {len(all_selected_ids)}."
        )

    st.divider()
    st.subheader("Параметры поиска проблемных артикулов")

    article_lookback_days = st.selectbox(
        "Период поиска article для заданий «Ждёт сортировки»",
        options=[7, 14, 31],
        index=0,
        format_func=lambda days: f"Последние {days} дней",
        key="shipment_status_article_lookback_days",
        help=(
            "Артикулы запрашиваются только у заданий с точным "
            "`wbStatus = waiting`. Статусы всех выбранных заданий "
            "получаются независимо от этого периода."
        ),
    )

    selected_supplies = [
        supply_by_id[supply_id]
        for supply_id in all_selected_ids
        if supply_id in supply_by_id
    ]

    result_signature = (
        tuple(sorted(all_selected_ids)),
        article_lookback_days,
    )

    saved_result = st.session_state.get(
        "shipment_status_result"
    )

    if (
        saved_result is not None
        and saved_result.get("signature") != result_signature
    ):
        st.session_state["shipment_status_result"] = None
        saved_result = None

    st.markdown("<br>", unsafe_allow_html=True)

    update_clicked = st.button(
        "🔄 Обновить статусы отгрузок",
        type="primary",
        use_container_width=True,
        key="update_wb_shipment_statuses",
        disabled=(
            not selected_supplies
            or bool(unavailable_selected_ids)
        ),
    )

    if update_clicked:
        st.session_state["shipment_status_result"] = None

        try:
            with st.spinner(
                "Получаю ID заданий поставок, актуальные статусы WB "
                "и артикулы только для заданий, ожидающих сортировки…"
            ):
                result = build_shipment_status_result(
                    client=client,
                    selected_supplies=selected_supplies,
                    article_lookback_days=article_lookback_days,
                )

            st.session_state["shipment_status_result"] = {
                "signature": result_signature,
                "result": result,
            }

        except (WBApiError, ValueError) as error:
            st.error(str(error))

    saved_result = st.session_state.get(
        "shipment_status_result"
    )

    if (
        saved_result is None
        or saved_result.get("signature") != result_signature
    ):
        st.info(
            "Выберите завершённые поставки и нажмите "
            "«Обновить статусы отгрузок»."
        )
        return

    _render_result(saved_result["result"])