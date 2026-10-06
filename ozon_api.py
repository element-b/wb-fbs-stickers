from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests


BASE_URL = "https://api-seller.ozon.ru"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Консервативный последовательный режим для ручного ежедневного запуска.
MIN_REQUEST_INTERVAL_SECONDS = 0.25

# Для первого стабильного внедрения используем небольшой и безопасный размер
# страницы. Пагинация реализована через cursor и has_next.
PAGE_SIZE = 100

# Согласованные статусы: товар уже передан в доставку / покинул склад.
COMPLETED_FBS_STATUSES = (
    "delivering",
    "sent_by_seller",
)


class OzonApiError(RuntimeError):
    """Ошибка безопасного обращения к Ozon Seller API."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class OzonFbsDemandResult:
    """Итог чтения фактического Ozon FBS-потребления."""

    demand_by_offer_id: dict[str, int]
    included_posting_count: int
    skipped_non_matching_offer_ids: list[str]


def _parse_ozon_datetime(
    value: object,
) -> datetime | None:
    """
    Преобразует ISO-даты Ozon в Europe/Moscow.

    Значение без timezone намеренно не интерпретируется:
    это может ошибочно включить posting на границе московского дня.
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
        return None

    return parsed.astimezone(MOSCOW_TZ)


def _to_utc_iso(
    value: datetime,
) -> str:
    """Преобразует datetime в RFC 3339 UTC для Ozon Seller API."""
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _period_boundaries(
    date_from: date,
    date_to: date,
) -> tuple[datetime, datetime]:
    """
    Возвращает границы московского периода.

    Левая граница включительна, правая исключительна.
    """
    period_start = datetime.combine(
        date_from,
        datetime_time.min,
        tzinfo=MOSCOW_TZ,
    )

    period_end_exclusive = datetime.combine(
        date_to + timedelta(days=1),
        datetime_time.min,
        tzinfo=MOSCOW_TZ,
    )

    return period_start, period_end_exclusive


def _safe_ozon_error_hint(
    response: requests.Response,
) -> str:
    """
    Возвращает короткое безопасное пояснение Ozon к HTTP-ошибке.

    Не выводятся:
    - API-ключ;
    - Client-Id;
    - заголовки;
    - полный ответ;
    - потенциально персональные данные из posting.

    Используются только короткие стандартные поля ошибки API.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""

    if not isinstance(payload, dict):
        return ""

    candidates = [
        payload.get("message"),
        payload.get("error"),
        payload.get("details"),
        payload.get("description"),
    ]

    for value in candidates:
        if not isinstance(value, str):
            continue

        text = " ".join(value.split())

        if text:
            # Ограничение длины исключает вывод большого тела ответа.
            return text[:300]

    return ""


class OzonClient:
    """
    Read-only клиент Ozon Seller API.

    Используемый endpoint:

    POST /v4/posting/fbs/list

    Никакие документы, posting, остатки, задания или поставки не создаются,
    не изменяются и не отменяются.
    """

    def __init__(
        self,
        client_id: str,
        api_key: str,
        timeout: int = 30,
        retries: int = 3,
    ) -> None:
        clean_client_id = client_id.strip()
        clean_api_key = api_key.strip()

        if not clean_client_id:
            raise ValueError("Не задан `OZON_CLIENT_ID`.")

        if not clean_api_key:
            raise ValueError("Не задан `OZON_API_KEY`.")

        self.timeout = timeout
        self.retries = retries
        self._last_request_at = 0.0

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Client-Id": clean_client_id,
                "Api-Key": clean_api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def _wait_for_rate_limit(self) -> None:
        """Соблюдает консервативный интервал между запросами."""
        elapsed = time.monotonic() - self._last_request_at
        delay = MIN_REQUEST_INTERVAL_SECONDS - elapsed

        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _retry_delay(
        response: requests.Response,
        attempt: int,
    ) -> float:
        """Возвращает задержку перед безопасной повторной попыткой."""
        retry_after = response.headers.get("Retry-After")

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
        payload: dict,
    ) -> dict:
        """
        Выполняет POST-запрос с ограниченными повторами.

        Повторяются только сетевые ошибки, HTTP 429 и HTTP 5xx.
        """
        url = f"{BASE_URL}{path}"

        for attempt in range(self.retries + 1):
            self._wait_for_rate_limit()

            try:
                response = self.session.post(
                    url=url,
                    json=payload,
                    timeout=self.timeout,
                )

                self._last_request_at = time.monotonic()

            except requests.RequestException:
                if attempt >= self.retries:
                    raise OzonApiError(
                        "Сетевая ошибка при обращении к Ozon Seller API. "
                        "Проверьте интернет-соединение и повторите позже."
                    )

                time.sleep(min(float(2 ** attempt), 8.0))
                continue

            if response.status_code == 401:
                raise OzonApiError(
                    "Ozon отклонил авторизацию. Проверьте "
                    "`OZON_CLIENT_ID` и `OZON_API_KEY` в Secrets.",
                    status=401,
                )

            if response.status_code == 403:
                raise OzonApiError(
                    "Ozon отказал в доступе. Проверьте права "
                    "API-ключа на чтение FBS-отправлений.",
                    status=403,
                )

            if response.status_code == 429:
                if attempt >= self.retries:
                    raise OzonApiError(
                        "Ozon временно ограничил частоту запросов. "
                        "Повторите обновление плана позже.",
                        status=429,
                    )

                time.sleep(
                    self._retry_delay(
                        response=response,
                        attempt=attempt,
                    )
                )
                continue

            if response.status_code >= 500:
                if attempt >= self.retries:
                    raise OzonApiError(
                        "Ozon Seller API временно недоступен. "
                        "Повторите обновление плана позже.",
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
                hint = _safe_ozon_error_hint(response)

                message = (
                    "Ozon Seller API вернул ошибку "
                    f"HTTP {response.status_code}."
                )

                if hint:
                    message += f" Пояснение Ozon: {hint}"

                raise OzonApiError(
                    message,
                    status=response.status_code,
                )

            try:
                data = response.json()
            except ValueError as error:
                raise OzonApiError(
                    "Ozon Seller API вернул ответ неизвестного формата."
                ) from error

            if not isinstance(data, dict):
                raise OzonApiError(
                    "Ozon Seller API вернул неожиданный формат ответа."
                )

            return data

        raise OzonApiError(
            "Не удалось выполнить запрос к Ozon Seller API."
        )

    @staticmethod
    def _as_positive_quantity(
        value: object,
        posting_number: str,
        offer_id: str,
    ) -> int:
        """Проверяет, что quantity — положительное целое число."""
        if isinstance(value, bool):
            raise OzonApiError(
                "Ozon вернул некорректное количество товара "
                f"в отправлении `{posting_number}`."
            )

        if isinstance(value, float) and not value.is_integer():
            raise OzonApiError(
                "Ozon вернул дробное количество товара "
                f"в отправлении `{posting_number}` "
                f"для offer_id `{offer_id}`."
            )

        try:
            quantity = int(value)
        except (TypeError, ValueError) as error:
            raise OzonApiError(
                "Ozon вернул некорректное количество товара "
                f"в отправлении `{posting_number}` "
                f"для offer_id `{offer_id}`."
            ) from error

        if quantity <= 0:
            raise OzonApiError(
                "Ozon вернул неположительное количество товара "
                f"в отправлении `{posting_number}` "
                f"для offer_id `{offer_id}`."
            )

        return quantity

    @staticmethod
    def _posting_is_test(
        posting: dict,
    ) -> bool:
        """Исключает тестовые posting, если API вернул технический флаг."""
        return bool(
            posting.get("is_test")
            or posting.get("is_test_order")
        )

    @staticmethod
    def _posting_cancelled_after_ship(
        posting: dict,
    ) -> bool:
        """Исключает отправления, отменённые после отгрузки."""
        cancellation = posting.get("cancellation")

        if not isinstance(cancellation, dict):
            return False

        return bool(
            cancellation.get("cancelled_after_ship")
        )

    @staticmethod
    def _posting_fingerprint(
        posting: dict,
    ) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        """
        Возвращает безопасный отпечаток posting для проверки дублей.

        Если один posting_number вернулся дважды с различными данными,
        расчёт останавливается, а не выбирает версию произвольно.
        """
        status = str(posting.get("status") or "").strip()

        delivering_date = str(
            posting.get("delivering_date") or ""
        ).strip()

        products = posting.get("products")

        if not isinstance(products, list):
            return (
                status,
                delivering_date,
                (("<invalid-products>", ""),),
            )

        normalized_products: list[tuple[str, str]] = []

        for product in products:
            if not isinstance(product, dict):
                normalized_products.append(
                    ("<invalid-product>", "")
                )
                continue

            normalized_products.append(
                (
                    str(product.get("offer_id") or "").strip(),
                    str(product.get("quantity") or "").strip(),
                )
            )

        return (
            status,
            delivering_date,
            tuple(sorted(normalized_products)),
        )

    def _list_postings_page(
        self,
        cursor: str,
        since: datetime,
        to: datetime,
    ) -> tuple[list[dict], bool, str]:
        """
        Получает одну страницу FBS-posting.

        Для стабильности используется минимальное тело v4:

        - `cursor`;
        - `filter.since`;
        - `filter.to`;
        - `filter.statuses`;
        - `limit`;
        - `sort_dir`.

        Необязательные поля `with`, `translit` и другие поля намеренно
        не отправляются: они не нужны для расчёта и не должны быть причиной
        ошибки валидации запроса.
        """
        payload = {
            "cursor": cursor,
            "filter": {
                "since": _to_utc_iso(since),
                "to": _to_utc_iso(to),
                "statuses": list(COMPLETED_FBS_STATUSES),
            },
            "limit": PAGE_SIZE,
            "sort_dir": "asc",
        }

        response = self._request(
            path="/v4/posting/fbs/list",
            payload=payload,
        )

        postings = response.get("postings")

        if not isinstance(postings, list):
            raise OzonApiError(
                "Ozon вернул ответ без списка `postings`."
            )

        has_next = response.get("has_next")

        if not isinstance(has_next, bool):
            raise OzonApiError(
                "Ozon вернул некорректный признак пагинации `has_next`."
            )

        next_cursor = response.get("cursor")

        if next_cursor is None:
            next_cursor = ""

        if not isinstance(next_cursor, str):
            raise OzonApiError(
                "Ozon вернул некорректный cursor пагинации."
            )

        return (
            [
                posting
                for posting in postings
                if isinstance(posting, dict)
            ],
            has_next,
            next_cursor,
        )

    def _list_postings(
        self,
        since: datetime,
        to: datetime,
    ) -> list[dict]:
        """Получает все страницы Ozon FBS-posting через cursor-пагинацию."""
        all_postings: list[dict] = []

        cursor = ""
        seen_cursors: set[str] = set()

        while True:
            postings, has_next, next_cursor = (
                self._list_postings_page(
                    cursor=cursor,
                    since=since,
                    to=to,
                )
            )

            all_postings.extend(postings)

            if not has_next:
                return all_postings

            if not next_cursor:
                raise OzonApiError(
                    "Ozon сообщил о следующей странице, но не вернул "
                    "cursor. План не обновлён, чтобы исключить "
                    "неполный расчёт."
                )

            if next_cursor in seen_cursors:
                raise OzonApiError(
                    "Ozon вернул повторяющийся cursor. План не обновлён, "
                    "чтобы исключить неполный расчёт."
                )

            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def completed_fbs_demand_by_offer_id(
        self,
        date_from: date,
        date_to: date,
        offer_id_prefix: str = "NAKL_",
    ) -> OzonFbsDemandResult:
        """
        Возвращает фактическое Ozon FBS-потребление по offer_id.

        Включаются только posting:

        - status: `delivering` или `sent_by_seller`;
        - `delivering_date` в нужном московском периоде;
        - не тестовые;
        - не отменённые после отгрузки;
        - с offer_id, начинающимся с точного регистрозависимого NAKL_.

        Период запроса равен периоду производственного анализа: 7 полностью
        завершённых московских дней. Для текущего процесса это достаточно,
        так как максимальная допустимая задержка FBS составляет 1–2 дня.

        Финальная бизнес-проверка всё равно выполняется по delivering_date,
        а не по дате создания posting.
        """
        if date_from > date_to:
            raise ValueError(
                "Дата начала периода Ozon не может быть позже даты конца."
            )

        prefix = offer_id_prefix.strip()

        if not prefix:
            raise ValueError(
                "Префикс Ozon offer_id не может быть пустым."
            )

        target_start, target_end_exclusive = _period_boundaries(
            date_from=date_from,
            date_to=date_to,
        )

        postings = self._list_postings(
            since=target_start,
            to=target_end_exclusive,
        )

        unique_postings: dict[str, dict] = {}
        fingerprints: dict[
            str,
            tuple[str, str, tuple[tuple[str, str], ...]],
        ] = {}

        for posting in postings:
            posting_number = str(
                posting.get("posting_number") or ""
            ).strip()

            if not posting_number:
                raise OzonApiError(
                    "Ozon вернул FBS-отправление без `posting_number`. "
                    "План не обновлён, чтобы исключить двойной учёт."
                )

            fingerprint = self._posting_fingerprint(posting)

            previous_fingerprint = fingerprints.get(posting_number)

            if previous_fingerprint is not None:
                if previous_fingerprint != fingerprint:
                    raise OzonApiError(
                        "Ozon вернул один `posting_number` с разными "
                        "данными. План не обновлён, чтобы исключить "
                        "двойной или недостоверный учёт."
                    )

                continue

            unique_postings[posting_number] = posting
            fingerprints[posting_number] = fingerprint

        demand_by_offer_id: dict[str, int] = {}
        skipped_non_matching_offer_ids: set[str] = set()
        included_posting_count = 0

        for posting_number, posting in unique_postings.items():
            status = str(posting.get("status") or "").strip()

            if status not in COMPLETED_FBS_STATUSES:
                continue

            if self._posting_is_test(posting):
                continue

            if self._posting_cancelled_after_ship(posting):
                continue

            delivering_at = _parse_ozon_datetime(
                posting.get("delivering_date")
            )

            if delivering_at is None:
                raise OzonApiError(
                    "Ozon вернул FBS-отправление в статусе передачи "
                    "в доставку без корректного `delivering_date`: "
                    f"`{posting_number}`. План не обновлён, чтобы "
                    "не потерять фактическое потребление."
                )

            if not (
                target_start
                <= delivering_at
                < target_end_exclusive
            ):
                continue

            products = posting.get("products")

            if not isinstance(products, list) or not products:
                raise OzonApiError(
                    "Ozon вернул FBS-отправление без списка товаров: "
                    f"`{posting_number}`. План не обновлён, чтобы "
                    "не потерять фактическое потребление."
                )

            included_posting_count += 1

            for product in products:
                if not isinstance(product, dict):
                    raise OzonApiError(
                        "Ozon вернул некорректный товар в FBS-отправлении "
                        f"`{posting_number}`."
                    )

                offer_id = str(
                    product.get("offer_id") or ""
                ).strip()

                if not offer_id:
                    raise OzonApiError(
                        "Ozon вернул товар без `offer_id` "
                        f"в FBS-отправлении `{posting_number}`. "
                        "План не обновлён, чтобы не сформировать "
                        "потенциально неполную очередь."
                    )

                quantity = self._as_positive_quantity(
                    value=product.get("quantity"),
                    posting_number=posting_number,
                    offer_id=offer_id,
                )

                # Точное регистрозависимое сравнение.
                # Изменены только внешние пробелы через strip().
                if not offer_id.startswith(prefix):
                    skipped_non_matching_offer_ids.add(offer_id)
                    continue

                demand_by_offer_id[offer_id] = (
                    demand_by_offer_id.get(offer_id, 0)
                    + quantity
                )

        return OzonFbsDemandResult(
            demand_by_offer_id=demand_by_offer_id,
            included_posting_count=included_posting_count,
            skipped_non_matching_offer_ids=sorted(
                skipped_non_matching_offer_ids,
                key=lambda value: (value.casefold(), value),
            ),
        )