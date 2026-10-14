from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

import pandas as pd
import streamlit as st

from ozon_api import (
    MOSCOW_TZ,
    OzonApiError,
    OzonClient,
    OzonFbsStickerPosting,
)
from ozon_mysklad_export import make_ozon_mysklad_xlsx
from ozon_pdf_export import make_ozon_grouped_labels_pdf


OZON_FBS_OFFER_PREFIX = "NAKL_"

PRINT_ORDER_OPTIONS = {
    "Для рулона — обратный порядок": True,
    "Обычный порядок": False,
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
    date_from: date | None,
    date_to: date | None,
) -> str:
    """Форматирует выбранный период."""
    if date_from is None or date_to is None:
        return "Дата не выбрана"

    if date_from == date_to:
        return date_from.strftime("%d.%m.%Y")

    return (
        f"{date_from.strftime('%d.%m.%Y')} — "
        f"{date_to.strftime('%d.%m.%Y')}"
    )


def _format_posting_label(
    posting: OzonFbsStickerPosting,
) -> str:
    """Формирует подпись Ozon FBS-отправления для списка."""
    articles = ", ".join(posting.offer_ids)

    return (
        f"{posting.posting_number}  |  "
        f"{posting.in_process_at.strftime('%d.%m.%Y %H:%M')}  |  "
        f"{articles}  |  "
        f"{posting.total_quantity} шт."
    )


def _safe_posting_label_or_missing(
    posting_number: str,
    postings_by_number: dict[str, OzonFbsStickerPosting],
) -> str:
    """Сохраняет временно скрытое отправление в мультивыборе."""
    posting = postings_by_number.get(posting_number)

    if posting is None:
        return (
            f"{posting_number} | больше не найдено "
            "в последнем ответе Ozon"
        )

    return _format_posting_label(posting)


def _posting_matches_search(
    posting: OzonFbsStickerPosting,
    search_text: str,
) -> bool:
    """Фильтрует отправление локально без новых API-запросов."""
    normalized_search = search_text.strip().casefold()

    if not normalized_search:
        return True

    if normalized_search in posting.posting_number.casefold():
        return True

    return any(
        normalized_search in offer_id.casefold()
        for offer_id in posting.offer_ids
    )


def _build_article_summary(
    postings: list[OzonFbsStickerPosting],
) -> list[dict[str, Any]]:
    """
    Суммирует quantity NAKL_-товаров по offer_id.

    Одно отправление может содержать несколько одинаковых или разных
    артикулов. Количество отправлений у артикула считается по уникальным
    номерам отправлений, а количество товаров — суммой quantity.
    """
    grouped: dict[str, dict[str, Any]] = {}

    for posting in postings:
        for offer_id, quantity in posting.offer_quantities.items():
            row = grouped.setdefault(
                offer_id,
                {
                    "Артикул Ozon": offer_id,
                    "Количество товаров": 0,
                    "Отправлений": set(),
                },
            )

            row["Количество товаров"] += quantity
            row["Отправлений"].add(posting.posting_number)

    result = [
        {
            "Артикул Ozon": row["Артикул Ozon"],
            "Количество товаров": row["Количество товаров"],
            "Количество отправлений": len(row["Отправлений"]),
        }
        for row in grouped.values()
    ]

    return sorted(
        result,
        key=lambda row: (
            -row["Количество товаров"],
            row["Артикул Ozon"].casefold(),
            row["Артикул Ozon"],
        ),
    )


def _build_selected_posting_rows(
    postings: list[OzonFbsStickerPosting],
) -> list[dict[str, Any]]:
    """Создаёт таблицу выбранных Ozon-отправлений."""
    rows: list[dict[str, Any]] = []

    for posting in postings:
        rows.append(
            {
                "Номер отправления Ozon": posting.posting_number,
                "Поступило в работу": posting.in_process_at.strftime(
                    "%d.%m.%Y %H:%M"
                ),
                "Артикулы NAKL_": ", ".join(posting.offer_ids),
                "Количество товаров NAKL_": posting.total_quantity,
            }
        )

    return rows


def _build_print_groups(
    postings: list[OzonFbsStickerPosting],
    label_pdf_by_posting_number: dict[str, bytes],
) -> list[dict[str, Any]]:
    """
    Строит группы для итогового PDF.

    Для однотоварного отправления:

    ```text
    Задание артикула с общим количеством
    → оригинальные QR-этикетки соответствующих отправлений
    ```

    Если в одном отправлении несколько NAKL_-артикулов, QR-этикетка
    не дублируется. Перед ней печатается отдельное задание со списком
    артикулов и количеством каждого.
    """
    single_article_postings: dict[
        str,
        list[OzonFbsStickerPosting],
    ] = defaultdict(list)

    multi_article_postings: list[OzonFbsStickerPosting] = []

    for posting in postings:
        if len(posting.offer_quantities) == 1:
            article = next(iter(posting.offer_quantities))
            single_article_postings[article].append(posting)
        else:
            multi_article_postings.append(posting)

    groups: list[dict[str, Any]] = []

    for article in sorted(
        single_article_postings,
        key=lambda value: (value.casefold(), value),
    ):
        article_postings = sorted(
            single_article_postings[article],
            key=lambda posting: (
                posting.in_process_at,
                posting.posting_number,
            ),
        )

        label_pdfs: list[bytes] = []

        for posting in article_postings:
            label_pdf = label_pdf_by_posting_number.get(
                posting.posting_number
            )

            if not label_pdf:
                raise ValueError(
                    "Не найдена QR-этикетка Ozon для отправления "
                    f"`{posting.posting_number}`."
                )

            label_pdfs.append(label_pdf)

        groups.append(
            {
                "title": article,
                "items": [
                    (
                        article,
                        sum(
                            posting.offer_quantities[article]
                            for posting in article_postings
                        ),
                    )
                ],
                "posting_count": len(article_postings),
                "label_pdfs": label_pdfs,
            }
        )

    for posting in sorted(
        multi_article_postings,
        key=lambda value: (
            value.in_process_at,
            value.posting_number,
        ),
    ):
        label_pdf = label_pdf_by_posting_number.get(
            posting.posting_number
        )

        if not label_pdf:
            raise ValueError(
                "Не найдена QR-этикетка Ozon для отправления "
                f"`{posting.posting_number}`."
            )

        items = sorted(
            posting.offer_quantities.items(),
            key=lambda item: (
                item[0].casefold(),
                item[0],
            ),
        )

        groups.append(
            {
                "title": (
                    "OZON FBS — "
                    f"{posting.posting_number}"
                ),
                "items": items,
                "posting_count": 1,
                "label_pdfs": [label_pdf],
            }
        )

    if not groups:
        raise ValueError(
            "Не удалось создать группы печати Ozon."
        )

    return groups


def _render_article_summary(
    rows: list[dict[str, Any]],
) -> None:
    """Показывает сводку выбранных отправлений по offer_id."""
    table = pd.DataFrame(
        rows,
        columns=[
            "Артикул Ozon",
            "Количество товаров",
            "Количество отправлений",
        ],
    )

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Артикул Ozon": st.column_config.TextColumn(
                "Артикул Ozon",
                width="large",
            ),
            "Количество товаров": st.column_config.NumberColumn(
                "Количество товаров",
                format="%d",
            ),
            "Количество отправлений": st.column_config.NumberColumn(
                "Количество отправлений",
                format="%d",
            ),
        },
    )


def _render_selected_postings(
    rows: list[dict[str, Any]],
) -> None:
    """Показывает отправления, вошедшие в печать."""
    table = pd.DataFrame(
        rows,
        columns=[
            "Номер отправления Ozon",
            "Поступило в работу",
            "Артикулы NAKL_",
            "Количество товаров NAKL_",
        ],
    )

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Номер отправления Ozon": st.column_config.TextColumn(
                "Номер отправления Ozon",
                width="large",
            ),
            "Поступило в работу": st.column_config.TextColumn(
                "Поступило в работу",
                width="medium",
            ),
            "Артикулы NAKL_": st.column_config.TextColumn(
                "Артикулы NAKL_",
                width="large",
            ),
            "Количество товаров NAKL_": st.column_config.NumberColumn(
                "Количество товаров NAKL_",
                format="%d",
            ),
        },
    )


def _init_state() -> None:
    """Инициализирует состояние вкладки Ozon FBS."""
    defaults = {
        "ozon_fbs_sticker_postings": None,
        "ozon_fbs_sticker_result": None,
        "ozon_fbs_sticker_widget_version": 0,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _get_ozon_credentials() -> tuple[str, str]:
    """Безопасно читает Ozon credentials из Streamlit Secrets."""
    try:
        client_id = str(
            st.secrets["OZON_CLIENT_ID"]
        ).strip()
    except Exception:
        client_id = ""

    try:
        api_key = str(
            st.secrets["OZON_API_KEY"]
        ).strip()
    except Exception:
        api_key = ""

    return client_id, api_key


def render_ozon_fbs_stickers_tab() -> None:
    """
    Отображает вкладку оригинальных Ozon QR-этикеток.

    Работает только с уже собранными FBS-отправлениями в статусе
    `awaiting_deliver`. Статусы Ozon не изменяются.
    """
    _init_state()

    st.title("🏷️ Стикеры FBS OZON")

    st.markdown(
        """
        <div class="description-box">
            После ручного нажатия «Собрать» в кабинете Ozon выберите
            FBS-отправления в статусе «Ожидает отгрузки». Приложение
            напечатает перед QR-этикетками задания с артикулом и
            количеством товаров, а также подготовит XLSX для МойСклад.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "В список попадают только FBS-отправления Ozon с точным "
        "статусом `awaiting_deliver` и товарами, у которых offer_id "
        f"начинается с `{OZON_FBS_OFFER_PREFIX}`."
    )

    st.caption(
        "Все действия read-only: приложение не подтверждает сборку, "
        "не меняет статусы и не отменяет отправления Ozon."
    )

    ozon_client_id, ozon_api_key = _get_ozon_credentials()

    if not ozon_client_id or not ozon_api_key:
        missing = []

        if not ozon_client_id:
            missing.append("`OZON_CLIENT_ID`")

        if not ozon_api_key:
            missing.append("`OZON_API_KEY`")

        st.error(
            "Для печати Ozon FBS-этикеток не настроены Secrets: "
            + ", ".join(missing)
        )
        return

    st.subheader("Фильтры FBS-отправлений")

    today = datetime.now(MOSCOW_TZ).date()

    date_col, load_col = st.columns([2, 1])

    with date_col:
        selected_date_range = st.date_input(
            "Дата поступления отправления в работу",
            value=(today, today),
            format="DD.MM.YYYY",
            key="ozon_fbs_sticker_date_range",
            help=(
                "Фильтруется поле Ozon `in_process_at` "
                "в часовом поясе Europe/Moscow."
            ),
        )

    with load_col:
        st.markdown("<br>", unsafe_allow_html=True)

        load_clicked = st.button(
            "⟳ Загрузить отправления Ozon",
            use_container_width=True,
            key="load_ozon_fbs_sticker_postings",
        )

    date_from, date_to = _parse_date_range(
        selected_date_range
    )

    if date_from is None or date_to is None:
        st.warning(
            "Выберите одну дату или полный диапазон дат."
        )
        return

    if load_clicked:
        st.session_state["ozon_fbs_sticker_postings"] = None
        st.session_state["ozon_fbs_sticker_result"] = None
        st.session_state[
            "ozon_fbs_sticker_widget_version"
        ] += 1

        client = OzonClient(
            client_id=ozon_client_id,
            api_key=ozon_api_key,
        )

        try:
            with st.spinner(
                "Получаю FBS-отправления Ozon в статусе "
                "«Ожидает отгрузки»…"
            ):
                postings = client.awaiting_deliver_nakl_postings(
                    date_from=date_from,
                    date_to=date_to,
                    offer_id_prefix=OZON_FBS_OFFER_PREFIX,
                )

            st.session_state[
                "ozon_fbs_sticker_postings"
            ] = postings

            st.success(
                "Список FBS-отправлений Ozon успешно обновлён."
            )

        except (OzonApiError, ValueError) as error:
            st.error(str(error))

    postings = st.session_state.get(
        "ozon_fbs_sticker_postings"
    )

    if postings is None:
        st.info(
            "Выберите дату и нажмите «Загрузить отправления Ozon»."
        )
        return

    if not postings:
        st.warning(
            "За выбранный период не найдено FBS-отправлений Ozon "
            "в статусе «Ожидает отгрузки» с товарами `NAKL_`."
        )
        return

    postings_by_number = {
        posting.posting_number: posting
        for posting in postings
    }

    st.caption(
        "Дата `in_process_at`: "
        f"**{_format_date_range(date_from, date_to)}**. "
        f"Найдено отправлений: **{len(postings_by_number)}**."
    )

    search_text = st.text_input(
        "Поиск отправления или артикула",
        placeholder=(
            "Номер отправления Ozon или артикул, "
            "например NAKL_KOTY_PINK_75"
        ),
        key="ozon_fbs_sticker_search",
    )

    visible_numbers = [
        posting.posting_number
        for posting in postings
        if _posting_matches_search(
            posting=posting,
            search_text=search_text,
        )
    ]

    st.caption(
        f"Найдено по текущему поиску: {len(visible_numbers)}."
    )

    widget_key = (
        "ozon_fbs_sticker_selected_postings_"
        f"{st.session_state['ozon_fbs_sticker_widget_version']}"
    )

    saved_selected_numbers = st.session_state.get(
        widget_key,
        [],
    )

    if not isinstance(saved_selected_numbers, list):
        saved_selected_numbers = []

    option_numbers = list(
        dict.fromkeys(
            [
                *saved_selected_numbers,
                *visible_numbers,
            ]
        )
    )

    st.divider()
    st.subheader("Выбор отправлений")

    selected_numbers = st.multiselect(
        "Найдите FBS-отправления и выберите нужные",
        options=option_numbers,
        format_func=lambda posting_number: (
            _safe_posting_label_or_missing(
                posting_number=posting_number,
                postings_by_number=postings_by_number,
            )
        ),
        key=widget_key,
        placeholder="Выберите одно или несколько отправлений",
    )

    clear_col, _ = st.columns([1, 3])

    with clear_col:
        clear_clicked = st.button(
            "🧹 Очистить выбор",
            use_container_width=True,
            key="clear_ozon_fbs_sticker_selection",
        )

    if clear_clicked:
        st.session_state["ozon_fbs_sticker_result"] = None
        st.session_state[
            "ozon_fbs_sticker_widget_version"
        ] += 1
        st.rerun()

    unique_selected_numbers = list(
        dict.fromkeys(selected_numbers)
    )

    missing_selected_numbers = [
        posting_number
        for posting_number in unique_selected_numbers
        if posting_number not in postings_by_number
    ]

    if missing_selected_numbers:
        st.warning(
            "Некоторые ранее выбранные отправления отсутствуют "
            "в последнем ответе Ozon. Снимите их с выбора либо "
            "обновите список: "
            + ", ".join(missing_selected_numbers)
        )

    selected_postings = [
        postings_by_number[posting_number]
        for posting_number in unique_selected_numbers
        if posting_number in postings_by_number
    ]

    if selected_postings:
        total_quantity = sum(
            posting.total_quantity
            for posting in selected_postings
        )

        unique_articles = {
            article
            for posting in selected_postings
            for article in posting.offer_ids
        }

        metric_1, metric_2, metric_3 = st.columns(3)

        metric_1.metric(
            "Выбрано отправлений",
            len(selected_postings),
        )

        metric_2.metric(
            "Товаров NAKL_",
            total_quantity,
        )

        metric_3.metric(
            "Уникальных артикулов",
            len(unique_articles),
        )

    st.divider()
    st.subheader("Параметры печати")

    print_order_label = st.selectbox(
        "Порядок печати",
        options=list(PRINT_ORDER_OPTIONS.keys()),
        index=0,
        key="ozon_fbs_print_order",
        help=(
            "Для рулона PDF-страницы разворачиваются. После размотки "
            "ленты задания и соответствующие QR-этикетки Ozon будут "
            "идти в обычном рабочем порядке."
        ),
    )

    reverse_print_order = PRINT_ORDER_OPTIONS[
        print_order_label
    ]

    result_signature = (
        tuple(sorted(unique_selected_numbers)),
        reverse_print_order,
    )

    saved_result = st.session_state.get(
        "ozon_fbs_sticker_result"
    )

    if (
        saved_result is not None
        and saved_result.get("signature") != result_signature
    ):
        st.session_state["ozon_fbs_sticker_result"] = None
        saved_result = None

    st.markdown("<br>", unsafe_allow_html=True)

    generate_clicked = st.button(
        "🖨️ Подготовить задания, QR-этикетки и XLSX",
        type="primary",
        use_container_width=True,
        key="generate_ozon_fbs_sticker_pdf",
        disabled=(
            not selected_postings
            or bool(missing_selected_numbers)
        ),
        help=(
            "Для точного сопоставления служебного задания с QR-кодом "
            "этикетки Ozon запрашиваются по одному отправлению. "
            "Если Ozon ещё не подготовил этикетки, повторите запрос "
            "через 45–60 секунд."
        ),
    )

    if generate_clicked:
        st.session_state["ozon_fbs_sticker_result"] = None

        client = OzonClient(
            client_id=ozon_client_id,
            api_key=ozon_api_key,
        )

        try:
            with st.spinner(
                "Получаю оригинальные QR-этикетки Ozon, создаю "
                "служебные задания и XLSX…"
            ):
                label_pdf_by_posting_number: dict[str, bytes] = {}

                for posting in selected_postings:
                    pdf_parts = client.package_labels_pdf(
                        posting_numbers=[
                            posting.posting_number
                        ]
                    )

                    if len(pdf_parts) != 1:
                        raise OzonApiError(
                            "Ozon вернул неожиданный набор PDF-этикеток "
                            "для одного FBS-отправления. Неполный файл "
                            "не сформирован."
                        )

                    label_pdf_by_posting_number[
                        posting.posting_number
                    ] = pdf_parts[0]

                print_groups = _build_print_groups(
                    postings=selected_postings,
                    label_pdf_by_posting_number=(
                        label_pdf_by_posting_number
                    ),
                )

                full_pdf = make_ozon_grouped_labels_pdf(
                    groups=print_groups,
                    reverse_page_order=reverse_print_order,
                )

                article_summary = _build_article_summary(
                    postings=selected_postings
                )

                mysklad_xlsx = make_ozon_mysklad_xlsx(
                    article_rows=article_summary
                )

                selected_posting_rows = (
                    _build_selected_posting_rows(
                        postings=selected_postings
                    )
                )

            st.session_state["ozon_fbs_sticker_result"] = {
                "signature": result_signature,
                "generated_at": datetime.now(MOSCOW_TZ),
                "full_pdf": full_pdf,
                "mysklad_xlsx": mysklad_xlsx,
                "article_summary": article_summary,
                "selected_posting_rows": selected_posting_rows,
                "posting_count": len(selected_postings),
                "article_count": len(article_summary),
                "total_quantity": sum(
                    posting.total_quantity
                    for posting in selected_postings
                ),
                "group_count": len(print_groups),
                "reverse_print_order": reverse_print_order,
            }

            st.success(
                "Задания, оригинальные QR-этикетки Ozon и XLSX готовы."
            )

        except (OzonApiError, ValueError) as error:
            st.error(str(error))

    saved_result = st.session_state.get(
        "ozon_fbs_sticker_result"
    )

    if (
        saved_result is None
        or saved_result.get("signature") != result_signature
    ):
        st.info(
            "Выберите FBS-отправления в статусе "
            "«Ожидает отгрузки» и подготовьте задания с QR-этикетками."
        )
        return

    st.divider()
    st.subheader("📊 Сводка выбранных отправлений")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric(
        "Отправлений",
        saved_result["posting_count"],
    )

    metric_2.metric(
        "Товаров NAKL_",
        saved_result["total_quantity"],
    )

    metric_3.metric(
        "Уникальных артикулов",
        saved_result["article_count"],
    )

    metric_4.metric(
        "Печатных заданий",
        saved_result["group_count"],
    )

    st.caption(
        "Файлы сформированы: "
        f"{saved_result['generated_at'].strftime('%d.%m.%Y %H:%M')} "
        "по Москве."
    )

    if saved_result["reverse_print_order"]:
        order_caption = (
            "PDF сформирован в обратном порядке страниц для рулонной "
            "печати. При размотке ленты задания и QR-этикетки будут "
            "идти в рабочем порядке."
        )
    else:
        order_caption = (
            "PDF сформирован в обычном логическом порядке страниц."
        )

    st.caption(order_caption)

    st.subheader("Артикулы NAKL_ в выбранных отправлениях")

    _render_article_summary(
        rows=saved_result["article_summary"]
    )

    st.subheader("Отправления, вошедшие в печать")

    _render_selected_postings(
        rows=saved_result["selected_posting_rows"]
    )

    st.divider()
    st.subheader("📥 PDF и XLSX для МойСклад")

    st.caption(
        "В PDF перед каждой группой QR-этикеток печатается задание "
        "с артикулом и количеством товаров. Оригинальные QR-этикетки "
        "Ozon не перерисовываются и не изменяются."
    )

    timestamp = saved_result["generated_at"].strftime(
        "%Y%m%d_%H%M%S"
    )

    pdf_col, xlsx_col = st.columns(2)

    with pdf_col:
        st.download_button(
            label="📥 Скачать PDF: задания и QR-этикетки Ozon",
            data=saved_result["full_pdf"],
            file_name=(
                f"ozon_fbs_tasks_and_qr_labels_{timestamp}.pdf"
            ),
            mime="application/pdf",
            use_container_width=True,
        )

    with xlsx_col:
        st.download_button(
            label="📊 Скачать XLSX для МойСклад",
            data=saved_result["mysklad_xlsx"],
            file_name=f"ozon_fbs_mysklad_{timestamp}.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            use_container_width=True,
        )

    st.caption(
        "XLSX содержит две колонки: `Артикул` и `Количество`. "
        "Количество — сумма товаров NAKL_ по всем выбранным "
        "FBS-отправлениям Ozon."
    )

    st.warning(
        "Перед рабочей печатью выполните пробу на одной группе. "
        "В окне печати выберите масштаб 100% / «Фактический размер» "
        "и отключите автоматическое подгоняние по странице."
    )