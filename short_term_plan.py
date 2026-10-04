from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


class ShortTermPlanError(RuntimeError):
    """Ошибка формирования краткосрочного WB FBS-плана."""


@dataclass(frozen=True)
class ShortTermPlanResult:
    """Итог расчёта краткосрочного производственного плана."""

    queue_table: pd.DataFrame
    full_table: pd.DataFrame
    normal_table: pd.DataFrame
    total_completed_units: int
    total_need_to_finish: int
    critical_count: int
    urgent_count: int
    replenish_count: int
    normal_count: int
    missing_stock_articles: list[str]


def _as_nonnegative_int(
    value: object,
    field_name: str,
) -> int:
    """Проверяет количество завершённых WB FBS-заданий."""
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ShortTermPlanError(
            f"Некорректное значение `{field_name}`."
        ) from error

    if number < 0:
        raise ShortTermPlanError(
            f"Значение `{field_name}` не может быть отрицательным."
        )

    return number


def _priority(
    coverage_days: float,
    need_to_finish: int,
) -> tuple[int, str, str]:
    """Возвращает приоритет, статус и рекомендацию."""
    if need_to_finish <= 0:
        return (
            4,
            "🟢 Норма",
            "Производство не требуется.",
        )

    if coverage_days < 1:
        return (
            1,
            "🔴 Критично",
            "Сейчас: активировать триммер и передать на упаковку.",
        )

    if coverage_days < 2:
        return (
            2,
            "🟠 Срочно",
            "Сегодня: активировать триммер и передать на упаковку.",
        )

    return (
        3,
        "🟡 Пополнить",
        "Добавить триммер и упаковку в ближайшую очередь.",
    )


def build_short_term_plan(
    completed_fbs_by_article: dict[str, int],
    physical_stock_by_article: dict[str, int],
) -> ShortTermPlanResult:
    """
    Формирует краткосрочный план пополнения готовой продукции WB FBS.

    Источник недельной потребности — сборочные задания, входящие
    в завершённые WB FBS-поставки за последние семь завершённых дней.

    Правила первого этапа:

    - целевой остаток равен фактическому выбытию за неделю;
    - используется только физический остаток `stock`;
    - reserve и quantity МоегоСклада не используются;
    - рулоны считаются заранее подготовленными;
    - количество на триммер равно количеству на упаковку.
    """
    normalized_demand: dict[str, int] = {}

    for article, quantity in completed_fbs_by_article.items():
        clean_article = str(article or "").strip()

        if not clean_article:
            continue

        normalized_demand[clean_article] = _as_nonnegative_int(
            value=quantity,
            field_name=(
                "потребление WB FBS "
                f"для артикула `{clean_article}`"
            ),
        )

    normalized_demand = {
        article: quantity
        for article, quantity in normalized_demand.items()
        if quantity > 0
    }

    if not normalized_demand:
        raise ShortTermPlanError(
            "За последние 7 завершённых дней не найдено "
            "завершённых WB FBS-поставок с заданиями."
        )

    normalized_stock: dict[str, int] = {}

    for article, quantity in physical_stock_by_article.items():
        clean_article = str(article or "").strip()

        if not clean_article:
            continue

        try:
            normalized_stock[clean_article] = int(quantity)
        except (TypeError, ValueError) as error:
            raise ShortTermPlanError(
                f"Некорректный остаток у артикула `{clean_article}`."
            ) from error

    internal_rows: list[dict] = []
    missing_stock_articles: list[str] = []

    for article in sorted(
        normalized_demand,
        key=lambda value: (value.casefold(), value),
    ):
        completed_units = normalized_demand[article]
        average_daily_demand = completed_units / 7

        physical_stock = normalized_stock.get(article, 0)

        if article not in normalized_stock:
            missing_stock_articles.append(article)

        target_stock = completed_units

        need_to_finish = max(
            target_stock - physical_stock,
            0,
        )

        coverage_days = (
            physical_stock / average_daily_demand
            if average_daily_demand > 0
            else 0.0
        )

        priority_number, status, action = _priority(
            coverage_days=coverage_days,
            need_to_finish=need_to_finish,
        )

        internal_rows.append(
            {
                "_priority_number": priority_number,
                "_coverage_days_raw": coverage_days,
                "Приоритет": status,
                "Артикул": article,
                "WB FBS, завершено за 7 дней": completed_units,
                "Среднее выбытие WB FBS в день": round(
                    average_daily_demand,
                    2,
                ),
                "Физический остаток": physical_stock,
                "Покрытие, дней": round(
                    coverage_days,
                    2,
                ),
                "Целевой остаток, 7 дней": target_stock,
                "Нужно довести до готовой продукции": need_to_finish,
                "На триммер": need_to_finish,
                "На упаковку": need_to_finish,
                "Действие": action,
            }
        )

    internal_rows.sort(
        key=lambda row: (
            row["_priority_number"],
            row["_coverage_days_raw"],
            -row["Нужно довести до готовой продукции"],
            -row["WB FBS, завершено за 7 дней"],
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    visible_columns = [
        "Приоритет",
        "Артикул",
        "WB FBS, завершено за 7 дней",
        "Среднее выбытие WB FBS в день",
        "Физический остаток",
        "Покрытие, дней",
        "Целевой остаток, 7 дней",
        "Нужно довести до готовой продукции",
        "На триммер",
        "На упаковку",
        "Действие",
    ]

    full_table = pd.DataFrame(
        [
            {
                column: row[column]
                for column in visible_columns
            }
            for row in internal_rows
        ]
    )

    queue_table = full_table[
        full_table["Нужно довести до готовой продукции"] > 0
    ].copy()

    normal_table = full_table[
        full_table["Нужно довести до готовой продукции"] == 0
    ].copy()

    return ShortTermPlanResult(
        queue_table=queue_table,
        full_table=full_table,
        normal_table=normal_table,
        total_completed_units=sum(normalized_demand.values()),
        total_need_to_finish=int(
            queue_table[
                "Нужно довести до готовой продукции"
            ].sum()
        ),
        critical_count=sum(
            row["_priority_number"] == 1
            for row in internal_rows
        ),
        urgent_count=sum(
            row["_priority_number"] == 2
            for row in internal_rows
        ),
        replenish_count=sum(
            row["_priority_number"] == 3
            for row in internal_rows
        ),
        normal_count=sum(
            row["_priority_number"] == 4
            for row in internal_rows
        ),
        missing_stock_articles=missing_stock_articles,
    )