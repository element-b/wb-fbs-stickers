from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from mysklad_api import MySkladApiError, MySkladClient
from short_term_export import make_short_term_plan_xlsx
from short_term_plan import (
    ShortTermPlanError,
    ShortTermPlanResult,
    build_short_term_plan,
)
from wb_statistics_api import (
    WBStatisticsApiError,
    WBStatisticsClient,
    seconds_until_orders_request,
)


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


st.set_page_config(
    page_title="Краткосрочный план WB FBS",
    page_icon="⚡",
    layout="wide",
)


def get_secret(name: str) -> str:
    """Безопасно читает строковый Streamlit Secret."""
    try:
        return str(st.secrets[name]).strip()
    except Exception:
        return ""


def apply_styles() -> None:
    """Применяет стили, согласованные с основной страницей."""
    st.markdown(
        """
        <style>
            .stApp {
                background-color: #FFFFFF;
                color: #000000;
            }

            .main .block-container {
                padding: 2rem 3rem 3rem 3rem;
                max-width: 1550px;
            }

            .stApp h1,
            .stApp h2,
            .stApp h3 {
                color: #000000 !important;
                font-weight: 650;
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

            .stButton > button,
            .stDownloadButton > button {
                background-color: #009B77;
                color: #FFFFFF !important;
                border: none;
                border-radius: 7px;
                padding: 9px 16px;
                font-weight: 600;
            }

            .stButton > button:hover,
            .stDownloadButton > button:hover {
                background-color: #008268;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def ensure_authenticated() -> None:
    """
    Не позволяет открыть производственную страницу без входа.

    Пользователь должен сначала войти на главной странице приложения.
    Используется существующий флаг авторизации из `app.py`.
    """
    if st.session_state.get("authenticated", False):
        return

    st.error(
        "Сначала войдите через главную страницу "
        "«Стикеры WB FBS»."
    )
    st.stop()


def ensure_configuration(
    wb_token: str,
    mysklad_token: str,
    store_id: str,
) -> None:
    """Проверяет обязательные Streamlit Secrets."""
    missing = []

    if not wb_token:
        missing.append("`WB_API_TOKEN`")

    if not mysklad_token:
        missing.append("`MYSKLAD_API_TOKEN`")

    if not store_id:
        missing.append(
            "`MYSKLAD_WB_FBS_FINISHED_STORE_ID`"
        )

    if missing:
        st.error(
            "Не настроены обязательные Streamlit Secrets: "
            + ", ".join(missing)
        )
        st.stop()


def render_plan_table(
    table: pd.DataFrame,
) -> None:
    """Выводит таблицу производственной очереди."""
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Приоритет": st.column_config.TextColumn(
                "Приоритет",
                width="medium",
            ),
            "Артикул": st.column_config.TextColumn(
                "Артикул",
                width="large",
            ),
            "WB FBS, заказов за 7 дней": (
                st.column_config.NumberColumn(
                    "WB FBS, заказов за 7 дней",
                    format="%d",
                )
            ),
            "Среднее WB FBS в день": (
                st.column_config.NumberColumn(
                    "Среднее WB FBS в день",
                    format="%.2f",
                )
            ),
            "Физический остаток": (
                st.column_config.NumberColumn(
                    "Физический остаток",
                    format="%d",
                )
            ),
            "Покрытие, дней": (
                st.column_config.NumberColumn(
                    "Покрытие, дней",
                    format="%.2f",
                )
            ),
            "Целевой остаток, 7 дней": (
                st.column_config.NumberColumn(
                    "Целевой остаток, 7 дней",
                    format="%d",
                )
            ),
            "Нужно довести до готовой продукции": (
                st.column_config.NumberColumn(
                    "Нужно довести до готовой продукции",
                    format="%d",
                )
            ),
            "На триммер": st.column_config.NumberColumn(
                "На триммер",
                format="%d",
            ),
            "На упаковку": st.column_config.NumberColumn(
                "На упаковку",
                format="%d",
            ),
            "Действие": st.column_config.TextColumn(
                "Действие",
                width="large",
            ),
        },
    )


def render_shift_list(
    result: ShortTermPlanResult,
) -> None:
    """Выводит короткий план, который можно передать Диме."""
    st.subheader("📋 Краткий план для производства")

    if result.queue_table.empty:
        st.success(
            "Все артикулы с WB FBS-спросом покрыты "
            "недельным физическим остатком."
        )
        return

    st.caption(
        "На первом этапе предполагается, что рулоны уже готовы. "
        "Поэтому количество для триммера и упаковки одинаково."
    )

    for index, row in enumerate(
        result.queue_table.to_dict(orient="records"),
        start=1,
    ):
        st.markdown(
            f"{index}. **{row['Артикул']}** — "
            f"`триммер: {row['На триммер']} шт.` → "
            f"`упаковка: {row['На упаковку']} шт.` "
            f"— {row['Приоритет']}."
        )


def main() -> None:
    """Точка входа страницы краткосрочного WB FBS-планирования."""
    ensure_authenticated()
    apply_styles()

    st.title("⚡ Краткосрочный план WB FBS")

    st.markdown(
        """
        <div class="description-box">
            После утренней сборки WB FBS проведите отгрузку или списание
            в МоемСкладе. Затем нажмите «Обновить потребности».
            Приложение сравнит физический остаток готовой продукции
            с недельным спросом WB FBS и сформирует очередь:
            что направить на триммер и далее на упаковку.
        </div>
        """,
        unsafe_allow_html=True,
    )

    wb_token = get_secret("WB_API_TOKEN")
    mysklad_token = get_secret("MYSKLAD_API_TOKEN")
    finished_store_id = get_secret(
        "MYSKLAD_WB_FBS_FINISHED_STORE_ID"
    )

    ensure_configuration(
        wb_token=wb_token,
        mysklad_token=mysklad_token,
        store_id=finished_store_id,
    )

    today = datetime.now(MOSCOW_TZ).date()

    # Используются семь завершённых московских дней.
    period_end = today - timedelta(days=1)
    period_start = period_end - timedelta(days=6)

    st.caption(
        "Период среднего WB FBS-спроса: "
        f"**{period_start.strftime('%d.%m.%Y')} — "
        f"{period_end.strftime('%d.%m.%Y')}**. "
        "Текущий день не включён, так как он ещё не завершён."
    )

    st.caption(
        "В расчёте используется только физический остаток `stock` "
        "выбранного склада. Резервы МоегоСклада сейчас намеренно "
        "не вычитаются."
    )

    cooldown_seconds = seconds_until_orders_request()

    if cooldown_seconds > 0:
        st.info(
            "WB Statistics API разрешает получать заказы "
            "не чаще одного раза в минуту. "
            f"Повторный запрос будет доступен примерно через "
            f"{cooldown_seconds} сек."
        )

    update_clicked = st.button(
        "⟳ Обновить потребности",
        type="primary",
        use_container_width=True,
    )

    if update_clicked:
        wb_client = WBStatisticsClient(
            token=wb_token
        )

        mysklad_client = MySkladClient(
            token=mysklad_token
        )

        try:
            with st.spinner(
                "Получаю физические остатки МоегоСклада "
                "и недельный спрос WB FBS…"
            ):
                store_name = mysklad_client.store_name(
                    store_id=finished_store_id
                )

                physical_stock_by_article = (
                    mysklad_client.finished_stock_by_article(
                        store_id=finished_store_id
                    )
                )

                wb_weekly_demand = wb_client.weekly_fbs_demand(
                    date_from=period_start,
                    date_to=period_end,
                )

                result = build_short_term_plan(
                    wb_weekly_demand=wb_weekly_demand,
                    physical_stock_by_article=(
                        physical_stock_by_article
                    ),
                )

            st.session_state["short_term_fbs_result"] = {
                "result": result,
                "period_start": period_start,
                "period_end": period_end,
                "store_name": store_name,
                "store_id": finished_store_id,
                "generated_at": datetime.now(MOSCOW_TZ),
            }

            st.success(
                "Потребности WB FBS успешно обновлены."
            )

        except (
            WBStatisticsApiError,
            MySkladApiError,
            ShortTermPlanError,
            ValueError,
        ) as error:
            st.error(str(error))

    saved = st.session_state.get(
        "short_term_fbs_result"
    )

    if saved is None:
        st.info(
            "После проведения утренней сборки в МоемСкладе "
            "нажмите «Обновить потребности»."
        )
        st.stop()

    result: ShortTermPlanResult = saved["result"]

    st.divider()
    st.subheader("📊 Сводка")

    metric_1, metric_2, metric_3, metric_4, metric_5 = st.columns(5)

    metric_1.metric(
        "WB FBS-заказов за 7 дней",
        result.total_wb_orders,
    )

    metric_2.metric(
        "Нужно на триммер",
        result.total_need_to_finish,
    )

    metric_3.metric(
        "Критично: менее 1 дня",
        result.critical_count,
    )

    metric_4.metric(
        "Срочно: 1–2 дня",
        result.urgent_count,
    )

    metric_5.metric(
        "Пополнить: 2–7 дней",
        result.replenish_count,
    )

    st.caption(
        "Склад: "
        f"`{saved['store_name']}`. "
        "Данные сформированы: "
        f"{saved['generated_at'].strftime('%d.%m.%Y %H:%M')} "
        "по Москве."
    )

    if result.critical_count > 0:
        st.error(
            "Есть критические артикулы с покрытием меньше одного дня. "
            "Их нужно поставить в начало очереди триммера и упаковки."
        )

    elif result.urgent_count > 0:
        st.warning(
            "Есть срочные артикулы с покрытием от одного до двух дней. "
            "Их рекомендуется закрыть в текущую смену."
        )

    elif result.replenish_count > 0:
        st.info(
            "Критических дефицитов нет, но часть артикулов "
            "нужно пополнить до недельного уровня."
        )

    render_shift_list(result)

    st.divider()
    st.subheader("🏭 Производственная очередь")

    if result.queue_table.empty:
        st.success(
            "Очередь пуста: все артикулы покрыты "
            "недельным WB FBS-остатком."
        )
    else:
        render_plan_table(result.queue_table)

    if result.missing_stock_articles:
        st.warning(
            "Следующие артикулы есть в WB FBS-спросе, но не найдены "
            "в отчёте выбранного склада МоегоСклада: "
            + ", ".join(result.missing_stock_articles)
            + ". Это может означать нулевой остаток, отсутствие товара "
            "на данном складе или несовпадение артикулов."
        )

    with st.expander(
        "Артикулы с достаточным остатком",
        expanded=False,
    ):
        if result.normal_table.empty:
            st.info(
                "Нет артикулов, полностью покрытых "
                "недельным уровнем спроса."
            )
        else:
            render_plan_table(result.normal_table)

    st.divider()
    st.subheader("📥 Выгрузка")

    try:
        generated_at = saved["generated_at"]

        export_bytes = make_short_term_plan_xlsx(
            queue_table=result.queue_table,
            full_table=result.full_table,
            parameters={
                "Период WB FBS-спроса": (
                    f"{saved['period_start'].strftime('%d.%m.%Y')} — "
                    f"{saved['period_end'].strftime('%d.%m.%Y')}"
                ),
                "Целевой уровень запаса": (
                    "Средняя потребность WB FBS за 7 дней"
                ),
                "Склад готовой продукции": saved["store_name"],
                "UUID склада": saved["store_id"],
                "Резервы МоегоСклада": (
                    "Не учитываются на первом этапе"
                ),
                "Время формирования": generated_at.strftime(
                    "%d.%m.%Y %H:%M"
                ),
                "Часовой пояс": "Europe/Moscow",
            },
        )

        file_timestamp = generated_at.strftime(
            "%Y%m%d_%H%M%S"
        )

        st.download_button(
            label="📊 Скачать краткосрочный план XLSX",
            data=export_bytes,
            file_name=(
                f"wb_fbs_short_term_plan_{file_timestamp}.xlsx"
            ),
            mime=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            use_container_width=True,
        )

    except ValueError as error:
        st.error(str(error))

    with st.expander(
        "Как интерпретировать результаты",
        expanded=False,
    ):
        st.markdown(
            """
            - **Физический остаток** — значение `stock` только
              выбранного склада готовой продукции МоегоСклада.
            - **Резервы** намеренно не участвуют в первом этапе.
            - **Целевой остаток, 7 дней** — число неотменённых
              WB FBS-заказов за последние семь завершённых дней.
            - **На триммер** — сколько единиц нужно запустить
              на триммере.
            - **На упаковку** — сколько единиц нужно передать
              на упаковку после триммера.
            - **Критично** — товара осталось меньше чем на один день
              среднего WB FBS-спроса.
            - **Срочно** — товара осталось от одного до двух дней.
            """
        )


if __name__ == "__main__":
    main()
