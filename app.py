from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from pdf_export import make_pdf
from pdf_tab import render_open_pdf_button
from pipeline import DataCheckError, collect_and_group
from wb_api import WBApiError, WBClient


MOSCOW_TZ = ZoneInfo("Europe/Moscow")

STICKER_SIZES = {
    "58 × 40 мм": (58, 40),
    "40 × 30 мм": (40, 30),
}


# ============================================================
# НАСТРОЙКА СТРАНИЦЫ
# ============================================================

st.set_page_config(
    page_title="FBZ",
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
    """
    Стили страницы входа.

    Оформление повторяет страницу авторизации проекта
    «Контроль поставок Ozon».
    """
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
                transition: all 0.25s ease-in-out !important;
            }

            div[data-testid="stForm"] .stFormSubmitButton > button:hover {
                background-color: #50C878 !important;
                color: #FFFFFF !important;
                border-color: #50C878 !important;
                box-shadow: 0 5px 18px rgba(80, 200, 120, 0.35) !important;
                transform: translateY(-1px);
            }

            div[data-testid="stForm"] .stFormSubmitButton > button:focus-visible {
                outline: 3px solid rgba(80, 200, 120, 0.45) !important;
                outline-offset: 3px;
            }

            .login-title {
                color: #FFFFFF !important;
                text-align: center;
                font-size: 1.9rem;
                font-weight: 700;
                margin-bottom: 0.55rem;
            }

            .login-subtitle {
                color: #C7C5D5 !important;
                text-align: center;
                font-size: 0.96rem;
                margin-bottom: 1.8rem;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# SESSION STATE
# ============================================================

def init_session_state() -> None:
    """Создаёт начальные переменные текущей пользовательской сессии."""
    defaults = {
        "authenticated": False,
        "supplies": None,
        "result": None,
        "supply_widget_version": 0,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def invalidate_result() -> None:
    """
    Удаляет старые сформированные PDF из памяти текущей сессии.

    После изменения выбранных поставок, размера стикера или периода
    поиска нельзя продолжать использовать предыдущий файл.
    """
    st.session_state["result"] = None


# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================

def get_users() -> dict[str, str]:
    """
    Читает пользователей из Streamlit Secrets.

    Ожидаемый формат Secrets:

    [users]
    kladovshik = "ваш-пароль"
    """
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

    empty_credentials = [
        username
        for username, password in users.items()
        if not username.strip() or not password
    ]

    if empty_credentials:
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
    """Проверяет логин и пароль из формы."""
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
    """Отображает страницу входа в стиле проекта Ozon."""
    apply_login_styles()

    st.markdown("<br><br><br><br>", unsafe_allow_html=True)

    _, center_column, _ = st.columns([1, 1.05, 1])

    with center_column:
        st.markdown(
            '<div class="login-title">📦 FBZ</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            (
                '<div class="login-subtitle">'
                ''
                '</div>'
            ),
            unsafe_allow_html=True,
        )

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
    """Выходит из приложения и очищает данные текущей сессии."""
    st.session_state.clear()
    st.rerun()


# ============================================================
# РАБОТА СО СПИСКОМ ПОСТАВОК И ФИЛЬТРАМИ
# ============================================================

def parse_created_at(value: object) -> datetime | None:
    """
    Преобразует поле WB `createdAt` в дату/время Europe/Moscow.

    Если WB передаёт дату без часовой зоны, она трактуется как UTC.
    """
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
    """Возвращает признак завершённой поставки по полю WB `done`."""
    value = supply.get("done", False)

    if isinstance(value, bool):
        return value

    return str(value).strip().lower() == "true"


def format_supply_label(supply: dict) -> str:
    """Создаёт подпись поставки для списка выбора."""
    supply_id = str(supply.get("id", "")).strip()
    name = str(supply.get("name") or "Без названия").strip()

    created_at = parse_created_at(supply.get("createdAt"))

    if created_at is None:
        created_text = "дата неизвестна"
    else:
        created_text = created_at.strftime("%d.%m.%Y %H:%M")

    status = "завершена" if supply_is_done(supply) else "активна"

    return (
        f"{name}  |  {supply_id}  |  "
        f"{created_text}  |  {status}"
    )


def supply_matches_filters(
    supply: dict,
    search_text: str,
    period_days: int | None,
    status_filter: str,
) -> bool:
    """
    Проверяет, должна ли поставка быть видна в списке.

    Фильтры применяются к уже загруженному списку, без новых запросов к WB.
    """
    if status_filter == "Только активные" and supply_is_done(supply):
        return False

    if status_filter == "Только завершённые" and not supply_is_done(supply):
        return False

    if period_days is not None:
        created_at = parse_created_at(supply.get("createdAt"))

        if created_at is None:
            return False

        min_date = (
            datetime.now(MOSCOW_TZ).date()
            - timedelta(days=period_days - 1)
        )

        if created_at.date() < min_date:
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
    """Сортирует поставки: сначала новые, потом по названию и ID."""
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
# БОКОВАЯ ПАНЕЛЬ
# ============================================================

def render_sidebar() -> None:
    """Отображает боковую панель приложения."""
    with st.sidebar:
        st.markdown("## 📦 WB FBS")
        st.caption("Стикеры сборочных заданий")
        st.caption(
            "Приложение не меняет поставки, заказы, статусы и короба."
        )

        st.divider()

        st.markdown("**Как пользоваться**")
        st.markdown(
            """
            1. Обновите список поставок WB.
            2. Установите фильтр.
            3. Выберите поставки.
            4. Сформируйте файлы.
            5. Откройте PDF нужного артикула.
            """
        )

        st.divider()

        st.caption(
            "Стикеры и PDF существуют только в памяти текущей сессии."
        )

        if st.button(
            "⍈ Выйти",
            use_container_width=True,
            key="logout_button",
        ):
            logout()


# ============================================================
# РЕЗУЛЬТАТЫ: ТАБЛИЦА АРТИКУЛОВ И PDF
# ============================================================

def build_article_table(summary: dict) -> pd.DataFrame:
    """
    Строит итоговую таблицу.

    Одна строка — один точный артикул продавца WB.
    """
    rows = []

    for group in summary["groups"]:
        rows.append(
            {
                "Артикул продавца": group["article"],
                "Количество стикеров": len(group["order_ids"]),
                "Количество поставок": group["supply_count"],
            }
        )

    return pd.DataFrame(rows)


def render_results(result: dict) -> None:
    """
    Показывает сводку по артикулам и ссылки на PDF.

    По умолчанию артикулы сортируются от большего количества стикеров
    к меньшему. Нижний блок отдельных PDF использует такой же порядок.
    """
    summary = result["summary"]

    st.divider()
    st.subheader("📊 Список по артикулам")

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)

    metric_1.metric(
        "Выбрано поставок",
        summary["supply_count"],
    )

    metric_2.metric(
        "Уникальных артикулов",
        summary["article_count"],
    )

    metric_3.metric(
        "Стикеров / заданий",
        summary["order_count"],
    )

    metric_4.metric(
        "Пустых поставок",
        len(summary["empty_supplies"]),
    )

    if summary["empty_supplies"]:
        st.warning(
            "В выбранных поставках не найдено сборочных заданий: "
            + ", ".join(summary["empty_supplies"])
        )

    st.caption(
        "Одна строка — один артикул продавца. "
        "Количество стикеров равно количеству сборочных заданий WB."
    )

    table = build_article_table(summary)

    filter_col_1, filter_col_2 = st.columns([2, 1])

    with filter_col_1:
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
        "Артикул — Я → А": (
            "Артикул продавца",
            False,
        ),
    }

    with filter_col_2:
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
        },
    )

    st.success(
        "Все выбранные задания сопоставлены с одним оригинальным "
        "стикером WB."
    )

    st.divider()
    st.subheader("📄 Общий файл")

    st.caption(
        "Перед каждой группой, включая первую, печатается служебная "
        "этикетка с артикулом и количеством стикеров. После неё идут "
        "оригинальные стикеры WB."
    )

    timestamp = datetime.now(MOSCOW_TZ).strftime("%Y%m%d_%H%M%S")

    st.download_button(
        label="📥 Скачать общий PDF по всем артикулам",
        data=result["full_pdf"],
        file_name=f"wb_fbs_all_articles_{timestamp}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )

    st.divider()
    st.subheader("🔗 Стикеры по отдельным артикулам")

    st.caption(
        "PDF каждого отдельного артикула также начинается со служебной "
        "этикетки с артикулом и количеством стикеров."
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

    header_col_1, header_col_2, header_col_3, header_col_4 = st.columns(
        [2.8, 0.8, 0.8, 1.7]
    )

    with header_col_1:
        st.caption("**Артикул продавца**")

    with header_col_2:
        st.caption("**Стикеры**")

    with header_col_3:
        st.caption("**Поставки**")

    with header_col_4:
        st.caption("**PDF**")

    for article in visible_articles:
        group = group_by_article[article]

        article_col, stickers_col, supplies_col, open_col = st.columns(
            [2.8, 0.8, 0.8, 1.7]
        )

        with article_col:
            st.markdown(f"`{article}`")

        with stickers_col:
            st.markdown(f"**{len(group['order_ids'])}**")

        with supplies_col:
            st.markdown(f"**{group['supply_count']}**")

        with open_col:
            render_open_pdf_button(
                article=article,
                pdf_bytes=article_pdf_by_name[article],
            )

    st.warning(
        f"Размер страницы PDF: {result['sticker_size_name']}. "
        "Перед рабочей печатью проверьте пробную этикетку. "
        "В окне печати выберите масштаб 100% / «Фактический размер» "
        "и отключите настройку «Подогнать под страницу»."
    )


# ============================================================
# ОСНОВНОЙ ЭКРАН
# ============================================================

def render_main_page(client: WBClient) -> None:
    """Отображает основной рабочий экран приложения."""
    render_sidebar()

    st.title("📦 Стикеры сборочных заданий WB FBS")

    st.markdown(
        """
        <div class="description-box">
            Выберите несколько поставок WB. Приложение найдёт одинаковые
            артикулы в разных поставках и сформирует оригинальные стикеры
            WB группами: сначала служебная этикетка с артикулом, затем все
            стикеры этого артикула. Поставки, статусы заказов и короба
            приложение не изменяет.
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
            "После обновления списка поставок ранее созданные файлы "
            "удаляются из текущей сессии."
        )

    if load_clicked:
        invalidate_result()

        st.session_state["supplies"] = None
        st.session_state["manual_supply_ids"] = ""
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

    filter_col_1, filter_col_2, filter_col_3 = st.columns([1, 1, 2])

    with filter_col_1:
        period_name = st.selectbox(
            "Период создания поставки",
            options=[
                "Последний 1 день",
                "Последние 3 дня",
                "Последние 7 дней",
                "Последние 30 дней",
                "Последние 90 дней",
                "Все поставки",
            ],
            index=0,
            help=(
                "Фильтруется поле WB `createdAt`: дата создания поставки "
                "в часовой зоне Europe/Moscow. Это не дата создания заказа."
            ),
        )

    with filter_col_2:
        status_filter = st.selectbox(
            "Статус поставки",
            options=[
                "Все",
                "Только активные",
                "Только завершённые",
            ],
            index=0,
            help=(
                "Используется поле WB `done`: "
                "активная поставка имеет значение done = false."
            ),
        )

    with filter_col_3:
        supply_search = st.text_input(
            "Поиск поставки",
            placeholder="Введите часть названия или ID",
        )

    period_days_map = {
        "Последний 1 день": 1,
        "Последние 3 дня": 3,
        "Последние 7 дней": 7,
        "Последние 30 дней": 30,
        "Последние 90 дней": 90,
        "Все поставки": None,
    }

    period_days = period_days_map[period_name]

    visible_ids = [
        supply_id
        for supply_id, supply in supply_by_id.items()
        if supply_matches_filters(
            supply=supply,
            search_text=supply_search,
            period_days=period_days,
            status_filter=status_filter,
        )
    ]

    visible_ids = sort_supply_ids(
        supply_ids=visible_ids,
        supply_by_id=supply_by_id,
    )

    today = datetime.now(MOSCOW_TZ).date()
    today_ids = []

    for supply_id, supply in supply_by_id.items():
        created_at = parse_created_at(supply.get("createdAt"))

        if created_at is not None and created_at.date() == today:
            today_ids.append(supply_id)

    today_ids = sort_supply_ids(
        supply_ids=today_ids,
        supply_by_id=supply_by_id,
    )

    st.caption(
        f"Поставок, подходящих под фильтры: {len(visible_ids)}. "
        f"Создано сегодня по Москве: {len(today_ids)}."
    )

    widget_key = (
        f"supplies_{st.session_state['supply_widget_version']}"
    )

    preset_ids = st.session_state.pop("preset_supply_ids", None)

    if preset_ids is not None:
        st.session_state[widget_key] = [
            supply_id
            for supply_id in preset_ids
            if supply_id in supply_by_id
        ]

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
        "Найдите поставки по названию или ID и выберите нужные",
        options=option_ids,
        format_func=lambda supply_id: format_supply_label(
            supply_by_id[supply_id]
        ),
        key=widget_key,
        placeholder="Выберите минимум две поставки",
    )

    action_col_1, action_col_2, action_col_3 = st.columns([1.3, 1, 2.7])

    with action_col_1:
        add_today_clicked = st.button(
            "➕ Добавить созданные сегодня",
            use_container_width=True,
            disabled=not today_ids,
        )

    with action_col_2:
        clear_selection_clicked = st.button(
            "🧹 Очистить выбор",
            use_container_width=True,
        )

    if add_today_clicked:
        st.session_state["preset_supply_ids"] = list(
            dict.fromkeys(
                [
                    *selected_ids,
                    *today_ids,
                ]
            )
        )

        invalidate_result()
        st.session_state["supply_widget_version"] += 1
        st.rerun()

    if clear_selection_clicked:
        st.session_state["preset_supply_ids"] = []
        st.session_state["manual_supply_ids"] = ""

        invalidate_result()
        st.session_state["supply_widget_version"] += 1
        st.rerun()

    manual_ids_text = st.text_input(
        "Добавить ID поставки вручную, через запятую",
        placeholder="Например: WB-GI-1234567, WB-GI-1234568",
        key="manual_supply_ids",
    )

    manual_ids = [
        value.strip()
        for value in manual_ids_text.split(",")
        if value.strip()
    ]

    all_ids = list(
        dict.fromkeys(
            [
                *selected_ids,
                *manual_ids,
            ]
        )
    )

    if all_ids:
        st.success(f"Выбрано поставок: {len(all_ids)}")

    if len(all_ids) == 1:
        st.warning(
            "Для рабочей группировки выберите минимум две поставки. "
            "Одну поставку используйте только для пробной проверки."
        )

    st.divider()
    st.subheader("Параметры формирования файла")

    settings_col_1, settings_col_2, settings_col_3 = st.columns([1, 1, 2])

    with settings_col_1:
        lookback_days = st.selectbox(
            "Период поиска заданий WB",
            options=[7, 31, 90, 180],
            index=0,
            format_func=lambda days: f"Последние {days} дней",
            help=(
                "WB выдаёт список заданий интервалами не более "
                "30 календарных дней. Чем больше период, тем больше "
                "время ожидания и число запросов."
            ),
        )

    with settings_col_2:
        sticker_size_name = st.selectbox(
            "Размер стикера WB",
            options=list(STICKER_SIZES.keys()),
            index=0,
        )

    with settings_col_3:
        test_mode = st.checkbox(
            "Тестовый режим: разрешить одну поставку",
            value=False,
            help=(
                "Используйте только для первой проверки токена, "
                "стикеров WB и пробной печати."
            ),
        )

    sticker_width, sticker_height = STICKER_SIZES[sticker_size_name]

    minimum_supply_count = 1 if test_mode else 2

    selected_supplies = [
        supply_by_id.get(supply_id, {"id": supply_id})
        for supply_id in all_ids
    ]

    result_signature = (
        tuple(sorted(all_ids)),
        lookback_days,
        sticker_width,
        sticker_height,
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
        disabled=len(all_ids) < minimum_supply_count,
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

                # Общий PDF: разделитель перед каждой группой.
                full_pdf = make_pdf(
                    groups=summary["groups"],
                    width_mm=sticker_width,
                    height_mm=sticker_height,
                    include_group_separators=True,
                )

                # PDF отдельного артикула:
                # также начинается со служебной этикетки.
                article_pdfs: list[tuple[str, bytes]] = []

                for group in summary["groups"]:
                    article_pdf = make_pdf(
                        groups=[group],
                        width_mm=sticker_width,
                        height_mm=sticker_height,
                        include_group_separators=True,
                    )

                    article_pdfs.append(
                        (
                            group["article"],
                            article_pdf,
                        )
                    )

            st.session_state["result"] = {
                "signature": result_signature,
                "summary": summary,
                "full_pdf": full_pdf,
                "article_pdfs": article_pdfs,
                "sticker_size_name": sticker_size_name,
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
    """Запускает приложение и выбирает экран входа или рабочий экран."""
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
