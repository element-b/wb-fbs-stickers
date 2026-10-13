from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from typing import Any

import pandas as pd
import streamlit as st
from pypdf import PdfReader, PdfWriter

from ozon_api import (
    AWAITING_DELIVER_STATUS,
    MOSCOW_TZ,
    OzonApiError,
    OzonClient,
    OzonFbsStickerPosting,
)


OZON_FBS_OFFER_PREFIX = "NAKL_"


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
    """Разбирает одиночную дату либо диапазон из st.date_input."""
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
    """Форматирует выбранный период для интерфейса."""
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
    """Создаёт подпись FBS-отправления в списке выбора."""
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
    """Сохраняет ранее выбранное отправление в мультивыборе."""
    posting = postings_by_number.get(posting_number)

    if posting is None:
        return (
            f"{posting_number} | больше не найдено в последнем "
            "ответе Ozon"
        )

    return _format_posting_label(posting)


def _posting_matches_search(
    posting: OzonFbsStickerPosting,
    search_text: str,
) -> bool:
    """Фильтрует отправление локально без API-запросов."""
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
    Группирует выбранные отправления по NAKL_-артикулам.

    Одно FBS-отправление может содержать несколько NAKL_-товаров.
    Поэтому количество отправлений по артикулу считается по уникальному
    набору номеров отправлений, а количество товаров — суммой quantity.
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
    """Готовит безопасную таблицу выбранных отправлений."""
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


def _merge_ozon_label_pdfs(
    pdf_parts: list[bytes],
) -> bytes:
    """
    Объединяет оригинальные PDF-этикетки Ozon в один PDF.

    Страницы Ozon не растеризуются, не масштабируются, не обрезаются
    и не перерисовываются. В итоговый файл копируются исходные страницы.
    """
    if not pdf_parts:
        raise ValueError(
            "Ozon не вернул ни одного PDF-файла с этикетками."
        )

    writer = PdfWriter()
    source_streams: list[BytesIO] = []

    try:
        for pdf_bytes in pdf_parts:
            if not isinstance(pdf_bytes, bytes):
                raise ValueError(
                    "Ozon вернул некорректное содержимое PDF-этикетки."
                )

            if not pdf_bytes.startswith(b"%PDF-"):
                raise ValueError(
                    "Ozon вернул этикетку не в формате PDF."
                )

            source = BytesIO(pdf_bytes)
            source_streams.append(source)

            try:
                reader = PdfReader(
                    source,
                    strict=False,
                )
            except Exception as error:
                raise ValueError(
                    "Не удалось прочитать PDF-этикетку Ozon."
                ) from error

            if reader.is_encrypted:
                raise ValueError(
                    "Ozon вернул защищённый PDF, который невозможно "
                    "безопасно объединить."
                )

            if not reader.pages:
                raise ValueError(
                    "Ozon вернул PDF без страниц этикеток."
                )

            for page in reader.pages:
                writer.add_page(page)

        output = BytesIO()
        writer.write(output)

        merged_pdf = output.getvalue()

        if not merged_pdf.startswith(b"%PDF-"):
            raise ValueError(
                "Не удалось подготовить итоговый PDF этикеток Ozon."
            )

        return merged_pdf

    finally:
        for stream in source_streams:
            stream.close()


def _render_article_summary(
    rows: list[dict[str, Any]],
) -> None:
    """Показывает группировку выбранных отправлений по артикулам."""
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
    """Инициализирует состояние новой Ozon-вкладки."""
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
    Отображает вкладку печати оригинальных QR-этикеток Ozon FBS.

    Вкладка не подтверждает сборку и не меняет статусы. Она работает
    только с уже собранными отправлениями `awaiting_deliver`.
    """
    _init_state()

    st.title("🏷️ Стикеры FBS OZON")

    st.markdown(
        """
        <div class="description-box">
            После того как менеджер вручную нажал «Собрать» в кабинете
            Ozon и отправления перешли в статус «Ожидает отгрузки»,
            выберите нужные задания и получите оригинальные PDF-этикетки
            Ozon с QR-кодами.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Источник: FBS-отправления Ozon с точным статусом "
        "`awaiting_deliver` («Ожидает отгрузки»). "
        "В список попадают только отправления, содержащие товары "
        f"с offer_id, начинающимся с `{OZON_FBS_OFFER_PREFIX}`."
    )

    st.caption(
        "Вкладка выполняет только read-only запросы. "
        "Она не нажимает «Собрать», не меняет статусы и не отменяет "
        "отправления."
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
                "Фильтруется поле Ozon `in_process_at` в часовом поясе "
                "Europe/Moscow. По умолчанию отображаются задания, "
                "поступившие в работу сегодня."
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

    result_signature = tuple(
        sorted(unique_selected_numbers)
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
        "🖨️ Получить и подготовить QR-этикетки Ozon",
        type="primary",
        use_container_width=True,
        key="generate_ozon_fbs_sticker_pdf",
        disabled=(
            not selected_postings
            or bool(missing_selected_numbers)
        ),
        help=(
            "Ozon рекомендует запрашивать этикетки через 45–60 секунд "
            "после ручного подтверждения сборки. Если этикетки ещё "
            "не готовы, повторите запрос позже."
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
                "Получаю оригинальные QR-этикетки Ozon "
                "и объединяю PDF…"
            ):
                pdf_parts = client.package_labels_pdf(
                    posting_numbers=[
                        posting.posting_number
                        for posting in selected_postings
                    ]
                )

                merged_pdf = _merge_ozon_label_pdfs(
                    pdf_parts=pdf_parts
                )

                article_summary = _build_article_summary(
                    postings=selected_postings
                )

                selected_posting_rows = (
                    _build_selected_posting_rows(
                        postings=selected_postings
                    )
                )

            st.session_state["ozon_fbs_sticker_result"] = {
                "signature": result_signature,
                "generated_at": datetime.now(MOSCOW_TZ),
                "pdf": merged_pdf,
                "article_summary": article_summary,
                "selected_posting_rows": selected_posting_rows,
                "posting_count": len(selected_postings),
                "article_count": len(article_summary),
                "total_quantity": sum(
                    posting.total_quantity
                    for posting in selected_postings
                ),
            }

            st.success(
                "Оригинальные QR-этикетки Ozon готовы к печати."
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
            "«Ожидает отгрузки» и нажмите кнопку подготовки "
            "QR-этикеток."
        )
        return

    st.divider()
    st.subheader("📊 Сводка выбранных отправлений")

    metric_1, metric_2, metric_3 = st.columns(3)

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

    st.caption(
        "PDF сформирован: "
        f"{saved_result['generated_at'].strftime('%d.%m.%Y %H:%M')} "
        "по Москве."
    )

    st.subheader("Артикулы NAKL_ в выбранных отправлениях")

    _render_article_summary(
        rows=saved_result["article_summary"]
    )

    st.subheader("Отправления, вошедшие в печать")

    _render_selected_postings(
        rows=saved_result["selected_posting_rows"]
    )

    st.divider()
    st.subheader("📥 Оригинальные QR-этикетки Ozon")

    st.caption(
        "Итоговый файл содержит оригинальные PDF-страницы Ozon. "
        "QR-коды и разметка Ozon не перерисовывались и не изменялись."
    )

    timestamp = saved_result["generated_at"].strftime(
        "%Y%m%d_%H%M%S"
    )

    st.download_button(
        label="📥 Скачать PDF с QR-этикетками Ozon",
        data=saved_result["pdf"],
        file_name=f"ozon_fbs_qr_labels_{timestamp}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )

    st.warning(
        "Перед рабочей печатью выполните пробу на одной этикетке. "
        "В окне печати выберите масштаб 100% / «Фактический размер» "
        "и отключите автоматическое подгоняние по странице."
    )