from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class FinishedProductsScope:
    """
    Дерево товарных групп готовой продукции.

    Включает корневую товарную группу и все её дочерние группы.
    """

    root_folder_id: str
    root_folder_name: str
    included_folder_ids: frozenset[str]

    @property
    def folder_count(self) -> int:
        """Возвращает количество групп в дереве."""
        return len(self.included_folder_ids)


def _as_number(value: object) -> float:
    """Преобразует значение API в число."""
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise MySkladApiError(
            "МойСклад вернул некорректное числовое значение."
        ) from error


def _to_piece_count(value: float) -> int:
    """Преобразует остаток в целое количество штук."""
    if value >= 0:
        return int(math.floor(value))

    return int(math.ceil(value))


def _entity_id(value: object) -> str:
    """Извлекает UUID из поля id либо meta.href."""
    if not isinstance(value, dict):
        return ""

    direct_id = str(value.get("id") or "").strip()

    if direct_id:
        return direct_id

    meta = value.get("meta")

    if not isinstance(meta, dict):
        return ""

    meta_id = str(meta.get("id") or "").strip()

    if meta_id:
        return meta_id

    href = str(meta.get("href") or "").strip().rstrip("/")

    if not href:
        return ""

    return href.rsplit("/", maxsplit=1)[-1]


class MySkladClient:
    """
    Read-only клиент JSON API МоегоСклада 1.2.

    Использует только GET-запросы. Не создаёт, не редактирует,
    не проводит и не удаляет документы, позиции, статусы и остатки.
    """

    def __init__(
        self,
        token: str,
        timeout: int = 60,
        retries: int = 3,
    ) -> None:
        clean_token = token.strip()

        if not clean_token:
            raise ValueError(
                "Не задан `MYSKLAD_API_TOKEN`."
            )

        self.timeout = timeout
        self.retries = retries
        self._last_request_at = 0.0

        self._assortment_folder_cache: dict[str, str] = {}

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {clean_token}",
                "Accept": "application/json;charset=utf-8",
                "Accept-Encoding": "gzip",
            }
        )

    def _wait_for_rate_limit(self) -> None:
        """Выдерживает интервал между запросами."""
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
        Выполняет безопасный GET-запрос.

        Полный ответ API, Authorization-заголовок и токен не выводятся.
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
                        "Проверьте подключение к интернету и повторите "
                        "операцию позже."
                    )

                time.sleep(min(float(2 ** attempt), 8.0))
                continue

            if response.status_code in (401, 403):
                raise MySkladApiError(
                    "МойСклад отказал в доступе. Проверьте токен "
                    "и права просмотра отгрузок, заказов, товаров "
                    "и товарных групп.",
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
        page_limit: int | None = None,
    ) -> list[dict]:
        """
        Получает все строки с пагинацией `limit` / `offset`.

        При `expand` применяется лимит 100, без `expand` — 1000.
        """
        query_template = dict(params or {})

        if page_limit is None:
            has_expand = bool(
                str(query_template.get("expand") or "").strip()
            )

            page_limit = 100 if has_expand else 1000

        if not 1 <= page_limit <= 1000:
            raise ValueError(
                "Размер страницы МоегоСклада должен быть от 1 до 1000."
            )

        result: list[dict] = []
        offset = 0

        while True:
            query = dict(query_template)
            query.update(
                {
                    "limit": page_limit,
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

            if len(rows) < page_limit:
                break

            offset += page_limit

        return result

    @staticmethod
    def _validate_uuid(
        value: str,
        entity_name: str,
    ) -> str:
        """Проверяет UUID сущности."""
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
        """Возвращает название склада."""
        clean_store_id = self._validate_store_id(store_id)

        payload = self._request(
            path=f"/entity/store/{clean_store_id}",
        )

        return str(payload.get("name") or "").strip() or clean_store_id

    def finished_stock_by_article(
        self,
        store_id: str,
    ) -> dict[str, int]:
        """Возвращает физические остатки по артикулам склада."""
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
            article = str(row.get("article") or "").strip()

            if not article:
                continue

            if "stock" not in row:
                raise MySkladApiError(
                    "МойСклад вернул строку остатков без поля `stock`."
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
        """Возвращает статусы документа «Отгрузка»."""
        metadata = self._request(
            path="/entity/demand/metadata",
        )

        states = metadata.get("states")

        if not isinstance(states, list):
            raise MySkladApiError(
                "МойСклад вернул метаданные отгрузок без `states`."
            )

        return [
            state
            for state in states
            if isinstance(state, dict)
        ]

    def customer_order_states(self) -> list[dict]:
        """Возвращает статусы документа «Заказ покупателя»."""
        metadata = self._request(
            path="/entity/customerorder/metadata",
        )

        states = metadata.get("states")

        if not isinstance(states, list):
            raise MySkladApiError(
                "МойСклад вернул метаданные заказов без `states`."
            )

        return [
            state
            for state in states
            if isinstance(state, dict)
        ]

    def product_folders(self) -> list[dict]:
        """
        Возвращает все товарные группы.

        Endpoint:

        ```text
        GET /entity/productfolder
        ```
        """
        return self._list_rows(
            path="/entity/productfolder",
        )

    def finished_products_scope(
        self,
        root_folder_name: str = "Готовая продукция",
    ) -> FinishedProductsScope:
        """
        Находит группу готовой продукции и все её дочерние группы.

        При отсутствии или дубликате корневой группы расчёт
        останавливается, чтобы не включить расходники ошибочно.
        """
        target_name = " ".join(
            str(root_folder_name or "").split()
        )

        if not target_name:
            raise ValueError(
                "Название группы готовой продукции не может быть пустым."
            )

        folders = self.product_folders()
        folders_by_id: dict[str, dict] = {}
        matched_ids: list[str] = []

        for folder in folders:
            folder_id = _entity_id(folder)

            if not UUID_PATTERN.fullmatch(folder_id):
                raise MySkladApiError(
                    "МойСклад вернул товарную группу без корректного UUID."
                )

            if folder_id in folders_by_id:
                raise MySkladApiError(
                    "МойСклад вернул товарную группу повторно."
                )

            folder_name = " ".join(
                str(folder.get("name") or "").split()
            )

            if not folder_name:
                raise MySkladApiError(
                    "МойСклад вернул товарную группу без названия."
                )

            folders_by_id[folder_id] = folder

            if folder_name.casefold() == target_name.casefold():
                matched_ids.append(folder_id)

        if not matched_ids:
            raise MySkladApiError(
                "Не найдена товарная группа "
                f"«{target_name}». Расчёт остановлен, чтобы не включить "
                "расходники."
            )

        if len(matched_ids) > 1:
            raise MySkladApiError(
                "Найдено несколько товарных групп с названием "
                f"«{target_name}». Оставьте одно однозначное название."
            )

        root_folder_id = matched_ids[0]

        children_by_parent: dict[str, set[str]] = {}

        for folder_id, folder in folders_by_id.items():
            parent_id = _entity_id(
                folder.get("productFolder")
            )

            if parent_id:
                children_by_parent.setdefault(
                    parent_id,
                    set(),
                ).add(folder_id)

        included_ids: set[str] = set()
        queue = [root_folder_id]

        while queue:
            current_id = queue.pop()

            if current_id in included_ids:
                continue

            included_ids.add(current_id)

            queue.extend(
                children_by_parent.get(
                    current_id,
                    set(),
                )
            )

        root_name = str(
            folders_by_id[root_folder_id].get("name") or target_name
        ).strip()

        return FinishedProductsScope(
            root_folder_id=root_folder_id,
            root_folder_name=root_name,
            included_folder_ids=frozenset(included_ids),
        )

    @staticmethod
    def _folder_id_from_product_payload(
        product: object,
    ) -> str:
        """Извлекает UUID productFolder из товара."""
        if not isinstance(product, dict):
            return ""

        return _entity_id(product.get("productFolder"))

    def _product_folder_id(
        self,
        product_id: str,
    ) -> str:
        """Возвращает товарную группу товара."""
        clean_id = self._validate_uuid(
            value=product_id,
            entity_name="товар",
        )

        cache_key = f"product:{clean_id}"

        if cache_key in self._assortment_folder_cache:
            return self._assortment_folder_cache[cache_key]

        product = self._request(
            path=f"/entity/product/{clean_id}",
        )

        folder_id = self._folder_id_from_product_payload(product)

        self._assortment_folder_cache[cache_key] = folder_id

        return folder_id

    def _variant_folder_id(
        self,
        variant_id: str,
    ) -> str:
        """Возвращает товарную группу модификации по её товару."""
        clean_id = self._validate_uuid(
            value=variant_id,
            entity_name="модификацию товара",
        )

        cache_key = f"variant:{clean_id}"

        if cache_key in self._assortment_folder_cache:
            return self._assortment_folder_cache[cache_key]

        variant = self._request(
            path=f"/entity/variant/{clean_id}",
            params={
                "expand": "product",
            },
        )

        product = variant.get("product")

        folder_id = self._folder_id_from_product_payload(product)

        if not folder_id:
            product_id = _entity_id(product)

            if not product_id:
                raise MySkladApiError(
                    "МойСклад вернул модификацию без основного товара."
                )

            folder_id = self._product_folder_id(product_id)

        self._assortment_folder_cache[cache_key] = folder_id

        return folder_id

    def assortment_product_folder_id(
        self,
        assortment: object,
    ) -> str:
        """
        Возвращает UUID товарной группы product или variant.

        При недостатке данных в позиции дополнительно получает товар
        или модификацию через read-only GET.
        """
        if not isinstance(assortment, dict):
            raise MySkladApiError(
                "МойСклад вернул позицию без номенклатуры."
            )

        meta = assortment.get("meta")

        if not isinstance(meta, dict):
            raise MySkladApiError(
                "МойСклад вернул номенклатуру без `meta`."
            )

        assortment_type = str(meta.get("type") or "").strip()
        assortment_id = _entity_id(assortment)

        if not UUID_PATTERN.fullmatch(assortment_id):
            raise MySkladApiError(
                "МойСклад вернул номенклатуру без корректного UUID."
            )

        cache_key = f"{assortment_type}:{assortment_id}"

        if cache_key in self._assortment_folder_cache:
            return self._assortment_folder_cache[cache_key]

        if assortment_type == "product":
            folder_id = self._folder_id_from_product_payload(
                assortment
            )

            if not folder_id:
                folder_id = self._product_folder_id(
                    assortment_id
                )

            self._assortment_folder_cache[cache_key] = folder_id
            return folder_id

        if assortment_type == "variant":
            product = assortment.get("product")

            folder_id = self._folder_id_from_product_payload(
                product
            )

            if not folder_id:
                product_id = _entity_id(product)

                if product_id:
                    folder_id = self._product_folder_id(product_id)
                else:
                    folder_id = self._variant_folder_id(
                        assortment_id
                    )

            self._assortment_folder_cache[cache_key] = folder_id
            return folder_id

        return ""

    def demands_by_moment(
        self,
        date_from: date,
        date_to: date,
    ) -> list[dict]:
        """
        Возвращает отгрузки с `moment` в выбранном диапазоне.

        Endpoint:

        ```text
        GET /entity/demand
        ```
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
            },
        )

    def customer_orders_by_moment(
        self,
        date_from: date,
        date_to: date,
    ) -> list[dict]:
        """Возвращает заказы покупателей по `moment`."""
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
            },
        )

    def customer_order_by_id(
        self,
        customer_order_id: str,
    ) -> dict:
        """
        Возвращает заказ и список связанных отгрузок.

        Endpoint:

        ```text
        GET /entity/customerorder/{id}?expand=demands
        ```
        """
        clean_id = self._validate_uuid(
            value=customer_order_id,
            entity_name="заказ покупателя",
        )

        return self._request(
            path=f"/entity/customerorder/{clean_id}",
            params={
                "expand": "demands",
            },
        )

    def demand_by_id(
        self,
        demand_id: str,
    ) -> dict:
        """
        Возвращает отгрузку, связанный заказ и контрагента отгрузки.

        Endpoint:

        ```text
        GET /entity/demand/{id}?expand=customerOrder,agent
        ```

        `agent` разворачивается намеренно: его название используется
        исключительно для классификации FBO WB и FBO Ozon.
        """
        clean_id = self._validate_uuid(
            value=demand_id,
            entity_name="отгрузку",
        )

        return self._request(
            path=f"/entity/demand/{clean_id}",
            params={
                "expand": "customerOrder,agent",
            },
        )

    def customer_order_positions(
        self,
        customer_order_id: str,
        expand_assortment: bool = False,
    ) -> list[dict]:
        """Возвращает позиции заказа покупателя."""
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
        """Возвращает позиции отгрузки."""
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