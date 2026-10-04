from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests


BASE_URL = "https://marketplace-api.wildberries.ru"

# В документации FBS WB указан интервал 200 мс.
# Добавлен небольшой запас, чтобы не упираться в лимит.
MIN_REQUEST_INTERVAL_SECONDS = 0.21

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class WBApiError(RuntimeError):
    """Ошибка при безопасном обращении к WB API."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status


def is_cross_border(value: object) -> bool:
    """Возвращает признак трансграничного заказа или поставки."""
    try:
        return int(value or 0) != 0
    except (TypeError, ValueError):
        return bool(value)


def parse_wb_datetime(value: object) -> datetime | None:
    """
    Преобразует дату WB API во время Europe/Moscow.

    WB обычно возвращает ISO-даты с UTC-смещением или суффиксом Z.
    Если timezone отсутствует, значение трактуется как UTC.
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
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(MOSCOW_TZ)


def supply_is_done(supply: dict) -> bool:
    """Проверяет, завершена ли WB-поставка."""
    value = supply.get("done", False)

    if isinstance(value, bool):
        return value

    return str(value).strip().casefold() == "true"


class WBClient:
    """
    Клиент WB Marketplace API.

    Выполняет только операции чтения:

    - получение списка поставок;
    - получение ID сборочных заданий поставки;
    - получение данных сборочных заданий;
    - получение оригинальных стикеров;
    - получение фактического недельного FBS-потребления
      по завершённым поставкам.
    """

    def __init__(
        self,
        token: str,
        timeout: int = 30,
        retries: int = 3,
    ) -> None:
        if not token.strip():
            raise ValueError("Не задан API-токен WB.")

        self.timeout = timeout
        self.retries = retries
        self._last_request_at = 0.0

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": token.strip(),
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def _wait_for_rate_limit(self) -> None:
        """Выдерживает минимальный интервал между запросами WB."""
        elapsed = time.monotonic() - self._last_request_at
        delay = MIN_REQUEST_INTERVAL_SECONDS - elapsed

        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _retry_delay(
        response: requests.Response,
        attempt: int,
    ) -> float:
        """
        Возвращает задержку для повторной попытки.

        В приоритете заголовки WB или стандартный Retry-After.
        """
        retry_after = (
            response.headers.get("X-Ratelimit-Retry")
            or response.headers.get("Retry-After")
        )

        if retry_after:
            try:
                return min(
                    max(float(retry_after), 1.0),
                    60.0,
                )
            except ValueError:
                pass

        return min(float(2 ** attempt), 8.0)

    def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict:
        """
        Выполняет запрос WB API с ограниченными повторами.

        Не повторяет 401, 403, 409 и остальные постоянные ошибки клиента.
        Тело ответа не выводится, чтобы не раскрывать лишние данные.
        """
        url = f"{BASE_URL}{path}"

        for attempt in range(self.retries + 1):
            self._wait_for_rate_limit()

            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    timeout=self.timeout,
                    **kwargs,
                )
                self._last_request_at = time.monotonic()

            except requests.RequestException:
                if attempt >= self.retries:
                    raise WBApiError(
                        "Сетевая ошибка при обращении к WB API. "
                        "Проверьте подключение к интернету "
                        "и повторите позже."
                    )

                time.sleep(min(float(2 ** attempt), 8.0))
                continue

            if response.status_code in (401, 403):
                raise WBApiError(
                    f"WB отказал в доступе: HTTP "
                    f"{response.status_code}. "
                    "Проверьте токен, категорию Marketplace "
                    "и права токена.",
                    status=response.status_code,
                )

            if response.status_code == 409:
                raise WBApiError(
                    "WB API вернул HTTP 409. Запрос не будет повторён, "
                    "так как такой ответ учитывается WB как несколько "
                    "запросов в лимите. Повторите операцию позже.",
                    status=response.status_code,
                )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.retries:
                    raise WBApiError(
                        "Временная ошибка WB API: HTTP "
                        f"{response.status_code}. "
                        "Повторите операцию позже.",
                        status=response.status_code,
                    )

                time.sleep(
                    self._retry_delay(
                        response=response,
                        attempt=attempt,
                    )
                )
                continue

            if not response.ok:
                raise WBApiError(
                    f"WB API вернул ошибку HTTP "
                    f"{response.status_code}.",
                    status=response.status_code,
                )

            if not response.content:
                return {}

            try:
                data = response.json()
            except ValueError as error:
                raise WBApiError(
                    "WB API вернул ответ неизвестного формата."
                ) from error

            if not isinstance(data, dict):
                raise WBApiError(
                    "WB API вернул неожиданный формат ответа."
                )

            return data

        raise WBApiError("Не удалось выполнить запрос к WB API.")

    def list_supplies(self) -> list[dict]:
        """
        Получает список поставок через GET /api/v3/supplies.

        Корректно обрабатывает пагинацию через параметры limit и next.
        """
        supplies: list[dict] = []
        cursor: int | str = 0
        seen_cursors: set[int | str] = set()

        while True:
            data = self._request(
                method="GET",
                path="/api/v3/supplies",
                params={
                    "limit": 1000,
                    "next": cursor,
                },
            )

            batch = data.get("supplies")

            if not isinstance(batch, list):
                raise WBApiError(
                    "WB вернул неожиданный ответ списка поставок."
                )

            for item in batch:
                if isinstance(item, dict):
                    supplies.append(item)

            next_cursor = data.get("next", 0)

            if not next_cursor or not batch:
                break

            if next_cursor in seen_cursors:
                raise WBApiError(
                    "WB вернул повторный курсор списка поставок."
                )

            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return supplies

    def supply_order_ids(self, supply_id: str) -> list[int]:
        """
        Получает ID сборочных заданий конкретной поставки.

        Метод:
        GET /api/marketplace/v3/supplies/{supplyId}/order-ids
        """
        safe_supply_id = quote(supply_id, safe="")

        data = self._request(
            method="GET",
            path=(
                "/api/marketplace/v3/supplies/"
                f"{safe_supply_id}/order-ids"
            ),
        )

        order_ids = data.get("orderIds")

        if not isinstance(order_ids, list):
            raise WBApiError(
                "WB вернул неожиданный ответ со списком "
                "сборочных заданий поставки."
            )

        result: list[int] = []

        for value in order_ids:
            try:
                result.append(int(value))
            except (TypeError, ValueError) as error:
                raise WBApiError(
                    "WB вернул некорректный ID сборочного задания."
                ) from error

        return result

    def orders_for_ids(
        self,
        target_ids: set[int],
        lookback_days: int = 31,
    ) -> dict[int, dict]:
        """
        Ищет данные нужных сборочных заданий через GET /api/v3/orders.

        WB API не поддерживает точечный запрос произвольного списка ID,
        поэтому данные ищутся последовательными периодами до 30 дней.

        В память сохраняются только поля, нужные приложению:

        - article;
        - crossBorderType.

        Адреса, ФИО покупателей и прочие персональные данные
        не сохраняются.
        """
        if not target_ids:
            return {}

        if lookback_days < 1:
            raise ValueError(
                "Период поиска сборочных заданий должен быть "
                "не меньше одного дня."
            )

        end = datetime.now(timezone.utc)
        current_start = end - timedelta(days=lookback_days)

        found: dict[int, dict] = {}

        while current_start < end:
            window_end = min(
                current_start + timedelta(days=30),
                end,
            )

            page_cursor: int | str = 0
            seen_cursors: set[int | str] = set()

            while True:
                data = self._request(
                    method="GET",
                    path="/api/v3/orders",
                    params={
                        "limit": 1000,
                        "next": page_cursor,
                        "dateFrom": int(current_start.timestamp()),
                        "dateTo": int(window_end.timestamp()),
                    },
                )

                orders = data.get("orders")

                if not isinstance(orders, list):
                    raise WBApiError(
                        "WB вернул неожиданный ответ со списком "
                        "сборочных заданий."
                    )

                for order in orders:
                    if not isinstance(order, dict):
                        continue

                    try:
                        order_id = int(order["id"])
                    except (KeyError, TypeError, ValueError):
                        continue

                    if order_id in target_ids:
                        found[order_id] = {
                            "article": order.get("article"),
                            "crossBorderType": order.get(
                                "crossBorderType",
                                0,
                            ),
                        }

                if set(found) == target_ids:
                    return found

                next_cursor = data.get("next", 0)

                if not next_cursor or not orders:
                    break

                if next_cursor in seen_cursors:
                    raise WBApiError(
                        "WB вернул повторный курсор списка "
                        "сборочных заданий."
                    )

                seen_cursors.add(next_cursor)
                page_cursor = next_cursor

            current_start = window_end

        return found

    def stickers(
        self,
        order_ids: list[int],
        width: int = 58,
        height: int = 40,
    ) -> list[dict]:
        """
        Получает оригинальные стикеры WB.

        Метод:
        POST /api/v3/orders/stickers

        В одном запросе WB допускает не более 100 ID заданий.
        """
        if (width, height) not in ((58, 40), (40, 30)):
            raise ValueError(
                "Допустимы только размеры стикера 58×40 или 40×30 мм."
            )

        if not order_ids:
            return []

        stickers: list[dict] = []

        for offset in range(0, len(order_ids), 100):
            batch = order_ids[offset:offset + 100]

            data = self._request(
                method="POST",
                path="/api/v3/orders/stickers",
                params={
                    "type": "png",
                    "width": width,
                    "height": height,
                },
                json={
                    "orders": batch,
                },
            )

            response_stickers = data.get("stickers")

            if not isinstance(response_stickers, list):
                raise WBApiError(
                    "WB вернул ответ без списка стикеров."
                )

            for sticker in response_stickers:
                if isinstance(sticker, dict):
                    stickers.append(sticker)

        return stickers

    def completed_fbs_demand_by_article(
        self,
        date_from: date,
        date_to: date,
        order_lookback_days: int = 31,
    ) -> dict[str, int]:
        """
        Возвращает фактическое WB FBS-потребление по артикулам.

        Источник спроса — завершённые WB-поставки:

        - `done = true`;
        - `closedAt` входит в указанный период;
        - в расчёт попадают все сборочные задания поставки;
        - потребление группируется по `article`.

        В отличие от Statistics API, метод считает товар, который был
        фактически собран и выбыл из готовой продукции.

        `order_lookback_days` — период поиска данных сборочных заданий.
        Он больше недельного периода, потому что заказ мог быть создан
        за несколько дней до закрытия поставки.
        """
        if date_from > date_to:
            raise ValueError(
                "Дата начала периода не может быть позже даты конца."
            )

        if order_lookback_days < 1:
            raise ValueError(
                "Период поиска заданий должен быть не меньше одного дня."
            )

        supplies = self.list_supplies()

        completed_supply_ids: list[str] = []

        for supply in supplies:
            if not supply_is_done(supply):
                continue

            closed_at = parse_wb_datetime(
                supply.get("closedAt")
            )

            if closed_at is None:
                continue

            closed_date = closed_at.date()

            if (
                closed_date < date_from
                or closed_date > date_to
            ):
                continue

            supply_id = str(supply.get("id") or "").strip()

            if not supply_id:
                raise WBApiError(
                    "WB вернул завершённую поставку без корректного ID."
                )

            completed_supply_ids.append(supply_id)

        if not completed_supply_ids:
            return {}

        # ID сборочного задания -> ID завершённой поставки.
        # Каждое задание должно быть учтено только один раз.
        order_to_supply: dict[int, str] = {}

        for supply_id in completed_supply_ids:
            order_ids = self.supply_order_ids(supply_id)

            for order_id in order_ids:
                previous_supply_id = order_to_supply.get(order_id)

                if (
                    previous_supply_id is not None
                    and previous_supply_id != supply_id
                ):
                    raise WBApiError(
                        "WB вернул одно и то же сборочное задание "
                        "в двух завершённых поставках. "
                        "Недельное потребление не рассчитано, "
                        "чтобы избежать двойного учёта."
                    )

                order_to_supply[order_id] = supply_id

        target_ids = set(order_to_supply)

        if not target_ids:
            return {}

        orders = self.orders_for_ids(
            target_ids=target_ids,
            lookback_days=order_lookback_days,
        )

        missing_order_ids = sorted(
            target_ids - set(orders)
        )

        if missing_order_ids:
            shown = ", ".join(
                str(order_id)
                for order_id in missing_order_ids[:20]
            )

            if len(missing_order_ids) > 20:
                shown += (
                    f" и ещё {len(missing_order_ids) - 20}"
                )

            raise WBApiError(
                "Не удалось найти данные некоторых сборочных заданий "
                "завершённых поставок: "
                f"{shown}. "
                "Попробуйте увеличить период поиска заданий."
            )

        demand_by_article: dict[str, int] = {}

        for order_id in target_ids:
            order = orders[order_id]

            article = str(
                order.get("article") or ""
            ).strip()

            if not article:
                raise WBApiError(
                    "WB не вернул артикул продавца для сборочного "
                    f"задания {order_id}. "
                    "Недельное потребление не рассчитано."
                )

            demand_by_article[article] = (
                demand_by_article.get(article, 0) + 1
            )

        return demand_by_article