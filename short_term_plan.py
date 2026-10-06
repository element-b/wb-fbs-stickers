from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


class ShortTermPlanError(RuntimeError):
    """Ошибка формирования краткосрочного FBS-плана."""


@dataclass(frozen=True)
class ShortTermPlanResult:
    """Итог расчёта краткосрочного производственного плана."""

    queue_table: pd.DataFrame
    full_table: pd.DataFrame
    normal_table: pd.DataFrame

    total_wb_completed_units: int
    total_ozon_completed_units: int
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
    """Проверяет количество фактического потребления."""
    if isinstance(value, bool):
        raise ShortTermPlanError(
            f"Некорректное значение `{field_name}`."
        )

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


def _normalize_demand(
    values: dict[str, int],
    source_name: str,
) -> dict[str, int]:
    """
    Нормализует спрос по артикулу.

    Допустимо удалить только внешние пробелы.
    Регистр, внутренние пробелы, суффиксы и символы не меняются.
    """
    normalized: dict[str, int] = {}

    for article, quantity in values.items():
        clean_article = str(article or "").strip()

        if not clean_article:
            raise ShortTermPlanError(
                f"Получен пустой артикул в источнике `{source_name}`."
            )

        normalized[clean_article] = (
            normalized.get(clean_article, 0)
            + _as_nonnegative_int(
                value=quantity,
                field_name=(
                    f"потребление {source_name} "
                    f"для артикула `{clean_article}`"
                ),
            )
        )

    return {
        article: quantity
        for article, quantity in normalized.items()
        if quantity > 0
    }


def _normalize_stock(
    values: dict[str, int],
) -> dict[str, int]:
    """Нормализует физические остатки МоегоСклада."""
    normalized: dict[str, int] = {}

    for article, quantity in values.items():
        clean_article = str(article or "").strip()

        if not clean_article:
            continue

        try:
            stock = int(quantity)
        except (TypeError, ValueError) as error:
            raise ShortTermPlanError(
                f"Некорректный остаток у артикула `{clean_article}`."
            ) from error

        normalized[clean_article] = stock

    return normalized


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
    completed_wb_fbs_by_article: dict[str, int],
    completed_ozon_fbs_by_article: dict[str, int],
    physical_stock_by_article: dict[str, int],
) -> ShortTermPlanResult:
    """
    Формирует общую краткосрочную очередь WB FBS + Ozon FBS.

    Правила:

    - TargetStock равен общему фактическому FBS-выбытию за 7 дней;
    - используется только `stock` МоегоСклада;
    - reserve, quantity и inTransit не учитываются;
    - WB article, Ozon offer_id и МойСклад article сопоставляются строго;
    - разрешается удалить только внешние пробелы;
    - количество на триммер равно количеству на упаковку.
    """
    normalized_wb = _normalize_demand(
        values=completed_wb_fbs_by_article,
        source_name="WB FBS",
    )

    normalized_ozon = _normalize_demand(
        values=completed_ozon_fbs_by_article,
        source_name="Ozon FBS",
    )

    normalized_stock = _normalize_stock(
        values=physical_stock_by_article,
    )

    all_articles = sorted(
        set(normalized_wb) | set(normalized_ozon),
        key=lambda value: (value.casefold(), value),
    )

    if not all_articles:
        raise ShortTermPlanError(
            "За последние 7 завершённых дней не найдено "
            "фактического FBS-потребления WB и Ozon."
        )

    internal_rows: list[dict] = []
    missing_stock_articles: list[str] = []

    for article in all_articles:
        wb_completed_units = normalized_wb.get(article, 0)
        ozon_completed_units = normalized_ozon.get(article, 0)

        completed_units = (
            wb_completed_units
            + ozon_completed_units
        )

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
                "WB FBS, завершено за 7 дней": (
                    wb_completed_units
                ),
                "Ozon FBS, завершено за 7 дней": (
                    ozon_completed_units
                ),
                "Всего FBS, завершено за 7 дней": (
                    completed_units
                ),
                "Среднее выбытие FBS в день": round(
                    average_daily_demand,
                    2,
                ),
                "Физический остаток": physical_stock,
                "Покрытие, дней": round(
                    coverage_days,
                    2,
                ),
                "Целевой остаток, 7 дней": target_stock,
                "Нужно довести до готовой продукции": (
                    need_to_finish
                ),
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
            -row["Всего FBS, завершено за 7 дней"],
            row["Артикул"].casefold(),
            row["Артикул"],
        )
    )

    visible_columns = [
        "Приоритет",
        "Артикул",
        "WB FBS, завершено за 7 дней",
        "Ozon FBS, завершено за 7 дней",
        "Всего FBS, завершено за 7 дней",
        "Среднее выбытие FBS в день",
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
        total_wb_completed_units=sum(
            normalized_wb.values()
        ),
        total_ozon_completed_units=sum(
            normalized_ozon.values()
        ),
        total_completed_units=(
            sum(normalized_wb.values())
            + sum(normalized_ozon.values())
        ),
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