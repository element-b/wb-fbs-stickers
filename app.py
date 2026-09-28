from __future__ import annotations

import hmac
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from mysklad_export import make_mysklad_xlsx
from pdf_export import make_pdf
from pdf_tab import render_open_pdf_button
from pipeline import DataCheckError, collect_and_group
from wb_api import WBApiError, WBClient


MOSCOW_TZ = ZoneInfo("Europe/Moscow")

STICKER_SIZES = {
    "58 × 40 мм": (58, 40),
    "40 × 30 мм": (40, 30),
}

PRINT_ORDER_OPTIONS = {
    "Для рулона — обратный порядок": True,
    "Обычный порядок": False,
}


# ============================================================
# НАСТРОЙКА СТРАНИЦЫ
# ============================================================

st.set_page_config(
    page_title="Стикеры WB FBS",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# СТИЛИ
# ============================================================

def apply_main_styles() -> None:
    """Основные стили авторизованной части приложения."""
    st.markdown(
        """
        <style>
            .stApp {
                background-color: #FFFFFF;
                color: #000000;
            }

            .main .block-container {
                padding: 2rem 3rem 3rem 3rem;
                max-width: 1450px;
            }

            .stApp h1,
            .stApp h2,
            .stApp h3 {
                color: #000000 !important;
                font-weight: 650;
            }

            .stApp p,
            .stApp span,
            .stApp div {
                color: #000000;
            }

            section[data-testid="stSidebar"] {
                background-color: #000000;
                border-right: 1px solid #262626;
                padding-top: 1rem;
            }

            section[data-testid="stSidebar"] * {
                color: #FFFFFF !important;
            }

            section[data-testid="stSidebar"] .stButton > button {
                width: 100%;
                background-color: transparent;
                color: #FFFFFF !important;
                border: 1px solid #4A4A4A;
                border-radius: 8px;
                padding: 11px 14px;
                font-size: 15px;
                font-weight: 500;
                transition: all 0.2s ease-in-out;
            }

            section[data-testid="stSidebar"] .stButton > button:hover {
                background-color: #202020;
                border-color: #777777;
                transform: translateX(2px);
            }

            .stButton > button {
                background-color: #009B77;
                color: #FFFFFF !important;
                border: none;
                border-radius: 7px;
                padding: 9px 16px;
                font-weight: 600;
                transition: all 0.2s ease-in-out;
            }

            .stButton > button:hover {
                background-color: #008268;
                box-shadow: 0 3px 10px rgba(0, 155, 119, 0.28);
            }

            div[data-testid="stVerticalBlockBorderWrapper"] {
                border: 1px solid #E4E4E7 !important;
                border-radius: 12px !important;
                background-color: #FAFAFA !important;
            }

            .description-box {
                border-left: 4px solid #009B77;
                background: #F2FBF7;
                border-radius: 6px;
                padding: 14px 18px;
                margin-bottom: 24px;
                color: #1F2937;
            }

            div[data-testid="metric-container"] {
                background-color: #FAFAFA;
                border: 1px solid #E5E7EB;
                border-radius: 10px;
                padding: 12px 16px;
            }

            [data-testid="stMetricValue"] {
                color: #000000 !important;
                font-size: 1.45rem !important;
                font-weight: 650 !important;
            }

            [data-testid="stMetricLabel"] {
                color: #444444 !important;
            }

            .stDownloadButton > button {
                width: 100%;
                background-color: #009B77 !important;
                color: #FFFFFF !important;
                border: none !important;
                border-radius: 7px !important;
                padding: 10px 16px !important;
                font-weight: 600 !important;
            }

            .stDownloadButton > button:hover {
                background-color: #008268 !important;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def apply_login_styles() -> None:
    """Стили страницы входа в стиле проекта «Контроль поставок Ozon»."""
    st.markdown(
        """
        <style>
            header[data-testid="stHeader"],
            section[data-testid="stSidebar"] {
                display: none !important;
            }

            .stApp {
                background:
                    radial-gradient(
                        circle at top left,
                        rgba(80, 200, 120, 0.16),
                        transparent 35%
                    ),
                    radial-gradient(
                        circle at bottom right,
                        rgba(0, 155, 119, 0.12),
                        transparent 38%
                    ),
                    linear-gradient(
                        135deg,
                        #0B0915 0%,
                        #151226 55%,
                        #0B1220 100%
                    );
            }

            .main .block-container {
                padding: 0 !important;
                max-width: 100% !important;
            }

            div[data-testid="stForm"] {
                background-color: rgba(21, 18, 38, 0.96);
                padding: 2.5rem;
                border-radius: 14px;
                border: 1px solid rgba(255, 255, 255, 0.09);
                box-shadow: 0 18px 48px rgba(0, 0, 0, 0.4);
            }

            div[data-testid="stForm"] input {
                background-color: #151226 !important;
                border: 1px solid #34304B !important;
                color: #FFFFFF !important;
                border-radius: 7px !important;
            }

            div[data-testid="stForm"] input::placeholder {
                color: #A4A1B4 !important;
            }

            div[data-testid="stForm"] .stFormSubmitButton > button {
                width: 100%;
                background-color: transparent !important;
                color: #50C878 !important;
                border: 2px solid #50C878 !important;
                border-radius: 8px !important;
                padding: 11px !important;
                font-weight: 650 !important;
            }

            div[data-testid="stForm"] .stFormSubmitButton > button:hover {
                background-color: #50C878 !important;
                color: #FFFFFF !important;
                border-color: #50C878 !important;
                box-shadow: 0 5px 18px rgba(80, 200, 120, 0.35) !important;
                transform: translateY(-1px);
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# SESSION STATE
# ============================================================

def init_session_state() -> None:
    """Создаёт начальные значения пользовательской сессии."""
    defaults = {
        "authenticated": False,
        "supplies": None,
        "result": None,
        "supply_widget_version": 0,
        "reprint_lookup_key": None,
        "reprint_lookup_error": None,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def invalidate_result() -> None:
    """Удаляет PDF и данные ранее сформированной партии."""
    st.session_state["result"] = None
    st.session_state["reprint_lookup_key"] = None
    st.session_state["reprint_lookup_error"] = None


# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================

def get_users() -> dict[str, str]:
    """Читает пользователей из Streamlit Secrets."""
    try:
        configured_users = st.secrets["users"]
    except Exception:
        st.error(
            "Не настроены пользователи. Добавьте таблицу `[users]` "
            "в Streamlit Secrets."
        )
        st.stop()

    try:
        users = {
            str(username): str(password)
            for username, password in configured_users.items()
        }
    except Exception:
        st.error(
            "Таблица `[users]` в Streamlit Secrets настроена некорректно."
        )
        st.stop()

    if not users:
        st.error("В таблице `[users]` нет ни одного пользователя.")
        st.stop()

    if any(
        not username.strip() or not password
        for username, password in users.items()
    ):
        st.error(
            "У одного или нескольких пользователей в Secrets указан "
            "пустой логин или пароль."
        )
        st.stop()

    return users


def authenticate(
    username: str,
    password: str,
    users: dict[str, str],
) -> bool:
    """Проверяет введённые логин и пароль."""
    matched = False

    for configured_username, configured_password in users.items():
        username_matches = hmac.compare_digest(
            username,
            configured_username,
        )

        password_matches = hmac.compare_digest(
            password,
            configured_password,
        )

        matched = matched or (
            username_matches and password_matches
        )

    return matched


def render_login_page(users: dict[str, str]) -> None:
    """Отображает страницу входа."""
    apply_login_styles()

    st.markdown("<br><br><br><br><br>", unsafe_allow_html=True)

    _, center_column, _ = st.columns([1, 1.05, 1])

    with center_column:
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input(
                "Логин",
                placeholder="Логин",
                autocomplete="username",
                label_visibility="collapsed",
            )

            password = st.text_input(
                "Пароль",
                placeholder="Пароль",
                type="password",
                autocomplete="current-password",
                label_visibility="collapsed",
            )

            st.markdown("<br>", unsafe_allow_html=True)

            submitted = st.form_submit_button(
                "Войти",
                use_container_width=True,
            )

            if submitted:
                if authenticate(username, password, users):
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("Неверный логин или пароль.")


def logout() -> None:
    """Выходит из приложения и очищает данные сессии."""
    st.session_state.clear()
    st.rerun()


# ============================================================
# ПОСТАВКИ И ФИЛЬТРЫ
# ============================================================

def parse_created_at(value: object) -> datetime | None:
    """Преобразует WB `createdAt` в дату и время Europe/Moscow."""
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(MOSCOW_TZ)

    except ValueError:
        return None


def supply_is_done(supply: dict) -> bool:
    """Проверяет признак завершённой поставки WB."""
    value = supply.get("done", False)

    if isinstance(value, bool):
        return value

    return str(value).strip().lower() == "true"


def format_supply_label(supply: dict) -> str:
    """Формирует подпись поставки для списка выбора."""
    supply_id = str(supply.get("id", "")).strip()
    name = str(supply.get("name") or "Без названия").strip()

    created_at = parse_created_at(supply.get("createdAt"))

    created_text = (
        created_at.strftime("%d.%m.%Y %H:%M")
        if created_at is not None
        else "дата неизвестна"
    )

    status = "завершена" if supply_is_done(supply) else "активна"

    return (
        f"{name}  |  {supply_id}  |  "
        f"{created_text}  |  {status}"
    )


def value_to_date(value: object) -> date | None:
    """Преобразует значение Streamlit в объект date."""
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return None


def parse_date_range(
    selected_value: object,
) -> tuple[date | None, date | None]:
    """Получает начальную и конечную дату из Streamlit date_input."""
    if isinstance(selected_value, (tuple, list)):
        values = list(selected_value)

        if not values:
            return None, None

        date_from = value_to_date(values[0])

        date_to = (
            value_to_date(values[1])
            if len(values) > 1
            else date_from
        )

        if date_from is None:
            return None, None

        if date_to is None:
            date_to = date_from

        return (
            min(date_from, date_to),
            max(date_from, date_to),
        )

    selected_date = value_to_date(selected_value)

    return selected_date, selected_date


def supply_matches_filters(
    supply: dict,
    search_text: str,
    date_from: date | None,
    date_to: date | None,
    status_filter: str,
) -> bool:
    """Проверяет, подходит ли поставка текущим фильтрам."""
    if status_filter == "Только активные" and supply_is_done(supply):
        return False

    if status_filter == "Только завершённые" and not supply_is_done(supply):
        return False

    created_at = parse_created_at(supply.get("createdAt"))

    if created_at is None:
        return False

    created_date = created_at.date()

    if date_from is not None and created_date < date_from:
        return False

    if date_to is not None and created_date > date_to:
        return False

    normalized_search = search_text.strip().casefold()

    if normalized_search:
        supply_id = str(supply.get("id") or "").casefold()
        supply_name = str(supply.get("name") or "").casefold()

        if (
            normalized_search not in supply_id
            and normalized_search not in supply_name
        ):
            return False

    return True


def sort_supply_ids(
    supply_ids: list[str],
    supply_by_id: dict[str, dict],
) -> list[str]:
    """Сортирует поставки: новые сверху."""
    def sort_key(supply_id: str) -> tuple[float, str, str]:
        supply = supply_by_id[supply_id]
        created_at = parse_created_at(supply.get("createdAt"))

        timestamp = (
            created_at.timestamp()
            if created_at is not None
            else 0
        )

        return (
            -timestamp,
            str(supply.get("name") or "").casefold(),
            supply_id.casefold(),
        )

    return sorted(supply_ids, key=sort_key)


# ============================================================
# ПЕРЕПЕЧАТКА ПО КОДУ
# ============================================================

def parse_sticker_code(value: str) -> str | None:
    """
    Принимает код в форматах:

    - 231648 9753;
    - 231648-9753;
    - 231648/9753;
    - 231648,9753.
    """
    match = re.fullmatch(
        r"\s*(\d+)\s*[\s,;/\-]+\s*(\d+)\s*",
        value or "",
    )

    if not match:
        return None

    return f"{match.group(1)} {match.group(2)}"


def render_reprint_search(result: dict) -> None:
    """Отображает поиск и перепечатку стикера текущей партии."""
    summary = result["summary"]
    lookup = summary.get("sticker_lookup", {})

    st.divider()
    st.subheader("♻️ Найти и перепечатать стикер")

    st.caption(
        "Введите две цифровые части со стикера, например: `231648 9753`. "
        "Поиск выполняется среди стикеров текущей сформированной партии."
    )

    search_col, button_col = st.columns([3, 1])

    with search_col:
        sticker_code = st.text_input(
            "Код стикера",
            placeholder="231648 9753",
            key="reprint_sticker_code",
            label_visibility="collapsed",
        )

    with button_col:
        search_clicked = st.button(
            "Найти стикер",
            use_container_width=True,
            key="find_reprint_sticker",
        )

    if search_clicked:
        parsed_code = parse_sticker_code(sticker_code)

        if parsed_code is None:
            st.session_state["reprint_lookup_key"] = None
            st.session_state["reprint_lookup_error"] = (
                "Введите две группы цифр, например: 231648 9753."
            )
        else:
            st.session_state["reprint_lookup_key"] = parsed_code
            st.session_state["reprint_lookup_error"] = None

    error = st.session_state.get("reprint_lookup_error")

    if error:
        st.error(error)
        return

    lookup_key = st.session_state.get("reprint_lookup_key")

    if not lookup_key:
        return

    found = lookup.get(lookup_key)

    if found is None:
        if summary.get("searchable_sticker_count", 0) == 0:
            st.warning(
                "WB не вернул цифровые части partA и partB для "
                "стикеров этой партии. Поиск по коду недоступен."
            )
        else:
            st.error(
                "Стикер с таким кодом не найден среди текущей партии."
            )
        return

    article = found["article"]
    order_id = found["order_id"]
    destinations = found.get("distribution_centers", [])

    st.success(
        f"Найден стикер: `{lookup_key}`. Артикул: `{article}`."
    )

    info_col, print_col = st.columns([2, 1])

    with info_col:
        st.markdown("**СЦ, куда отправляется данный артикул:**")

        if destinations:
            for center in destinations:
                st.write(f"• {center}")
        else:
            st.write("• Не определено")

        st.caption(
            f"ID сборочного задания: {order_id}. "
            "В PDF будет служебная этикетка и один оригинальный "
            "стикер WB."
        )

    with print_col:
        reprint_group = {
            "article": article,
            "order_ids": [order_id],
            "stickers": [found["png_bytes"]],
        }

        try:
            reprint_pdf = make_pdf(
                groups=[reprint_group],
                width_mm=result["sticker_width"],
                height_mm=result["sticker_height"],
                include_group_separators=True,
                reverse_page_order=result["reverse_print_order"],
            )

            render_open_pdf_button(
                article=f"{article} — перепечатка",
                pdf_bytes=reprint_pdf,
            )

        except ValueError as error:
            st.error(str(error))


# ============================================================
# РЕЗУЛЬТАТЫ И ВЫГРУЗКА
# ============================================================

def build_article_table(summary: dict) -> pd.DataFrame:
    """Строит таблицу артикулов с количеством и СЦ назначения."""
    rows = []

    for group in summary["groups"]:
        centers = group.get("distribution_centers", [])

        rows.append(
            {
                "Артикул продавца": group["article"],
                "Количество стикеров": len(group["order_ids"]),
                "Количество поставок": group["supply_count"],
                "СЦ назначения": ", ".join(centers) if centers else "—",
            }
        )

    return pd.DataFrame(rows)


def render_results(result: dict) -> None:
    """Отображает итоговую таблицу, перепечатку и ссылки на PDF."""
    summary = result["summary"]

    st.divider()
    st.subheader("📊 Список по артикулам")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric("Выбрано поставок", summary["supply_count"])
    metric_2.metric("Уникальных артикулов", summary["article_count"])
    metric_3.metric("Стикеров / заданий", summary["order_count"])
    metric_4.metric("Пустых поставок", len(summary["empty_supplies"]))

    if summary["empty_supplies"]:
        st.warning(
            "В выбранных поставках не найдено сборочных заданий: "
            + ", ".join(summary["empty_supplies"])
        )

    st.caption(
        "СЦ определяется из названия поставки. Пример: "
        "`Накл Софьино от 26.09.2026` → `Софьино`."
    )

    table = build_article_table(summary)

    search_col, sort_col = st.columns([2, 1])

    with search_col:
        article_search = st.text_input(
            "Поиск по артикулу",
            placeholder="Например: NAKL_AFFIRMATIONS_160",
        )

    sort_options = {
        "Количество стикеров — по убыванию": (
            "Количество стикеров",
            False,
        ),
        "Количество стикеров — по возрастанию": (
            "Количество стикеров",
            True,
        ),
        "Количество поставок — по убыванию": (
            "Количество поставок",
            False,
        ),
        "Количество поставок — по возрастанию": (
            "Количество поставок",
            True,
        ),
        "Артикул — А → Я": (
            "Артикул продавца",
            True,
        ),
    }

    with sort_col:
        sort_label = st.selectbox(
            "Сортировка списка",
            options=list(sort_options.keys()),
            index=0,
        )

    sort_by, sort_ascending = sort_options[sort_label]

    filtered_table = table.copy()

    if article_search.strip():
        filtered_table = filtered_table[
            filtered_table["Артикул продавца"].str.contains(
                article_search.strip(),
                case=False,
                regex=False,
                na=False,
            )
        ]

    filtered_table = filtered_table.sort_values(
        by=sort_by,
        ascending=sort_ascending,
        kind="stable",
    )

    st.dataframe(
        filtered_table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Артикул продавца": st.column_config.TextColumn(
                "Артикул продавца",
                width="large",
            ),
            "Количество стикеров": st.column_config.NumberColumn(
                "Количество стикеров",
                format="%d",
            ),
            "Количество поставок": st.column_config.NumberColumn(
                "Количество поставок",
                format="%d",
            ),
            "СЦ назначения": st.column_config.TextColumn(
                "СЦ назначения",
                width="large",
            ),
        },
    )

    st.success(
        "Все выбранные задания сопоставлены с одним оригинальным "
        "стикером WB."
    )

    render_reprint_search(result)

    st.divider()
    st.subheader("📄 Общий файл и выгрузка в МойСклад")

    if result["reverse_print_order"]:
        order_caption = (
            "PDF сформирован в обратном порядке страниц для рулонной "
            "печати. При размотке ленты порядок групп будет рабочим."
        )
    else:
        order_caption = (
            "PDF сформирован в обычном порядке страниц."
        )

    st.caption(
        "Перед каждой группой печатается служебная этикетка, затем идут "
        "оригинальные стикеры WB. " + order_caption
    )

    timestamp = datetime.now(MOSCOW_TZ).strftime("%Y%m%d_%H%M%S")

    pdf_col, xlsx_col = st.columns(2)

    with pdf_col:
        st.download_button(
            label="📥 Скачать общий PDF",
            data=result["full_pdf"],
            file_name=f"wb_fbs_all_articles_{timestamp}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

    with xlsx_col:
        st.download_button(
            label="📊 Скачать XLSX для МойСклад",
            data=result["mysklad_xlsx"],
            file_name=f"wb_fbs_mysklad_{timestamp}.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            use_container_width=True,
        )

    st.caption(
        "XLSX содержит: `Артикул` поштучно и отдельный список "
        "уникальных `СЦ назначения`."
    )

    st.divider()
    st.subheader("🔗 Стикеры по отдельным артикулам")

    st.caption(
        "Каждый PDF начинается со служебной этикетки. "
        "Для рулонной печати PDF также сформирован в обратном порядке."
    )

    article_pdf_by_name = dict(result["article_pdfs"])

    group_by_article = {
        group["article"]: group
        for group in summary["groups"]
    }

    visible_articles = filtered_table["Артикул продавца"].tolist()

    if not visible_articles:
        st.warning("По текущему поиску не найдено артикулов.")
        return

    header_1, header_2, header_3, header_4, header_5 = st.columns(
        [2.4, 0.7, 0.7, 1.5, 1.3]
    )

    with header_1:
        st.caption("**Артикул**")

    with header_2:
        st.caption("**Стик.**")

    with header_3:
        st.caption("**Пост.**")

    with header_4:
        st.caption("**СЦ**")

    with header_5:
        st.caption("**PDF**")

    for article in visible_articles:
        group = group_by_article[article]
        centers = group.get("distribution_centers", [])

        article_col, count_col, supply_col, center_col, open_col = st.columns(
            [2.4, 0.7, 0.7, 1.5, 1.3]
        )

        with article_col:
            st.markdown(f"`{article}`")

        with count_col:
            st.markdown(f"**{len(group['order_ids'])}**")

        with supply_col:
            st.markdown(f"**{group['supply_count']}**")

        with center_col:
            st.caption(", ".join(centers) if centers else "—")

        with open_col:
            render_open_pdf_button(
                article=article,
                pdf_bytes=article_pdf_by_name[article],
            )

    st.warning(
        f"Размер страницы PDF: {result['sticker_size_name']}. "
        "Перед рабочей печатью выполните пробу на нескольких стикерах. "
        "В окне печати выберите масштаб 100% / «Фактический размер» "
        "и отключите «Подогнать под страницу»."
    )


# ============================================================
# БОКОВАЯ ПАНЕЛЬ
# ============================================================

def render_sidebar() -> None:
    """Отображает боковую панель."""
    with st.sidebar:
        st.markdown("## 📦 WB FBS")
        st.caption("Стикеры сборочных заданий")
        st.caption(
            "Приложение не меняет поставки, статусы заказов и короба."
        )

        st.divider()

        st.markdown("**Как пользоваться**")
        st.markdown(
            """
            1. Обновите список поставок WB.
            2. Выберите дату или диапазон.
            3. Выберите поставки.
            4. Сформируйте файлы.
            5. Печатайте PDF в режиме для рулона.
            """
        )

        st.divider()

        if st.button(
            "⍈ Выйти",
            use_container_width=True,
            key="logout_button",
        ):
            logout()


# ============================================================
# ОСНОВНОЙ ЭКРАН
# ============================================================

def render_main_page(client: WBClient) -> None:
    """Отображает основной рабочий экран."""
    render_sidebar()

    st.title("📦 Стикеры сборочных заданий WB FBS")

    st.markdown(
        """
        <div class="description-box">
            Выберите поставки WB. Приложение сгруппирует оригинальные
            стикеры по артикулам, покажет СЦ назначения, создаст PDF
            для рулонной печати и XLSX для МойСклад.
        </div>
        """,
        unsafe_allow_html=True,
    )

    load_col, info_col = st.columns([1.2, 2.8])

    with load_col:
        load_clicked = st.button(
            "⟳ Загрузить / обновить поставки",
            use_container_width=True,
        )

    with info_col:
        st.caption(
            "После обновления списка поставок сформированные ранее "
            "файлы удаляются из текущей сессии."
        )

    if load_clicked:
        invalidate_result()

        st.session_state["supplies"] = None
        st.session_state["supply_widget_version"] += 1

        try:
            with st.spinner("Загружаю список поставок WB…"):
                st.session_state["supplies"] = client.list_supplies()
        except WBApiError as error:
            st.error(str(error))

    supplies = st.session_state["supplies"]

    if supplies is None:
        st.info("Нажмите «Загрузить / обновить поставки».")
        st.stop()

    if not supplies:
        st.warning("WB не вернул доступных поставок.")
        st.stop()

    supply_by_id: dict[str, dict] = {}

    for supply in supplies:
        if not isinstance(supply, dict):
            continue

        supply_id = str(supply.get("id", "")).strip()

        if supply_id:
            supply_by_id[supply_id] = supply

    if not supply_by_id:
        st.error(
            "WB вернул список поставок без корректных идентификаторов."
        )
        st.stop()

    st.divider()
    st.subheader("Фильтры списка поставок")

    today = datetime.now(MOSCOW_TZ).date()

    date_col, status_col, search_col = st.columns([1.6, 1, 1.8])

    with date_col:
        selected_date_range = st.date_input(
            "Дата создания поставки",
            value=(today, today),
            format="DD.MM.YYYY",
            help=(
                "Фильтруется `createdAt`: дата создания поставки WB "
                "в часовом поясе Europe/Moscow."
            ),
        )

    with status_col:
        status_filter = st.selectbox(
            "Статус поставки",
            options=[
                "Все",
                "Только активные",
                "Только завершённые",
            ],
        )

    with search_col:
        supply_search = st.text_input(
            "Поиск поставки",
            placeholder="Введите часть названия или ID",
        )

    date_from, date_to = parse_date_range(selected_date_range)

    if date_from is None or date_to is None:
        visible_ids: list[str] = []
        date_filter_text = "Дата не выбрана"
    else:
        date_filter_text = (
            date_from.strftime("%d.%m.%Y")
            if date_from == date_to
            else (
                f"{date_from.strftime('%d.%m.%Y')} — "
                f"{date_to.strftime('%d.%m.%Y')}"
            )
        )

        visible_ids = [
            supply_id
            for supply_id, supply in supply_by_id.items()
            if supply_matches_filters(
                supply=supply,
                search_text=supply_search,
                date_from=date_from,
                date_to=date_to,
                status_filter=status_filter,
            )
        ]

        visible_ids = sort_supply_ids(
            supply_ids=visible_ids,
            supply_by_id=supply_by_id,
        )

    st.caption(
        f"Дата создания поставки: {date_filter_text}. "
        f"Найдено поставок: {len(visible_ids)}."
    )

    widget_key = (
        f"supplies_{st.session_state['supply_widget_version']}"
    )

    saved_selected_ids = st.session_state.get(widget_key, [])

    if not isinstance(saved_selected_ids, list):
        saved_selected_ids = []

    # Выбранные поставки остаются в поле, даже если текущий фильтр
    # временно скрывает их из общего списка.
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
        "Найдите поставки по названию или ID и выберите нужные",
        options=option_ids,
        format_func=lambda supply_id: format_supply_label(
            supply_by_id[supply_id]
        ),
        key=widget_key,
        placeholder="Выберите минимум две поставки",
    )

    clear_col, _ = st.columns([1, 3])

    with clear_col:
        clear_selection_clicked = st.button(
            "🧹 Очистить выбор",
            use_container_width=True,
        )

    if clear_selection_clicked:
        invalidate_result()
        st.session_state["supply_widget_version"] += 1
        st.rerun()

    all_ids = list(dict.fromkeys(selected_ids))

    if all_ids:
        st.success(f"Выбрано поставок: {len(all_ids)}")

    if len(all_ids) == 1:
        st.warning(
            "Для рабочей группировки выберите минимум две поставки."
        )

    st.divider()
    st.subheader("Параметры формирования файла")

    settings_col_1, settings_col_2, settings_col_3 = st.columns(
        [1, 1, 1.5]
    )

    with settings_col_1:
        lookback_days = st.selectbox(
            "Период поиска заданий WB",
            options=[7, 31, 90, 180],
            index=0,
            format_func=lambda days: f"Последние {days} дней",
        )

    with settings_col_2:
        sticker_size_name = st.selectbox(
            "Размер стикера WB",
            options=list(STICKER_SIZES.keys()),
            index=0,
        )

    with settings_col_3:
        print_order_label = st.selectbox(
            "Порядок печати",
            options=list(PRINT_ORDER_OPTIONS.keys()),
            index=0,
            help=(
                "Для рулона используется обратный порядок PDF-страниц. "
                "Это упрощает намотку длинной ленты."
            ),
        )

    sticker_width, sticker_height = STICKER_SIZES[sticker_size_name]

    reverse_print_order = PRINT_ORDER_OPTIONS[print_order_label]

    selected_supplies = [
        supply_by_id[supply_id]
        for supply_id in all_ids
        if supply_id in supply_by_id
    ]

    result_signature = (
        tuple(sorted(all_ids)),
        lookback_days,
        sticker_width,
        sticker_height,
        reverse_print_order,
    )

    current_result = st.session_state.get("result")

    if (
        current_result is not None
        and current_result["signature"] != result_signature
    ):
        invalidate_result()

    st.markdown("<br>", unsafe_allow_html=True)

    generate_clicked = st.button(
        "🔄 Обновить данные и сформировать файлы",
        type="primary",
        disabled=len(all_ids) < 2,
        use_container_width=True,
    )

    if generate_clicked:
        invalidate_result()

        try:
            with st.spinner(
                "Получаю задания, проверяю артикулы и запрашиваю "
                "оригинальные стикеры WB…"
            ):
                summary = collect_and_group(
                    client=client,
                    selected_supplies=selected_supplies,
                    lookback_days=lookback_days,
                    sticker_width=sticker_width,
                    sticker_height=sticker_height,
                )

                full_pdf = make_pdf(
                    groups=summary["groups"],
                    width_mm=sticker_width,
                    height_mm=sticker_height,
                    include_group_separators=True,
                    reverse_page_order=reverse_print_order,
                )

                article_pdfs: list[tuple[str, bytes]] = []

                for group in summary["groups"]:
                    article_pdf = make_pdf(
                        groups=[group],
                        width_mm=sticker_width,
                        height_mm=sticker_height,
                        include_group_separators=True,
                        reverse_page_order=reverse_print_order,
                    )

                    article_pdfs.append(
                        (
                            group["article"],
                            article_pdf,
                        )
                    )

                mysklad_xlsx = make_mysklad_xlsx(
                    summary["groups"]
                )

            st.session_state["result"] = {
                "signature": result_signature,
                "summary": summary,
                "full_pdf": full_pdf,
                "article_pdfs": article_pdfs,
                "mysklad_xlsx": mysklad_xlsx,
                "sticker_size_name": sticker_size_name,
                "sticker_width": sticker_width,
                "sticker_height": sticker_height,
                "reverse_print_order": reverse_print_order,
            }

            st.success(
                "Все данные проверены. Файлы для печати готовы."
            )

        except (WBApiError, DataCheckError, ValueError) as error:
            st.error(str(error))

    result = st.session_state.get("result")

    if result is None or result["signature"] != result_signature:
        st.info(
            "Файлы появятся только после успешной полной проверки "
            "выбранных поставок."
        )
        st.stop()

    render_results(result)


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def main() -> None:
    """Запускает приложение."""
    init_session_state()

    users = get_users()

    if not st.session_state["authenticated"]:
        render_login_page(users)
        st.stop()

    apply_main_styles()

    try:
        token = str(st.secrets["WB_API_TOKEN"]).strip()
    except Exception:
        st.error("Добавьте `WB_API_TOKEN` в Streamlit Secrets.")
        st.stop()

    if not token:
        st.error("`WB_API_TOKEN` в Streamlit Secrets пуст.")
        st.stop()

    client = WBClient(token=token)

    render_main_page(client)


if __name__ == "__main__":
    main()