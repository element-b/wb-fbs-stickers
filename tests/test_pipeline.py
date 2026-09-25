from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image

from pipeline import DataCheckError, collect_and_group


def make_png() -> bytes:
    """Создаёт тестовый PNG размером 580×400 пикселей."""
    image = Image.new(
        "RGB",
        (580, 400),
        "white",
    )

    output = BytesIO()
    image.save(output, format="PNG")

    return output.getvalue()


class FakeClient:
    """Тестовая замена WBClient без реальных запросов в WB."""

    def __init__(self, stickers: list[dict] | None = None) -> None:
        self._stickers = stickers

    def supply_order_ids(self, supply_id: str) -> list[int]:
        data = {
            "WB-GI-1": [101, 102],
            "WB-GI-2": [103],
        }

        return data[supply_id]

    def orders_for_ids(
        self,
        target_ids: set[int],
        lookback_days: int,
    ) -> dict[int, dict]:
        all_orders = {
            101: {
                "article": "ART-A",
                "crossBorderType": 0,
            },
            102: {
                "article": "ART-B",
                "crossBorderType": 0,
            },
            103: {
                "article": "ART-A",
                "crossBorderType": 0,
            },
        }

        return {
            order_id: all_orders[order_id]
            for order_id in target_ids
        }

    def stickers(
        self,
        order_ids: list[int],
        width: int,
        height: int,
    ) -> list[dict]:
        if self._stickers is not None:
            return self._stickers

        png = make_png()

        # Возвращаем стикеры в обратном порядке.
        # Pipeline должен сопоставить их по orderId, а не по позиции.
        return [
            {
                "orderId": order_id,
                "file": base64.b64encode(png).decode("ascii"),
            }
            for order_id in reversed(order_ids)
        ]


def test_groups_exact_articles_and_keeps_stable_order() -> None:
    """Одинаковые артикулы должны объединяться без смешивания групп."""
    result = collect_and_group(
        client=FakeClient(),
        selected_supplies=[
            {"id": "WB-GI-1"},
            {"id": "WB-GI-2"},
        ],
        lookback_days=31,
        sticker_width=58,
        sticker_height=40,
    )

    assert result["supply_count"] == 2
    assert result["order_count"] == 3
    assert result["article_count"] == 2

    assert result["groups"][0]["article"] == "ART-A"
    assert result["groups"][0]["order_ids"] == [101, 103]
    assert result["groups"][0]["supply_count"] == 2
    assert len(result["groups"][0]["stickers"]) == 2

    assert result["groups"][1]["article"] == "ART-B"
    assert result["groups"][1]["order_ids"] == [102]
    assert result["groups"][1]["supply_count"] == 1
    assert len(result["groups"][1]["stickers"]) == 1


def test_blocks_pdf_when_wb_returns_not_all_stickers() -> None:
    """Неполный набор стикеров должен блокировать результат."""
    png = make_png()

    client = FakeClient(
        stickers=[
            {
                "orderId": 101,
                "file": base64.b64encode(png).decode("ascii"),
            }
        ]
    )

    with pytest.raises(
        DataCheckError,
        match="WB не вернул стикеры",
    ):
        collect_and_group(
            client=client,
            selected_supplies=[
                {"id": "WB-GI-1"},
                {"id": "WB-GI-2"},
            ],
            lookback_days=31,
            sticker_width=58,
            sticker_height=40,
        )


def test_blocks_cross_border_orders() -> None:
    """Трансграничные задания не должны попадать в обычный PDF."""

    class CrossBorderClient(FakeClient):
        def orders_for_ids(
            self,
            target_ids: set[int],
            lookback_days: int,
        ) -> dict[int, dict]:
            result = super().orders_for_ids(
                target_ids=target_ids,
                lookback_days=lookback_days,
            )

            result[103]["crossBorderType"] = 1

            return result

    with pytest.raises(
        DataCheckError,
        match="трансграничные",
    ):
        collect_and_group(
            client=CrossBorderClient(),
            selected_supplies=[
                {"id": "WB-GI-1"},
                {"id": "WB-GI-2"},
            ],
            lookback_days=31,
            sticker_width=58,
            sticker_height=40,
        )
