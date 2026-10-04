from __future__ import annotations

import math
import time
from datetime import date, datetime
from threading import Lock
from zoneinfo import ZoneInfo

import requests


BASE_URL = "https://statistics-api.wildberries.ru"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Документация WB ограничивает метод заказов:
# 1 запрос в минуту на кабинет.
ORDERS_REQUEST_INTERVAL_SECONDS = 60.0

_rate_limit_lock = Lock()
_last_orders_request_at: float | None = None


class WBStatisticsApiError(RuntimeError):
    """Ошибка безопасного обращения к WB Statistics API."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status


def _as_bool(value: object) -> bool:
    """Нормализует значение WB API к булевому типу."""
    if isinstance(value, bool):
        return value

    return str(value).strip().casefold() in {
        "1",
        "true",
        "yes",
        "да",
    }


def _parse_wb_datetime(value: object) -> datetime | None:
    """
    Преобразует строку WB API во время Москвы.

    Если WB не указал timezone, значение рассматривается как время Москвы,
    поскольку для Statistics API даты указаны в Moscow time zone.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=MOSCOW_TZ)

    return parsed.astimezone(MOSCOW_TZ)


def _parse_wb_order_date(value: object) -> date | None:
    """Возвращает московскую календарную дату заказа WB."""
    parsed = _parse_wb_datetime(value)

    return parsed.date() if parsed is not None else None


def seconds_until_orders_request() -> int:
    """
    Возвращает число секунд до следующего разрешённого запроса WB.

    Лимит хранится только в памяти текущего процесса. Сам WB API
    дополнительно применяет собственное серверное ограничение.
    """
    with _rate_limit_lock:
        last_request_at = _last_orders_request_at

    if last_request_at is None:
        return 0

    elapsed = time.monotonic() - last_request_at
    remaining = ORDERS_REQUEST_INTERVAL_SECONDS - elapsed

    return max(0, int(math.ceil(remaining)))


def _reserve_orders_request_slot() -> None:
    """
    Резервирует слот вызова Statistics API.

    Время фиксируется до HTTP-запроса, потому что даже неуспешный запрос
    может быть учтён Wildberries в лимите.
    """
    global _last_orders_request_at

    with _rate_limit_lock:
        if _last_orders_request_at is not None:
            elapsed = time.monotonic() - _last_orders_request_at
            remaining = ORDERS_REQUEST_INTERVAL_SECONDS - elapsed

            if remaining > 0:
                raise WBStatisticsApiError(
                    "WB ограничивает получение статистики заказов: "
                    "разрешён один запрос в минуту. "
                    f"Повторите через {math.ceil(remaining)} сек."
                )

        _last_orders_request_at = time.monotonic()


class WBStatisticsClient:
    """
    Клиент WB Statistics API.

    Используется только для чтения среднего спроса WB FBS.
    Данные в WB не создаются и не изменяются.
    """

    def __init__(
        self,
        token: str,
        timeout: int = 60,
    ) -> None:
        if not token.strip():
            raise ValueError(
                "Не задан `WB_API_TOKEN`."
            )

        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": token.strip(),
                "Accept": "application/json",
            }
        )

    def _orders_since(
        self,
        date_from: date,
    ) -> list[dict]:
        """
        Получает сведения о заказах из WB Statistics API.

        `dateFrom` для `flag=0` означает дату последнего изменения
        заказа, а не только дату его создания. После получения ответа
        приложение дополнительно фильтрует строки по полю `date`.

        WB ограничивает один ответ приблизительно 80 000 строками.
        Для первого этапа при достижении лимита расчёт блокируется,
        чтобы не строить план по неполным данным.
        """
        _reserve_orders_request_slot()

        try:
            response = self.session.get(
                f"{BASE_URL}/api/v1/supplier/orders",
                params={
                    "dateFrom": date_from.isoformat(),
                    "flag": 0,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise WBStatisticsApiError(
                "Сетевая ошибка при обращении к WB Statistics API. "
                "Проверьте интернет-подключение и повторите позже."
            ) from error

        if response.status_code in (401, 403):
            raise WBStatisticsApiError(
                "WB Statistics API отказал в доступе. "
                "Текущий `WB_API_TOKEN` не имеет нужной категории "
                "доступа «Статистика» либо токен недействителен. "
                "Для этого метода WB требует токен Statistics.",
                status=response.status_code,
            )

        if response.status_code == 429:
            raise WBStatisticsApiError(
                "WB ограничил частоту получения статистики заказов. "
                "Повторите обновление потребностей позже.",
                status=response.status_code,
            )

        if not response.ok:
            raise WBStatisticsApiError(
                "WB Statistics API вернул ошибку "
                f"HTTP {response.status_code}.",
                status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise WBStatisticsApiError(
                "WB Statistics API вернул ответ неизвестного формата."
            ) from error

        if not isinstance(payload, list):
            raise WBStatisticsApiError(
                "WB Statistics API вернул неожиданный формат заказов."
            )

        rows = [
            row
            for row in payload
            if isinstance(row, dict)
        ]

        if len(rows) >= 80_000:
            raise WBStatisticsApiError(
                "WB вернул слишком большой набор заказов. "
                "Один ответ метода ограничен примерно 80 000 строками, "
                "поэтому недельный спрос может быть неполным. "
                "На первом этапе неполный производственный план "
                "не формируется."
            )

        return rows

    def weekly_fbs_demand(
        self,
        date_from: date,
        date_to: date,
    ) -> dict[str, int]:
        """
        Возвращает недельный спрос WB FBS по артикулам продавца.

        В расчёт включаются только:

        - заказы с датой внутри периода;
        - warehouseType «Склад продавца»;
        - неотменённые заказы;
        - строки с непустым supplierArticle.

        Для идентификации заказа используется `srid`, как рекомендует WB.
        Если одна запись возвращена несколько раз, берётся её последнее
        состояние по `lastChangeDate`.
        """
        if date_from > date_to:
            raise ValueError(
                "Дата начала периода WB не может быть позже даты конца."
            )

        rows = self._orders_since(date_from=date_from)

        latest_order_by_srid: dict[str, dict] = {}
        latest_change_by_srid: dict[str, datetime] = {}

        for row in rows:
            order_date = _parse_wb_order_date(row.get("date"))

            if (
                order_date is None
                or order_date < date_from
                or order_date > date_to
            ):
                continue

            warehouse_type = str(
                row.get("warehouseType") or ""
            ).strip()

            if warehouse_type.casefold() != "склад продавца".casefold():
                continue

            article = str(
                row.get("supplierArticle") or ""
            ).strip()

            if not article:
                continue

            srid = str(row.get("srid") or "").strip()

            if not srid:
                raise WBStatisticsApiError(
                    "WB вернул FBS-заказ без `srid`. "
                    "Нельзя безопасно исключить повторные строки."
                )

            changed_at = _parse_wb_datetime(
                row.get("lastChangeDate")
            )

            if changed_at is None:
                changed_at = datetime.min.replace(
                    tzinfo=MOSCOW_TZ
                )

            previous_changed_at = latest_change_by_srid.get(srid)

            if (
                previous_changed_at is None
                or changed_at >= previous_changed_at
            ):
                latest_order_by_srid[srid] = row
                latest_change_by_srid[srid] = changed_at

        demand_by_article: dict[str, int] = {}

        for row in latest_order_by_srid.values():
            if _as_bool(row.get("isCancel")):
                continue

            article = str(
                row.get("supplierArticle") or ""
            ).strip()

            if not article:
                continue

            demand_by_article[article] = (
                demand_by_article.get(article, 0) + 1
            )

        return demand_by_article