from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests


BASE_URL = "https://marketplace-api.wildberries.ru"


class WBApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class WBClient:
    def __init__(self, token: str, timeout: int = 30, retries: int = 3):
        if not token.strip():
            raise ValueError("Не задан API-токен WB.")
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": token.strip(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._last_request = 0.0

    def _request(self, method: str, path: str, **kwargs):
        url = BASE_URL + path

        for attempt in range(self.retries + 1):
            # Учитываем опубликованный WB интервал 200 мс.
            pause = 0.21 - (time.monotonic() - self._last_request)
            if pause > 0:
                time.sleep(pause)

            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
                self._last_request = time.monotonic()
            except requests.RequestException:
                if attempt >= self.retries:
                    raise WBApiError("Сетевая ошибка при обращении к WB API.")
                time.sleep(min(2 ** attempt, 8))
                continue

            if response.status_code in (401, 403):
                raise WBApiError(
                    f"WB отказал в доступе (HTTP {response.status_code}). "
                    "Проверьте тип токена, категорию и права.",
                    response.status_code,
                )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.retries:
                    raise WBApiError(
                        f"Временная ошибка WB API "
                        f"(HTTP {response.status_code}).",
                        response.status_code,
                    )
                retry_after = (
                    response.headers.get("X-Ratelimit-Retry")
                    or response.headers.get("Retry-After")
                )
                try:
                    delay = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(min(max(delay, 1), 60))
                continue

            if not response.ok:
                # Не показываем тело ответа, чтобы не раскрыть лишние данные.
                raise WBApiError(
                    f"WB API вернул ошибку HTTP {response.status_code}.",
                    response.status_code,
                )

            if not response.content:
                return {}

            try:
                return response.json()
            except ValueError:
                raise WBApiError("WB API вернул ответ неизвестного формата.")

        raise WBApiError("Не удалось выполнить запрос к WB API.")

    def list_supplies(self) -> list[dict]:
        result = []
        cursor = 0
        seen = set()

        while True:
            data = self._request(
                "GET",
                "/api/v3/supplies",
                params={"limit": 1000, "next": cursor},
            )
            batch = data.get("supplies")
            if not isinstance(batch, list):
                raise WBApiError("Неожиданный ответ списка поставок.")
            result.extend(batch)

            next_cursor = data.get("next", 0)
            if not next_cursor or not batch:
                break
            if next_cursor in seen:
                raise WBApiError("WB вернул повторный курсор списка поставок.")
            seen.add(next_cursor)
            cursor = next_cursor

        return result

    def supply_order_ids(self, supply_id: str) -> list[int]:
        safe_id = quote(supply_id, safe="")
        data = self._request(
            "GET",
            f"/api/marketplace/v3/supplies/{safe_id}/order-ids",
        )
        ids = data.get("orderIds")
        if not isinstance(ids, list):
            raise WBApiError("Неожиданный ответ со списком заданий.")
        try:
            return [int(value) for value in ids]
        except (TypeError, ValueError):
            raise WBApiError("WB вернул некорректный ID задания.")

    def orders_for_ids(self, target_ids: set[int]) -> dict[int, dict]:
        """
        Ищет задания в скользящем окне 365 дней.
        Каждый запрос охватывает не более 30 дней; страницы обходятся полностью.
        В память сохраняются только нужные ID и поля, необходимые приложению.
        """
        if not target_ids:
            return {}

        end = datetime.now(timezone.utc)
        cursor = end - timedelta(days=365)
        found: dict[int, dict] = {}

        while cursor < end:
            window_end = min(cursor + timedelta(days=30), end)
            date_from = int(cursor.timestamp())
            date_to = int(window_end.timestamp())
            page_cursor = 0
            seen_cursors = set()

            while True:
                data = self._request(
                    "GET",
                    "/api/v3/orders",
                    params={
                        "limit": 1000,
                        "next": page_cursor,
                        "dateFrom": date_from,
                        "dateTo": date_to,
                    },
                )
                batch = data.get("orders")
                if not isinstance(batch, list):
                    raise WBApiError("Неожиданный ответ со списком заказов.")

                for order in batch:
                    try:
                        order_id = int(order["id"])
                    except (KeyError, TypeError, ValueError):
                        continue

                    if order_id in target_ids:
                        # Не сохраняем адреса и прочие персональные данные.
                        found[order_id] = {
                            "article": order.get("article"),
                            "crossBorderType": order.get("crossBorderType", 0),
                        }

                next_cursor = data.get("next", 0)
                if not next_cursor or not batch:
                    break
                if next_cursor in seen_cursors:
                    raise WBApiError("WB вернул повторный курсор списка заданий.")
                seen_cursors.add(next_cursor)
                page_cursor = next_cursor

            # На границе окон допускается повтор записи; ID дедуплицируются.
            cursor = window_end

        return found

    def stickers(
        self,
        order_ids: list[int],
        width: int = 58,
        height: int = 40,
    ) -> list[dict]:
        if (width, height) not in ((58, 40), (40, 30)):
            raise ValueError("Допустимы размеры стикера 58×40 или 40×30.")

        result = []
        for offset in range(0, len(order_ids), 100):
            batch = order_ids[offset:offset + 100]
            data = self._request(
                "POST",
                "/api/v3/orders/stickers",
                params={"type": "png", "width": width, "height": height},
                json={"orders": batch},
            )
            stickers = data.get("stickers")
            if not isinstance(stickers, list):
                raise WBApiError("WB вернул ответ без списка стикеров.")
            result.extend(stickers)

        return result


def is_cross_border(value) -> bool:
    try:
        return int(value or 0) != 0
    except (TypeError, ValueError):
        return bool(value)