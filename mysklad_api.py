from __future__ import annotations

import math
import re
import time
from typing import Any

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
    """Преобразует числовое значение API МоегоСклада."""
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise MySkladApiError(
            "МойСклад вернул некорректное числовое значение остатка."
        ) from error


def _to_piece_count(value: float) -> int:
    """
    Преобразует остаток в целое число штук.

    Положительная дробная часть округляется вниз: нельзя отправить
    на упаковку больше единиц, чем фактически доступно.
    """
    if value >= 0:
        return int(math.floor(value))

    return int(math.ceil(value))


class MySkladClient:
    """
    Клиент JSON API МоегоСклада 1.2.

    Первый этап использует только GET-запросы:

    - получение карточки склада;
    - получение отчёта «Остатки по складам».

    Резервы намеренно не участвуют в расчёте.
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
        """Выдерживает небольшой интервал между запросами."""
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
        """Выполняет GET-запрос с ограниченными повторами."""
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
                        "Проверьте интернет-подключение и повторите позже."
                    )

                time.sleep(min(float(2 ** attempt), 8.0))
                continue

            if response.status_code in (401, 403):
                raise MySkladApiError(
                    "МойСклад отказал в доступе. Проверьте токен "
                    "и права просмотра складов и остатков.",
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
        Получает все строки стандартной пагинации limit/offset.

        Максимальный размер страницы МоегоСклада — 1000 строк.
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
    def _validate_store_id(store_id: str) -> str:
        """Проверяет формат UUID склада."""
        clean_store_id = store_id.strip()

        if not UUID_PATTERN.fullmatch(clean_store_id):
            raise ValueError(
                "Некорректный "
                "`MYSKLAD_WB_FBS_FINISHED_STORE_ID`. "
                "Укажите UUID склада МоегоСклада."
            )

        return clean_store_id

    @staticmethod
    def _meta_matches_store(
        meta: object,
        store_id: str,
    ) -> bool:
        """Проверяет, относится ли meta-объект к выбранному складу."""
        if not isinstance(meta, dict):
            return False

        if str(meta.get("id") or "").strip() == store_id:
            return True

        href = str(meta.get("href") or "").rstrip("/")

        return href.endswith(f"/{store_id}")

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

        Используется отчёт МоегоСклада:

        GET /report/stock/bystore

        В строке отчёта находится список `stockByStore`.
        Код дополнительно проверяет UUID склада внутри этого списка,
        поэтому остатки других складов не попадают в расчёт.

        Важно:
        - используется только `stock`;
        - `reserve` намеренно игнорируется;
        - `quantity` намеренно игнорируется.
        """
        clean_store_id = self._validate_store_id(store_id)

        store_href = (
            f"{BASE_URL}/entity/store/{clean_store_id}"
        )

        rows = self._list_rows(
            path="/report/stock/bystore",
            params={
                "filter": f"store={store_href}",
                "groupBy": "variant",
                "stockMode": "all",
            },
        )

        stock_by_article: dict[str, float] = {}

        for row in rows:
            article = str(row.get("article") or "").strip()

            # Без артикула невозможно сопоставить остаток с WB.
            if not article:
                continue

            stock_by_store = row.get("stockByStore")

            if not isinstance(stock_by_store, list):
                raise MySkladApiError(
                    "МойСклад вернул строку отчёта без списка "
                    "`stockByStore`."
                )

            selected_store_stock = 0.0
            selected_store_found = False

            for store_stock in stock_by_store:
                if not isinstance(store_stock, dict):
                    continue

                if not self._meta_matches_store(
                    meta=store_stock.get("meta"),
                    store_id=clean_store_id,
                ):
                    continue

                if "stock" not in store_stock:
                    raise MySkladApiError(
                        "МойСклад вернул остаток склада без поля `stock`."
                    )

                selected_store_stock += _as_number(
                    store_stock["stock"]
                )
                selected_store_found = True

            # Ответ был отфильтрован по складу. Если строка всё же не
            # содержит выбранный склад, её нельзя безопасно учитывать.
            if not selected_store_found:
                continue

            stock_by_article[article] = (
                stock_by_article.get(article, 0.0)
                + selected_store_stock
            )

        return {
            article: _to_piece_count(quantity)
            for article, quantity in stock_by_article.items()
        }