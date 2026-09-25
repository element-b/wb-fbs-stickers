from __future__ import annotations

import hmac
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from pdf_export import make_pdf
from pipeline import DataCheckError, collect_and_group
from wb_api import WBApiError, WBClient


st.set_page_config(
    page_title="Стикеры WB FBS",
    page_icon="📦",
    layout="wide",
)

st.markdown(
    """
    <style>
    .stApp {
        background: #fff;
        color: #000;
    }

    .main .block-container {
        max-width: 1450px;
        padding: 2rem 3rem;
    }

    section[data-testid="stSidebar"] {
        background: #000;
    }

    section[data-testid="stSidebar"] * {
        color: #fff !important;
    }

    .stButton > button,
    .stDownloadButton > button {
        background: #009B77;
        color: white !important;
        border: 0;
        border-radius: 7px;
        font-weight: 600;
        min-height: 2.7rem;
    }

    div[data-testid="metric-container"] {
        background: #fafafa;
        border: 1px solid #e5e7eb;
        border-radius: 10px;
        padding: 12px 16px;
    }

    .description-box {
        border-left: 4px solid #009B77;
        background: #F2FBF7;
        border-radius: 6px;
        padding: 14px 18px;
        margin-bottom: 20px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_users() -> dict[str, str]:
    """Читает сотрудников из Streamlit Secrets и проверяет конфигурацию."""
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
        st.error("Таблица `[users]` в Streamlit Secrets настроена некорректно.")
        st.stop()

    if not users:
        st.error("В таблице `[users]` нет ни одного пользователя.")
        st.stop()

    too_short = [
        username
        for username, password in users.items()
        if len(username) < 33 or len(password) < 33
    ]
    if too_short:
        st.error(
            "Для каждого пользователя логин и пароль в Secrets "
            "должны содержать не менее 33 символов. "
            "Проверьте конфигурацию в Streamlit."
        )
        st.stop()

    return users


def authenticate(username: str, password: str, users: dict[str, str]) -> bool:
    """
    Сравнивает введённые данные с каждым пользователем.

    Логин и пароль сравниваются через compare_digest. Проверка выполняется
    для всех записей, чтобы не прекращать цикл на первом совпадении.
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
        matched = matched or (username_matches and password_matches)

    return matched


def render_login_page(users: dict[str, str]) -> None:
    st.title("📦 Вход — стикеры WB FBS")
    st.caption("Введите выданные вам логин и пароль.")

    with st.form("login_form", clear_on_submit=True):
        username = st.text_input(
            "Логин",
            max_chars=128,
            autocomplete="username",
        )
        password = st.text_input(
            "Пароль",
            type="password",
            max_chars=128,
            autocomplete="current-password",
        )
        submitted = st.form_submit_button(
            "Войти",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        # Намеренно не удаляем пробелы и не меняем регистр:
        # введённые секреты должны совпадать с Secrets точно.
        if authenticate(username, password, users):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Неверный логин или пароль.")


users = get_users()

if not st.session_state.get("authenticated", False):
    render_login_page(users)
    st.stop()


with st.sidebar:
    st.markdown("## 📦 WB FBS")
    st.caption("Вход выполнен")
    st.caption("Только выбор поставок и получение стикеров.")

    if st.button("Выйти", use_container_width=True):
        # Очищаем состояние сессии, включая результат со стикерами в памяти.
        st.session_state.clear()
        st.rerun()


try:
    token = str(st.secrets["WB_API_TOKEN"])
except Exception:
    st.error("Добавьте `WB_API_TOKEN` в Streamlit Secrets.")
    st.stop()

if not token.strip():
    st.error("`WB_API_TOKEN` в Streamlit Secrets пуст.")
    st.stop()

client = WBClient(token)

if "supplies" not in st.session_state:
    st.session_state["supplies"] = None

if "result" not in st.session_state:
    st.session_state["result"] = None

if "supply_widget_version" not in st.session_state:
    st.session_state["supply_widget_version"] = 0


st.title("📦 Стикеры сборочных заданий WB FBS")

st.markdown(
    """
    <div class="description-box">
    Сначала вручную создайте сборки в WB Seller. Затем выберите одну или
    несколько поставок, например «Накл Ростов от 23.09.2026», и обновите
    данные. Приложение не меняет заказы, поставки, статусы и короба.
    </div>
    """,
    unsafe_allow_html=True,
)


load_col, _ = st.columns([1, 3])

with load_col:
    if st.button(
        "⟳ Загрузить / обновить поставки",
        use_container_width=True,
    ):
        st.session_state["supplies"] = None
        st.session_state["result"] = None
        st.session_state["supply_widget_version"] += 1

        try:
            with st.spinner("Загружаю список поставок WB…"):
                st.session_state["supplies"] = client.list_supplies()
        except WBApiError as exc:
            st.error(str(exc))


if st.session_state["supplies"] is None:
    st.info("Нажмите «Загрузить / обновить поставки».")
    st.stop()


supplies = st.session_state["supplies"]
supply_by_id = {
    str(item["id"]): item
    for item in supplies
    if item.get("id")
}

labels = {}
for supply_id, item in supply_by_id.items():
    labels[supply_id] = (
        f"{item.get('name') or 'Без названия'}  |  "
        f"{supply_id}  |  "
        f"{item.get('createdAt') or 'дата неизвестна'}"
    )

widget_key = f"supplies_{st.session_state['supply_widget_version']}"

selected_ids = st.multiselect(
    "Поставки — ищите по названию или ID, выберите несколько",
    options=list(labels),
    format_func=lambda value: labels[value],
    key=widget_key,
)

manual_ids_text = st.text_input(
    "Добавить ID вручную, через запятую",
    placeholder="WB-GI-1234567",
)

manual_ids = [
    value.strip()
    for value in manual_ids_text.split(",")
    if value.strip()
]

all_ids = list(dict.fromkeys([*selected_ids, *manual_ids]))


with st.expander("Фильтр «За сегодня»", expanded=False):
    st.caption(
        "Фильтруется дата создания поставки `createdAt`, локальная зона "
        "Europe/Moscow. Это не дата создания заказа."
    )

    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    today_ids = []

    for supply_id, item in supply_by_id.items():
        created = item.get("createdAt")
        if not created:
            continue

        try:
            created_date = datetime.fromisoformat(
                created.replace("Z", "+00:00")
            ).astimezone(ZoneInfo("Europe/Moscow")).date()

            if created_date == today:
                today_ids.append(supply_id)
        except (TypeError, ValueError):
            continue

    if st.button("Добавить поставки, созданные сегодня"):
        selected_with_today = list(dict.fromkeys([*all_ids, *today_ids]))

        # У виджета Streamlit нельзя менять состояние после его создания
        # в том же проходе. Меняем его ключ, затем перезапускаем страницу.
        st.session_state["preset_supply_ids"] = selected_with_today
        st.session_state["result"] = None
        st.session_state["supply_widget_version"] += 1
        st.rerun()


# Выбор, установленный кнопкой фильтра «За сегодня».
preset_ids = st.session_state.pop("preset_supply_ids", None)
if preset_ids is not None:
    all_ids = list(dict.fromkeys(preset_ids))


selection_signature = tuple(sorted(all_ids))

result = st.session_state.get("result")
if result and result["signature"] != selection_signature:
    st.session_state["result"] = None
    result = None

if all_ids:
    st.caption(f"Выбрано поставок: {len(all_ids)}")

chosen = [
    supply_by_id.get(supply_id, {"id": supply_id})
    for supply_id in all_ids
]


if st.button(
    "🔄 Обновить данные и сформировать файл",
    type="primary",
    disabled=not all_ids,
    use_container_width=True,
):
    # Предыдущий результат становится недоступен сразу при новом обновлении.
    st.session_state["result"] = None

    try:
        with st.spinner(
            "Получаю задания и стикеры, проверяю полноту результата…"
        ):
            summary = collect_and_group(client, chosen)
            pdf = make_pdf(summary["groups"], 58, 40)

            st.session_state["result"] = {
                "summary": summary,
                "pdf": pdf,
                "signature": selection_signature,
            }

    except (WBApiError, DataCheckError, ValueError) as exc:
        st.error(str(exc))


result = st.session_state.get("result")

if not result or result["signature"] != selection_signature:
    st.info("Файл появится только после успешной полной проверки.")
    st.stop()


summary = result["summary"]

metric_1, metric_2, metric_3, metric_4 = st.columns(4)
metric_1.metric("Выбрано поставок", summary["supply_count"])
metric_2.metric("Уникальных артикулов", summary["article_count"])
metric_3.metric("Заданий / стикеров", summary["order_count"])
metric_4.metric("Пустых поставок", len(summary["empty_supplies"]))

if summary["empty_supplies"]:
    st.warning(
        "Пустые поставки: " + ", ".join(summary["empty_supplies"])
    )


table = pd.DataFrame([
    {
        "Артикул продавца": group["article"],
        "Количество заданий / стикеров": len(group["order_ids"]),
        "Количество поставок": group["supply_count"],
    }
    for group in summary["groups"]
])

search = st.text_input("Поиск по артикулу")
if search:
    table = table[
        table["Артикул продавца"].str.contains(
            search,
            case=False,
            regex=False,
        )
    ]

sort_by = st.selectbox(
    "Сортировка таблицы",
    [
        "Артикул продавца",
        "Количество заданий / стикеров",
        "Количество поставок",
    ],
)

st.dataframe(
    table.sort_values(sort_by, kind="stable"),
    use_container_width=True,
    hide_index=True,
)

st.success("Все выбранные задания сопоставлены с одним стикером.")
st.warning(
    "PDF — предварительный формат. Перед рабочей партией проверьте пробную "
    "печать и настройки масштаба принтера. Размер страницы сейчас 58×40 мм."
)

st.download_button(
    "📥 Скачать последовательный PDF",
    data=result["pdf"],
    file_name="wb_fbs_stickers.pdf",
    mime="application/pdf",
    use_container_width=True,
)