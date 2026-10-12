from __future__ import annotations

import math
import re
import time
from datetime import date, datetime, time as datetime_time

import requests


BASE_URL = "https://api.moysklad.ru/api/remap/1.2"

MIN_REQUEST_INTERVAL_SECONDS = 0.25

UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)


class MySkladApiError(RuntimeError):
    """Ошибка безопасного обращения к API МоегоСклада."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status


def _as_number(value: object) -> float:
    """Преобразует количество API МоегоСклада в число."""
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise MySkladApiError(
            "МойСклад вернул некорректное числовое значение остатка."
        ) from error


def _to_piece_count(value: float) -> int:
    """
    Преобразует остаток в число штук.

    Положительные дробные значения округляются вниз, чтобы план
    не показывал больше готовой продукции, чем есть фактически.
    """
    if value >= 0:
        return int(math.floor(value))

    return int(math.ceil(value))


class MySkladClient:
    """
    Read-only клиент JSON API МоегоСклада 1.2.

    Выполняет только GET-запросы:

    - карточка склада;
    - отчёт остатков;
    - статусы заказов и отгрузок;
    - заказы покупателей;
    - отгрузки;
    - товарные позиции заказов и отгрузок.

    Клиент не создаёт, не изменяет, не проводит и не удаляет документы.
    """

    def __init__(
        self,
        token: str,
        timeout: int = 60,
        retries: int = 3,
    ) -> None:
        if not token.strip():
            raise ValueError(
                "Не задан `MYSKLAD_API_TOKEN`."
            )

        self.timeout = timeout
        self.retries = retries
        self._last_request_at = 0.0

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token.strip()}",
                "Accept": "application/json;charset=utf-8",
                "Accept-Encoding": "gzip",
            }
        )

    def _wait_for_rate_limit(self) -> None:
        """Выдерживает безопасный интервал между запросами."""
        elapsed = time.monotonic() - self._last_request_at
        delay = MIN_REQUEST_INTERVAL_SECONDS - elapsed

        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _retry_delay(
        response: requests.Response,
        attempt: int,
    ) -> float:
        """Определяет задержку перед повторным запросом."""
        retry_after = (
            response.headers.get("X-Lognex-Retry-After")
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
        path: str,
        params: dict[str, object] | None = None,
    ) -> dict:
        """
        Выполняет GET-запрос к МоемСкладу с ограниченными повторами.

        Полные тела ответов, токены и заголовки авторизации не выводятся.
        """
        url = f"{BASE_URL}{path}"

        for attempt in range(self.retries + 1):
            self._wait_for_rate_limit()

            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                )
                self._last_request_at = time.monotonic()

            except requests.RequestException:
                if attempt >= self.retries:
                    raise MySkladApiError(
                        "Сетевая ошибка при обращении к API МоегоСклада. "
                        "Проверьте подключение к интернету "
                        "и повторите позже."
                    )

                time.sleep(min(float(2 ** attempt), 8.0))
                continue

            if response.status_code in (401, 403):
                raise MySkladApiError(
                    "МойСклад отказал в доступе. Проверьте токен "
                    "и права чтения заказов, отгрузок и остатков.",
                    status=response.status_code,
                )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.retries:
                    raise MySkladApiError(
                        "МойСклад временно недоступен или ограничил "
                        "частоту запросов. Повторите позже.",
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
                raise MySkladApiError(
                    "API МоегоСклада вернул ошибку "
                    f"HTTP {response.status_code}.",
                    status=response.status_code,
                )

            try:
                payload = response.json()
            except ValueError as error:
                raise MySkladApiError(
                    "МойСклад вернул ответ неизвестного формата."
                ) from error

            if not isinstance(payload, dict):
                raise MySkladApiError(
                    "МойСклад вернул неожиданный формат ответа."
                )

            return payload

        raise MySkladApiError(
            "Не удалось выполнить запрос к API МоегоСклада."
        )

    def _list_rows(
        self,
        path: str,
        params: dict[str, object] | None = None,
    ) -> list[dict]:
        """
        Получает все строки через стандартную пагинацию limit / offset.

        Максимальный размер одной страницы МоегоСклада — 1000 строк.
        """
        result: list[dict] = []
        offset = 0
        limit = 1000

        while True:
            query = dict(params or {})
            query.update(
                {
                    "limit": limit,
                    "offset": offset,
                }
            )

            payload = self._request(
                path=path,
                params=query,
            )

            rows = payload.get("rows")

            if not isinstance(rows, list):
                raise MySkladApiError(
                    "МойСклад вернул ответ без списка `rows`."
                )

            result.extend(
                row
                for row in rows
                if isinstance(row, dict)
            )

            if len(rows) < limit:
                break

            offset += limit

        return result

    @staticmethod
    def _validate_uuid(
        value: str,
        entity_name: str,
    ) -> str:
        """Проверяет UUID сущности, полученный из ответа API."""
        clean_value = value.strip()

        if not UUID_PATTERN.fullmatch(clean_value):
            raise MySkladApiError(
                "МойСклад вернул "
                f"{entity_name} без корректного UUID."
            )

        return clean_value

    @staticmethod
    def _validate_store_id(store_id: str) -> str:
        """Проверяет UUID склада из Streamlit Secrets."""
        clean_store_id = store_id.strip()

        if not UUID_PATTERN.fullmatch(clean_store_id):
            raise ValueError(
                "Некорректный "
                "`MYSKLAD_WB_FBS_FINISHED_STORE_ID`. "
                "Укажите UUID склада МоегоСклада."
            )

        return clean_store_id

    def store_name(
        self,
        store_id: str,
    ) -> str:
        """Возвращает название выбранного склада."""
        clean_store_id = self._validate_store_id(store_id)

        payload = self._request(
            path=f"/entity/store/{clean_store_id}",
        )

        name = str(payload.get("name") or "").strip()

        return name or clean_store_id

    def finished_stock_by_article(
        self,
        store_id: str,
    ) -> dict[str, int]:
        """
        Возвращает физические остатки по артикулам выбранного склада.

        Используется:

        GET /report/stock/all
        """
        clean_store_id = self._validate_store_id(store_id)

        store_href = (
            f"{BASE_URL}/entity/store/{clean_store_id}"
        )

        rows = self._list_rows(
            path="/report/stock/all",
            params={
                "filter": f"store={store_href}",
                "stockMode": "all",
                "groupBy": "variant",
            },
        )

        stock_by_article: dict[str, float] = {}

        for row in rows:
            article = str(
                row.get("article") or ""
            ).strip()

            if not article:
                continue

            if "stock" not in row:
                raise MySkladApiError(
                    "МойСклад вернул строку отчёта без поля `stock`."
                )

            stock_by_article[article] = (
                stock_by_article.get(article, 0.0)
                + _as_number(row["stock"])
            )

        return {
            article: _to_piece_count(quantity)
            for article, quantity in stock_by_article.items()
        }

    def demand_states(self) -> list[dict]:
        """
        Получает статусы отгрузок.

        Endpoint:

        GET /entity/demand/metadata

        Статусы содержатся в поле `states`.
        """
        metadata = self._request(
            path="/entity/demand/metadata",
        )

        states = metadata.get("states")

        if not isinstance(states, list):
            raise MySkladApiError(
                "МойСклад вернул метаданные отгрузок без списка "
                "статусов `states`."
            )

        return [
            state
            for state in states
            if isinstance(state, dict)
        ]

    def customer_order_states(self) -> list[dict]:
        """
        Получает статусы заказов покупателей.

        Endpoint:

        GET /entity/customerorder/metadata

        Статусы содержатся в поле `states`.
        """
        metadata = self._request(
            path="/entity/customerorder/metadata",
        )

        states = metadata.get("states")

        if not isinstance(states, list):
            raise MySkladApiError(
                "МойСклад вернул метаданные заказов покупателей "
                "без списка статусов `states`."
            )

        return [
            state
            for state in states
            if isinstance(state, dict)
        ]

    def demands_by_moment(
        self,
        date_from: date,
        date_to: date,
    ) -> list[dict]:
        """
        Возвращает отгрузки с `moment` в выбранном периоде.

        Используется:

        GET /entity/demand
        """
        if date_from > date_to:
            raise ValueError(
                "Дата начала периода не может быть позже даты окончания."
            )

        moment_from = datetime.combine(
            date_from,
            datetime_time.min,
        ).strftime("%Y-%m-%d %H:%M:%S")

        moment_to = datetime.combine(
            date_to,
            datetime_time.max.replace(microsecond=0),
        ).strftime("%Y-%m-%d %H:%M:%S")

        return self._list_rows(
            path="/entity/demand",
            params={
                "filter": (
                    f"moment>={moment_from};"
                    f"moment<={moment_to}"
                ),
                "expand": "agent",
            },
        )

    def customer_orders_by_moment(
        self,
        date_from: date,
        date_to: date,
    ) -> list[dict]:
        """
        Возвращает заказы покупателей с `moment` в выбранном периоде.

        Используется:

        GET /entity/customerorder

        Запрашивается `expand=agent`, чтобы в фильтре можно было
        безопасно показать контрагента без дополнительного обращения
        к карточке контрагента.
        """
        if date_from > date_to:
            raise ValueError(
                "Дата начала периода не может быть позже даты окончания."
            )

        moment_from = datetime.combine(
            date_from,
            datetime_time.min,
        ).strftime("%Y-%m-%d %H:%M:%S")

        moment_to = datetime.combine(
            date_to,
            datetime_time.max.replace(microsecond=0),
        ).strftime("%Y-%m-%d %H:%M:%S")

        return self._list_rows(
            path="/entity/customerorder",
            params={
                "filter": (
                    f"moment>={moment_from};"
                    f"moment<={moment_to}"
                ),
                "expand": "agent",
            },
        )

    def customer_order_by_id(
        self,
        customer_order_id: str,
    ) -> dict:
        """
        Получает заказ покупателя с актуальными связанными отгрузками.

        Используется:

        GET /entity/customerorder/{id}

        Поле `demands` — источник связанных отгрузок заказа.
        """
        clean_id = self._validate_uuid(
            value=customer_order_id,
            entity_name="заказ покупателя",
        )

        return self._request(
            path=f"/entity/customerorder/{clean_id}",
            params={
                "expand": "agent,demands",
            },
        )

    def demand_by_id(
        self,
        demand_id: str,
    ) -> dict:
        """
        Получает отгрузку для проверки обратной связи с заказом.

        Используется:

        GET /entity/demand/{id}

        Проверяются поля:

        - `customerOrder`;
        - `agent`;
        - `state`.
        """
        clean_id = self._validate_uuid(
            value=demand_id,
            entity_name="отгрузку",
        )

        return self._request(
            path=f"/entity/demand/{clean_id}",
            params={
                "expand": "agent,customerOrder",
            },
        )

    def customer_order_positions(
        self,
        customer_order_id: str,
        expand_assortment: bool = False,
    ) -> list[dict]:
        """
        Получает все позиции заказа покупателя.

        Используется:

        GET /entity/customerorder/{id}/positions

        При `expand_assortment=True` дополнительно запрашивается
        артикул из карточки товара для сверки.
        """
        clean_id = self._validate_uuid(
            value=customer_order_id,
            entity_name="заказ покупателя",
        )

        params: dict[str, object] = {}

        if expand_assortment:
            params["expand"] = "assortment"

        return self._list_rows(
            path=(
                f"/entity/customerorder/{clean_id}/positions"
            ),
            params=params,
        )

    def demand_positions(
        self,
        demand_id: str,
        expand_assortment: bool = False,
    ) -> list[dict]:
        """
        Получает все позиции отгрузки.

        Используется:

        GET /entity/demand/{id}/positions

        При `expand_assortment=True` дополнительно запрашивается
        артикул товара.
        """
        clean_id = self._validate_uuid(
            value=demand_id,
            entity_name="отгрузку",
        )

        params: dict[str, object] = {}

        if expand_assortment:
            params["expand"] = "assortment"

        return self._list_rows(
            path=f"/entity/demand/{clean_id}/positions",
            params=params,
        )