from __future__ import annotations

import base64
import binascii
import time
from dataclasses import dataclass
from datetime import (
    date,
    datetime,
    time as datetime_time,
    timedelta,
    timezone,
)
from zoneinfo import ZoneInfo

import requests


BASE_URL = "https://api-seller.ozon.ru"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

MIN_REQUEST_INTERVAL_SECONDS = 0.25

PAGE_SIZE = 100

# Статусы, используемые существующей вкладкой краткосрочного плана.
COMPLETED_FBS_STATUSES = (
    "delivering",
    "sent_by_seller",
)

# Posting может быть создан до целевого периода передачи в доставку.
DEFAULT_QUERY_LOOKBACK_DAYS = 31

# Прямой endpoint печати этикеток Ozon допускает не более 20 отправлений.
PACKAGE_LABEL_BATCH_SIZE = 20

# Рабочий статус для печати этикеток после ручного нажатия «Собрать».
AWAITING_DELIVER_STATUS = "awaiting_deliver"


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
    """Результат чтения фактического Ozon FBS-потребления."""

    demand_by_offer_id: dict[str, int]
    included_posting_count: int
    skipped_non_matching_offer_ids: list[str]


@dataclass(frozen=True)
class OzonFbsStickerPosting:
    """
    Безопасные данные FBS-отправления для вкладки печати этикеток.

    Не содержит данных покупателей, адресов, телефонов, финансовой
    информации и полного ответа API.
    """

    posting_number: str
    in_process_at: datetime
    offer_quantities: dict[str, int]

    @property
    def total_quantity(self) -> int:
        """Возвращает сумму товаров NAKL_ в отправлении."""
        return sum(self.offer_quantities.values())

    @property
    def offer_ids(self) -> list[str]:
        """Возвращает отсортированный список артикулов NAKL_."""
        return sorted(
            self.offer_quantities,
            key=lambda value: (value.casefold(), value),
        )


def _parse_ozon_datetime(
    value: object,
) -> datetime | None:
    """
    Преобразует ISO-дату Ozon в Europe/Moscow.

    Значение без timezone не трактуется автоматически как UTC либо
    Москва: иначе отправление может попасть в неверный календарный день.
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

    Левая граница включительна, правая — исключительна.
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
    Возвращает короткое безопасное пояснение ошибки Ozon.

    Не выводятся ключи API, заголовки, полный JSON, адреса и данные
    покупателей. Используются только стандартные текстовые поля ошибки.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""

    if not isinstance(payload, dict):
        return ""

    for field_name in (
        "message",
        "error",
        "details",
        "description",
    ):
        value = payload.get(field_name)

        if not isinstance(value, str):
            continue

        text = " ".join(value.split())

        if text:
            return text[:350]

    return ""


class OzonClient:
    """
    Read-only клиент Ozon Seller API.

    Использует только запросы чтения и получения готовых этикеток:

    - POST /v4/posting/fbs/list;
    - POST /v2/posting/fbs/package-label.

    Клиент не подтверждает сборку, не меняет статусы, не отменяет
    отправления, не создаёт поставки и не редактирует документы.
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
        """Определяет задержку перед повторной попыткой."""
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

    @staticmethod
    def _as_positive_quantity(
        value: object,
        posting_number: str,
        offer_id: str,
    ) -> int:
        """Проверяет положительное целое количество товара."""
        if isinstance(value, bool):
            raise OzonApiError(
                "Ozon вернул некорректное количество товара "
                f"в FBS-отправлении `{posting_number}` "
                f"для offer_id `{offer_id}`."
            )

        if isinstance(value, float) and not value.is_integer():
            raise OzonApiError(
                "Ozon вернул дробное количество товара "
                f"в FBS-отправлении `{posting_number}` "
                f"для offer_id `{offer_id}`."
            )

        try:
            quantity = int(value)
        except (TypeError, ValueError) as error:
            raise OzonApiError(
                "Ozon вернул некорректное количество товара "
                f"в FBS-отправлении `{posting_number}` "
                f"для offer_id `{offer_id}`."
            ) from error

        if quantity <= 0:
            raise OzonApiError(
                "Ozon вернул неположительное количество товара "
                f"в FBS-отправлении `{posting_number}` "
                f"для offer_id `{offer_id}`."
            )

        return quantity

    @staticmethod
    def _posting_is_test(
        posting: dict,
    ) -> bool:
        """Исключает тестовые отправления при наличии флага."""
        return bool(
            posting.get("is_test")
            or posting.get("is_test_order")
        )

    @staticmethod
    def _posting_cancelled_after_ship(
        posting: dict,
    ) -> bool:
        """Определяет отмену отправления после отгрузки."""
        cancellation = posting.get("cancellation")

        if not isinstance(cancellation, dict):
            return False

        return bool(
            cancellation.get("cancelled_after_ship")
        )

    @staticmethod
    def _posting_fingerprint(
        posting: dict,
    ) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
        """
        Формирует безопасный отпечаток отправления для проверки дублей.

        Если один `posting_number` приходит с разными данными, расчёт
        останавливается, чтобы не выбрать произвольную версию.
        """
        status = str(posting.get("status") or "").strip()

        delivering_date = str(
            posting.get("delivering_date") or ""
        ).strip()

        in_process_at = str(
            posting.get("in_process_at") or ""
        ).strip()

        products = posting.get("products")

        if not isinstance(products, list):
            return (
                status,
                delivering_date,
                in_process_at,
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
            in_process_at,
            tuple(sorted(normalized_products)),
        )

    def _request(
        self,
        path: str,
        payload: dict,
    ) -> dict:
        """
        Выполняет POST-запрос, ожидающий JSON.

        Повторы выполняются только для сетевых ошибок, HTTP 429 и HTTP 5xx.
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
                    "Ozon отказал в доступе. Проверьте права API-ключа "
                    "на чтение FBS-отправлений и этикеток.",
                    status=403,
                )

            if response.status_code == 429:
                if attempt >= self.retries:
                    raise OzonApiError(
                        "Ozon временно ограничил частоту запросов. "
                        "Повторите операцию позже.",
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

    def _request_pdf(
        self,
        path: str,
        payload: dict,
    ) -> bytes:
        """
        Получает PDF-этикетки Ozon.

        Метод `POST /v2/posting/fbs/package-label` в разных версиях
        контракта может вернуть:

        - PDF непосредственно в теле ответа;
        - JSON с base64-строкой в поле `content`.

        Поддерживаются оба документируемых варианта. Полный ответ Ozon
        и base64-содержимое не выводятся в интерфейс или логи.
        """
        url = f"{BASE_URL}{path}"

        for attempt in range(self.retries + 1):
            self._wait_for_rate_limit()

            try:
                response = self.session.post(
                    url=url,
                    json=payload,
                    timeout=self.timeout,
                    headers={
                        "Accept": "application/pdf, application/json",
                    },
                )
                self._last_request_at = time.monotonic()

            except requests.RequestException:
                if attempt >= self.retries:
                    raise OzonApiError(
                        "Сетевая ошибка при получении QR-этикеток Ozon. "
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
                    "Ozon отказал в доступе к FBS-этикеткам. "
                    "Проверьте права API-ключа.",
                    status=403,
                )

            if response.status_code == 429:
                if attempt >= self.retries:
                    raise OzonApiError(
                        "Ozon временно ограничил частоту запросов "
                        "этикеток. Повторите позже.",
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
                        "Ozon Seller API временно недоступен при "
                        "получении этикеток. Повторите позже.",
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
                    "Ozon не подготовил QR-этикетки: "
                    f"HTTP {response.status_code}."
                )

                if hint:
                    message += f" Пояснение Ozon: {hint}"

                raise OzonApiError(
                    message,
                    status=response.status_code,
                )

            raw_pdf = bytes(response.content or b"")

            if raw_pdf.startswith(b"%PDF-"):
                return raw_pdf

            try:
                payload_data = response.json()
            except ValueError as error:
                raise OzonApiError(
                    "Ozon вернул этикетки в неизвестном формате."
                ) from error

            if not isinstance(payload_data, dict):
                raise OzonApiError(
                    "Ozon вернул этикетки в неожиданном формате."
                )

            encoded_pdf = payload_data.get("content")

            if not isinstance(encoded_pdf, str) or not encoded_pdf:
                raise OzonApiError(
                    "Ozon не вернул PDF-содержимое QR-этикеток. "
                    "Возможно, этикетки ещё не готовы. Повторите "
                    "запрос через 45–60 секунд."
                )

            try:
                decoded_pdf = base64.b64decode(
                    encoded_pdf,
                    validate=True,
                )
            except (ValueError, binascii.Error) as error:
                raise OzonApiError(
                    "Ozon вернул повреждённое содержимое "
                    "QR-этикеток."
                ) from error

            if not decoded_pdf.startswith(b"%PDF-"):
                raise OzonApiError(
                    "Ozon вернул этикетки не в формате PDF."
                )

            return decoded_pdf

        raise OzonApiError(
            "Не удалось получить QR-этикетки Ozon."
        )

    def _list_postings_page(
        self,
        cursor: str,
        since: datetime,
        to: datetime,
    ) -> tuple[list[dict], bool, str]:
        """Получает одну страницу FBS-отправлений Ozon."""
        payload = {
            "cursor": cursor,
            "filter": {
                "since": _to_utc_iso(since),
                "to": _to_utc_iso(to),
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
        """Получает все страницы FBS-отправлений через cursor."""
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
                    "cursor. Расчёт остановлен, чтобы не использовать "
                    "неполный список отправлений."
                )

            if next_cursor in seen_cursors:
                raise OzonApiError(
                    "Ozon вернул повторяющийся cursor. Расчёт остановлен, "
                    "чтобы не использовать неполный список отправлений."
                )

            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def completed_fbs_demand_by_offer_id(
        self,
        date_from: date,
        date_to: date,
        offer_id_prefix: str = "NAKL_",
        query_lookback_days: int = (
            DEFAULT_QUERY_LOOKBACK_DAYS
        ),
    ) -> OzonFbsDemandResult:
        """
        Возвращает фактическое Ozon FBS-потребление по offer_id.

        Используется существующей вкладкой краткосрочного плана.
        """
        if date_from > date_to:
            raise ValueError(
                "Дата начала периода Ozon не может быть позже даты конца."
            )

        if query_lookback_days < 1:
            raise ValueError(
                "Период поиска Ozon FBS-posting должен быть "
                "не меньше одного дня."
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

        query_start = target_start - timedelta(
            days=query_lookback_days
        )

        postings = self._list_postings(
            since=query_start,
            to=target_end_exclusive,
        )

        unique_postings: dict[str, dict] = {}
        fingerprints: dict[
            str,
            tuple[str, str, str, tuple[tuple[str, str], ...]],
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
                    f"`{posting_number}`."
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
                    f"`{posting_number}`."
                )

            posting_has_matching_product = False

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
                        f"в FBS-отправлении `{posting_number}`."
                    )

                if not offer_id.startswith(prefix):
                    skipped_non_matching_offer_ids.add(offer_id)
                    continue

                quantity = self._as_positive_quantity(
                    value=product.get("quantity"),
                    posting_number=posting_number,
                    offer_id=offer_id,
                )

                demand_by_offer_id[offer_id] = (
                    demand_by_offer_id.get(offer_id, 0)
                    + quantity
                )

                posting_has_matching_product = True

            if posting_has_matching_product:
                included_posting_count += 1

        return OzonFbsDemandResult(
            demand_by_offer_id=demand_by_offer_id,
            included_posting_count=included_posting_count,
            skipped_non_matching_offer_ids=sorted(
                skipped_non_matching_offer_ids,
                key=lambda value: (value.casefold(), value),
            ),
        )

    def awaiting_deliver_nakl_postings(
        self,
        date_from: date,
        date_to: date,
        offer_id_prefix: str = "NAKL_",
        query_lookback_days: int = (
            DEFAULT_QUERY_LOOKBACK_DAYS
        ),
    ) -> list[OzonFbsStickerPosting]:
        """
        Возвращает FBS-отправления, готовые к печати QR-этикеток.

        Включаются только отправления:

        - со статусом `awaiting_deliver`;
        - не тестовые;
        - не отменённые после отгрузки;
        - с корректным `in_process_at` в выбранном московском периоде;
        - содержащие хотя бы один товар, у которого offer_id начинается
          с указанного префикса, обычно `NAKL_`.

        Период фильтруется локально строго по `in_process_at`.
        Запрос к Ozon расширяется назад на 31 день, потому что API списка
        использует собственную фильтрацию дат, а отправление может быть
        создано до даты фактического поступления в обработку.

        Метод не меняет статус отправления и не подтверждает сборку.
        """
        if date_from > date_to:
            raise ValueError(
                "Дата начала периода Ozon не может быть позже даты конца."
            )

        if query_lookback_days < 1:
            raise ValueError(
                "Период поиска FBS-отправлений Ozon должен быть "
                "не меньше одного дня."
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

        query_start = target_start - timedelta(
            days=query_lookback_days
        )

        postings = self._list_postings(
            since=query_start,
            to=target_end_exclusive,
        )

        unique_postings: dict[str, dict] = {}
        fingerprints: dict[
            str,
            tuple[str, str, str, tuple[tuple[str, str], ...]],
        ] = {}

        for posting in postings:
            posting_number = str(
                posting.get("posting_number") or ""
            ).strip()

            if not posting_number:
                raise OzonApiError(
                    "Ozon вернул FBS-отправление без `posting_number`. "
                    "Список этикеток не сформирован, чтобы исключить "
                    "двойной учёт."
                )

            fingerprint = self._posting_fingerprint(posting)

            previous_fingerprint = fingerprints.get(posting_number)

            if previous_fingerprint is not None:
                if previous_fingerprint != fingerprint:
                    raise OzonApiError(
                        "Ozon вернул один `posting_number` с разными "
                        "данными. Список этикеток не сформирован, "
                        "чтобы исключить неоднозначный выбор."
                    )

                continue

            unique_postings[posting_number] = posting
            fingerprints[posting_number] = fingerprint

        result: list[OzonFbsStickerPosting] = []

        for posting_number, posting in unique_postings.items():
            status = str(posting.get("status") or "").strip()

            if status != AWAITING_DELIVER_STATUS:
                continue

            if self._posting_is_test(posting):
                continue

            if self._posting_cancelled_after_ship(posting):
                continue

            in_process_at = _parse_ozon_datetime(
                posting.get("in_process_at")
            )

            if in_process_at is None:
                raise OzonApiError(
                    "Ozon вернул отправление «Ожидает отгрузки» "
                    "без корректного `in_process_at`. "
                    "Список не сформирован, чтобы не включить "
                    "отправление в неверную дату."
                )

            if not (
                target_start
                <= in_process_at
                < target_end_exclusive
            ):
                continue

            products = posting.get("products")

            if not isinstance(products, list) or not products:
                raise OzonApiError(
                    "Ozon вернул FBS-отправление без списка товаров. "
                    "Список этикеток не сформирован."
                )

            offer_quantities: dict[str, int] = {}

            for product in products:
                if not isinstance(product, dict):
                    raise OzonApiError(
                        "Ozon вернул некорректный товар в "
                        "FBS-отправлении."
                    )

                offer_id = str(
                    product.get("offer_id") or ""
                ).strip()

                if not offer_id:
                    raise OzonApiError(
                        "Ozon вернул товар без `offer_id` "
                        "в FBS-отправлении."
                    )

                if not offer_id.startswith(prefix):
                    continue

                quantity = self._as_positive_quantity(
                    value=product.get("quantity"),
                    posting_number=posting_number,
                    offer_id=offer_id,
                )

                offer_quantities[offer_id] = (
                    offer_quantities.get(offer_id, 0)
                    + quantity
                )

            if not offer_quantities:
                continue

            result.append(
                OzonFbsStickerPosting(
                    posting_number=posting_number,
                    in_process_at=in_process_at,
                    offer_quantities=offer_quantities,
                )
            )

        return sorted(
            result,
            key=lambda posting: (
                -posting.in_process_at.timestamp(),
                posting.posting_number,
            ),
        )

    def package_labels_pdf(
        self,
        posting_numbers: list[str],
    ) -> list[bytes]:
        """
        Получает оригинальные PDF-этикетки Ozon.

        Endpoint:

        POST /v2/posting/fbs/package-label

        Ozon разрешает не более 20 номеров отправлений в одном запросе.
        Возвращается список PDF-частей в порядке запросов. Объединение
        выполняется отдельным модулем без изменения оригинальных страниц.

        Если этикетка ещё не готова, Ozon возвращает ошибку. Частичный
        результат не возвращается: пользователь не получит неполный PDF.
        """
        normalized_numbers: list[str] = []
        seen_numbers: set[str] = set()

        for value in posting_numbers:
            posting_number = str(value or "").strip()

            if not posting_number:
                raise ValueError(
                    "Номер FBS-отправления Ozon не может быть пустым."
                )

            if posting_number in seen_numbers:
                continue

            seen_numbers.add(posting_number)
            normalized_numbers.append(posting_number)

        if not normalized_numbers:
            raise ValueError(
                "Выберите хотя бы одно FBS-отправление Ozon."
            )

        pdf_parts: list[bytes] = []

        for offset in range(
            0,
            len(normalized_numbers),
            PACKAGE_LABEL_BATCH_SIZE,
        ):
            batch = normalized_numbers[
                offset:offset + PACKAGE_LABEL_BATCH_SIZE
            ]

            pdf_parts.append(
                self._request_pdf(
                    path="/v2/posting/fbs/package-label",
                    payload={
                        "posting_number": batch,
                    },
                )
            )

        return pdf_parts