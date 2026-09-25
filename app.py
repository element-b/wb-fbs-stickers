from __future__ import annotations

import hmac
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from pdf_export import make_pdf
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
    page_title="Стикеры WB FBS",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# СТИЛИ
# ============================================================

def apply_styles() -> None:
    """Применяет основные стили интерфейса."""
    st.markdown(
        """
        <style>
            .stApp {
                background-color: #FFFFFF;
                color: #000000;
            }

            .main .block-container {
                max-width: 1450px;
                padding: 2rem 3rem 3rem 3rem;
            }

            .stApp h1,
            .stApp h2,
            .stApp h3 {
                color: #000000 !important;
                font-weight: 650;
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
            }

            section[data-testid="stSidebar"] .stButton > button:hover {
                background-color: #202020;
                border-color: #777777;
            }

            .stButton > button {
                background-color: #009B77;
                color: #FFFFFF !important;
                border: none;
                border-radius: 7px;
                padding: 9px 16px;
                font-weight: 600;
            }

            .stButton > button:hover {
                background-color: #008268;
                box-shadow: 0 3px 10px rgba(0, 155, 119, 0.28);
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

            .description-box {
                border-left: 4px solid #009B77;
                background: #F2FBF7;
                border-radius: 6px;
                padding: 14px 18px;
                margin-bottom: 24px;
                color: #1F2937;
            }

            .login-card {
                max-width: 430px;
                margin: 7rem auto 0 auto;
                padding: 2rem;
                background: #FFFFFF;
                border: 1px solid #E5E7EB;
                border-radius: 14px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# SESSION STATE
# ============================================================

def init_session_state() -> None:
    """Создаёт начальные значения состояния пользовательской сессии."""
    defaults = {
        "authenticated": False,
        "supplies": None,
        "result": None,
        "supply_widget_version": 0,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================

def get_users() -> dict[str, str]:
    """
    Читает сотрудников из Streamlit Secrets.

    Ожидаемая конфигурация:

    [users]
    kladovshik = "случайный-длинный-пароль"
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
    """
    Проверяет пару логин/пароль.

    Все записи перебираются полностью, без раннего выхода из цикла.
    """
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
    st.markdown('<div class="login-card">', unsafe_allow_html=True)

    st.title("📦 WB FBS")
    st.caption("Получение и группировка стикеров сборочных заданий.")

    with st.form("login_form", clear_on_submit=False):
        username = st.text_input(
            "Логин",
            autocomplete="username",
        )

        password = st.text_input(
            "Пароль",
            type="password",
            autocomplete="current-password",
        )

        submitted = st.form_submit_button(
            "Войти",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if authenticate(username, password, users):
            st.session_state["authenticated"] = True
            st.rerun()

        st.error("Неверный логин или пароль.")

    st.markdown("</div>", unsafe_allow_html=True)


def logout() -> None:
    """Очищает сессию, включая PDF и стикеры в памяти."""
    st.session_state.clear()
    st.rerun()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def parse_created_at_to_moscow_date(value: object):
    """Возвращает дату создания поставки в зоне Europe/Moscow."""
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        return (
            datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
            .astimezone(MOSCOW_TZ)
            .date()
        )
    except ValueError:
        return None


def format_supply_label(supply: dict) -> str:
    """Формирует подпись поставки для селектора."""
    supply_id = str(supply.get("id", "")).strip()
    name = str(supply.get("name") or "Без названия").strip()
    created_at = str(supply.get("createdAt") or "дата неизвестна")

    return f"{name}  |  {supply_id}  |  {created_at}"


def invalidate_result() -> None:
    """Удаляет ранее сформированный PDF из текущей сессии."""
    st.session_state["result"] = None


# ============================================================
# БОКОВАЯ ПАНЕЛЬ
# ============================================================

def render_sidebar() -> None:
    """Отображает боковую панель."""
    with st.sidebar:
        st.markdown("## 📦 WB FBS")
        st.caption("Стикеры сборочных заданий")
        st.caption("Поставки, заказы и статусы не изменяются.")

        st.divider()

        st.markdown("**Как пользоваться**")
        st.markdown(
            """
            1. Загрузите список поставок.
            2. Выберите поставки WB.
            3. Нажмите «Обновить данные и сформировать файл».
            4. Проверьте сводную таблицу.
            5. Скачайте PDF для печати.
            """
        )

        st.divider()

        st.caption(
            "Стикеры и PDF хранятся только в памяти текущей сессии."
        )

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
    """Отображает основной экран работы с поставками."""
    render_sidebar()

    st.title("📦 Стикеры сборочных заданий WB FBS")

    st.markdown(
        """
        <div class="description-box">
            Сначала создайте поставки вручную в WB Seller. Затем выберите
            нужные поставки и сформируйте PDF. Стикеры одного артикула
            будут идти подряд. Приложение не создаёт поставки, не изменяет
            статусы заказов, не управляет коробами и не печатает ярлыки поставок.
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
            "При обновлении списка поставок предыдущий сформированный "
            "PDF автоматически удаляется из текущей сессии."
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

    for item in supplies:
        if not isinstance(item, dict):
            continue

        supply_id = str(item.get("id", "")).strip()

        if supply_id:
            supply_by_id[supply_id] = item

    if not supply_by_id:
        st.error("WB вернул список поставок без корректных идентификаторов.")
        st.stop()

    labels = {
        supply_id: format_supply_label(supply)
        for supply_id, supply in supply_by_id.items()
    }

    # Новый ключ нужен для полного сброса multiselect после обновления WB.
    widget_key = (
        f"supplies_{st.session_state['supply_widget_version']}"
    )

    # Этот блок должен быть ДО создания multiselect.
    # Иначе Streamlit не позволит программно изменить значение виджета.
    preset_ids = st.session_state.pop("preset_supply_ids", None)

    if preset_ids is not None:
        st.session_state[widget_key] = [
            supply_id
            for supply_id in preset_ids
            if supply_id in labels
        ]

    st.divider()
    st.subheader("Выбор поставок")

    selected_ids = st.multiselect(
        "Найдите поставку по названию или ID и выберите нужные",
        options=list(labels.keys()),
        format_func=lambda supply_id: labels[supply_id],
        key=widget_key,
        placeholder="Начните вводить название или ID поставки",
    )

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

    today = datetime.now(MOSCOW_TZ).date()

    today_ids = [
        supply_id
        for supply_id, supply in supply_by_id.items()
        if parse_created_at_to_moscow_date(
            supply.get("createdAt")
        ) == today
    ]

    with st.expander("Фильтр «За сегодня»", expanded=False):
        st.caption(
            "Фильтруется поле `createdAt` — дата создания поставки WB "
            "в часовой зоне Europe/Moscow. Это не дата создания заказа."
        )

        st.caption(
            f"Поставок с датой создания сегодня: {len(today_ids)}."
        )

        if st.button(
            "Добавить поставки, созданные сегодня",
            use_container_width=True,
        ):
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

    settings_col_1, settings_col_2 = st.columns(2)

    with settings_col_1:
        lookback_days = st.selectbox(
            "Период поиска данных сборочных заданий",
            options=[31, 90, 180],
            index=0,
            format_func=lambda days: f"Последние {days} дней",
            help=(
                "WB позволяет получать заказы только интервалами "
                "до 30 календарных дней. Больший период увеличивает "
                "число запросов и время ожидания."
            ),
        )

    with settings_col_2:
        sticker_size_name = st.selectbox(
            "Размер оригинального стикера WB",
            options=list(STICKER_SIZES.keys()),
            index=0,
            help=(
                "Размер должен совпадать с реальным размером термоэтикетки "
                "и настройкой драйвера принтера."
            ),
        )

    sticker_width, sticker_height = STICKER_SIZES[sticker_size_name]

    test_mode = st.checkbox(
        "Тестовый режим: разрешить формирование PDF по одной поставке",
        value=False,
        help=(
            "Используйте только для первой безопасной проверки токена, "
            "метода получения стикеров и пробной печати. "
            "В обычной работе выберите минимум две поставки."
        ),
    )

    minimum_supply_count = 1 if test_mode else 2

    if all_ids:
        st.caption(f"Выбрано поставок: {len(all_ids)}")

    if len(all_ids) == 1 and not test_mode:
        st.warning(
            "Для рабочей группировки выберите минимум две поставки. "
            "Для проверки одной поставки включите тестовый режим."
        )

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

    result = st.session_state.get("result")

    if result is not None and result["signature"] != result_signature:
        invalidate_result()
        result = None

    st.markdown("<br>", unsafe_allow_html=True)

    generate_clicked = st.button(
        "🔄 Обновить данные и сформировать файл",
        type="primary",
        disabled=len(all_ids) < minimum_supply_count,
        use_container_width=True,
    )

    if generate_clicked:
        # Даже если WB ответит ошибкой, старый файл скачать нельзя.
        invalidate_result()

        try:
            with st.spinner(
                "Получаю задания, проверяю артикулы и запрашиваю стикеры WB…"
            ):
                summary = collect_and_group(
                    client=client,
                    selected_supplies=selected_supplies,
                    lookback_days=lookback_days,
                    sticker_width=sticker_width,
                    sticker_height=sticker_height,
                )

                pdf = make_pdf(
                    groups=summary["groups"],
                    width_mm=sticker_width,
                    height_mm=sticker_height,
                )

            st.session_state["result"] = {
                "signature": result_signature,
                "summary": summary,
                "pdf": pdf,
                "sticker_size_name": sticker_size_name,
            }

            st.success(
                "Данные полностью проверены. PDF готов к скачиванию."
            )

        except (WBApiError, DataCheckError, ValueError) as error:
            st.error(str(error))

    result = st.session_state.get("result")

    if result is None or result["signature"] != result_signature:
        st.info(
            "Файл для скачивания появится только после успешной полной "
            "проверки выбранных поставок."
        )
        st.stop()

    summary = result["summary"]

    st.divider()
    st.subheader("Сводка")

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
        "Заданий / стикеров",
        summary["order_count"],
    )

    metric_4.metric(
        "Пустых поставок",
        len(summary["empty_supplies"]),
    )

    if summary["empty_supplies"]:
        st.warning(
            "В выбранных поставках нет заданий: "
            + ", ".join(summary["empty_supplies"])
        )

    table = pd.DataFrame(
        [
            {
                "Артикул продавца": group["article"],
                "Количество заданий / стикеров": len(
                    group["order_ids"]
                ),
                "Количество поставок": group["supply_count"],
            }
            for group in summary["groups"]
        ]
    )

    table_col_1, table_col_2 = st.columns([2, 1])

    with table_col_1:
        article_search = st.text_input(
            "Поиск по артикулу",
            placeholder="Введите часть артикула",
        )

    with table_col_2:
        sort_by = st.selectbox(
            "Сортировка",
            options=[
                "Артикул продавца",
                "Количество заданий / стикеров",
                "Количество поставок",
            ],
        )

    if article_search:
        table = table[
            table["Артикул продавца"].str.contains(
                article_search,
                case=False,
                regex=False,
                na=False,
            )
        ]

    st.dataframe(
        table.sort_values(
            by=sort_by,
            kind="stable",
        ),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Артикул продавца": st.column_config.TextColumn(
                "Артикул продавца",
                width="large",
            ),
            "Количество заданий / стикеров": (
                st.column_config.NumberColumn(
                    "Количество заданий / стикеров",
                    format="%d",
                )
            ),
            "Количество поставок": st.column_config.NumberColumn(
                "Количество поставок",
                format="%d",
            ),
        },
    )

    st.success(
        "Для каждого выбранного задания найден ровно один пригодный стикер."
    )

    st.warning(
        f"PDF сформирован в размере {result['sticker_size_name']}. "
        "Перед рабочей партией обязательно выполните пробную печать "
        "со масштабом 100% или «Фактический размер». Не включайте "
        "«Подогнать под страницу»."
    )

    timestamp = datetime.now(MOSCOW_TZ).strftime("%Y%m%d_%H%M%S")

    st.download_button(
        label="📥 Скачать последовательный PDF",
        data=result["pdf"],
        file_name=f"wb_fbs_stickers_{timestamp}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def main() -> None:
    """Запускает Streamlit-приложение."""
    apply_styles()
    init_session_state()

    users = get_users()

    if not st.session_state["authenticated"]:
        render_login_page(users)
        st.stop()

    try:
        token = str(st.secrets["WB_API_TOKEN"]).strip()
    except Exception:
        st.error(
            "Добавьте `WB_API_TOKEN` в Streamlit Secrets."
        )
        st.stop()

    if not token:
        st.error("`WB_API_TOKEN` в Streamlit Secrets пуст.")
        st.stop()

    client = WBClient(token=token)

    render_main_page(client)


if __name__ == "__main__":
    main()