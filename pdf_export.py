from __future__ import annotations

import os
from functools import lru_cache
from io import BytesIO
from typing import Any

from PIL import Image
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ALLOWED_SIZES = (
    (58, 40),
    (40, 30),
)

SEPARATOR_FONT_NAME = "WBSeparatorFont"

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


@lru_cache(maxsize=1)
def get_separator_font_name() -> str:
    """
    Возвращает Unicode-шрифт для служебной этикетки.

    DejaVu Sans обычно установлен в Streamlit Community Cloud и
    поддерживает кириллицу. Если шрифт не найден, используется Helvetica.
    """
    if SEPARATOR_FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return SEPARATOR_FONT_NAME

    for font_path in FONT_CANDIDATES:
        if not os.path.isfile(font_path):
            continue

        try:
            pdfmetrics.registerFont(
                TTFont(
                    SEPARATOR_FONT_NAME,
                    font_path,
                )
            )
            return SEPARATOR_FONT_NAME
        except Exception:
            continue

    return "Helvetica-Bold"


def article_for_separator(article: str) -> str:
    """
    Удаляет префикс NAKL_ только на служебной этикетке.

    Исходный артикул WB в таблице, XLSX и других данных не изменяется.
    """
    original_article = str(article).strip()

    if original_article.upper().startswith("NAKL_"):
        shortened_article = original_article[5:].strip()

        if shortened_article:
            return shortened_article

    return original_article


def split_text_by_width(
    text: str,
    font_name: str,
    font_size: float,
    max_width: float,
) -> list[str]:
    """Разбивает длинный артикул на строки без изменения символов."""
    if not text:
        return [""]

    lines: list[str] = []
    current_line = ""

    for character in text:
        candidate = current_line + character

        if (
            current_line
            and pdfmetrics.stringWidth(
                candidate,
                font_name,
                font_size,
            ) > max_width
        ):
            lines.append(current_line)
            current_line = character
        else:
            current_line = candidate

    if current_line:
        lines.append(current_line)

    return lines


def get_article_layout(
    article: str,
    font_name: str,
    max_width: float,
) -> tuple[float, list[str]]:
    """Подбирает размер шрифта и переносы для служебной этикетки."""
    article_text = article_for_separator(article)

    for font_size in range(18, 6, -1):
        lines = split_text_by_width(
            text=article_text,
            font_name=font_name,
            font_size=float(font_size),
            max_width=max_width,
        )

        if len(lines) <= 3:
            return float(font_size), lines

    return 7.0, split_text_by_width(
        text=article_text,
        font_name=font_name,
        font_size=7.0,
        max_width=max_width,
    )


def draw_group_separator(
    pdf: canvas.Canvas,
    article: str,
    sticker_count: int,
    page_width: float,
    page_height: float,
) -> None:
    """
    Рисует служебную этикетку группы.

    На этикетке:
    - артикул без начального NAKL_;
    - количество стикеров WB.
    """
    font_name = get_separator_font_name()

    margin = 4 * mm
    usable_width = page_width - (margin * 2)

    article_font_size, article_lines = get_article_layout(
        article=article,
        font_name=font_name,
        max_width=usable_width,
    )

    line_height = article_font_size * 1.16
    article_block_height = len(article_lines) * line_height

    quantity_text = f"QTY: {sticker_count}"
    quantity_font_size = min(
        15.0,
        max(10.0, page_height / 10),
    )

    total_content_height = (
        article_block_height
        + quantity_font_size
        + (6 * mm)
    )

    start_y = (
        (page_height + total_content_height) / 2
        - line_height
    )

    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont(font_name, article_font_size)

    current_y = start_y

    for line in article_lines:
        pdf.drawCentredString(
            page_width / 2,
            current_y,
            line,
        )
        current_y -= line_height

    current_y -= 3.5 * mm

    pdf.setFont(font_name, quantity_font_size)
    pdf.drawCentredString(
        page_width / 2,
        current_y,
        quantity_text,
    )

    pdf.showPage()


def draw_wb_sticker(
    pdf: canvas.Canvas,
    png_bytes: bytes,
    page_width: float,
    page_height: float,
) -> None:
    """
    Добавляет один оригинальный PNG-стикер WB на отдельную страницу PDF.

    Изображение не отражается, не обрезается и не перерисовывается.
    """
    if not isinstance(png_bytes, bytes) or not png_bytes:
        raise ValueError(
            "Не удалось получить изображение стикера WB."
        )

    try:
        with Image.open(BytesIO(png_bytes)) as image:
            image_width, image_height = image.size
            image_format = image.format

        if image_format != "PNG":
            raise ValueError(
                "WB вернул стикер не в формате PNG."
            )

        if image_width <= 0 or image_height <= 0:
            raise ValueError(
                "WB вернул PNG с некорректным размером."
            )

        image_ratio = image_width / image_height
        page_ratio = page_width / page_height

        if abs(image_ratio - page_ratio) > 0.01:
            raise ValueError(
                "Пропорции стикера WB не соответствуют "
                "выбранному размеру страницы."
            )

        pdf.drawImage(
            ImageReader(BytesIO(png_bytes)),
            x=0,
            y=0,
            width=page_width,
            height=page_height,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )

        pdf.showPage()

    except OSError as error:
        raise ValueError(
            "Не удалось прочитать PNG-изображение стикера WB."
        ) from error


def build_pdf_pages(
    groups: list[dict],
    include_group_separators: bool,
) -> list[dict[str, Any]]:
    """
    Собирает логический порядок PDF-страниц.

    Обычная последовательность группы:

    1. Служебная этикетка артикула;
    2. Первый оригинальный WB-стикер;
    3. Второй оригинальный WB-стикер;
    4. И так далее.

    При reverse_page_order список страниц будет развёрнут перед печатью.
    """
    pages: list[dict[str, Any]] = []

    for group in groups:
        if not isinstance(group, dict):
            raise ValueError(
                "Некорректная группа при создании PDF."
            )

        article = str(group.get("article") or "").strip()
        stickers = group.get("stickers", [])
        order_ids = group.get("order_ids", [])

        if not article:
            raise ValueError(
                "В группе стикеров отсутствует артикул."
            )

        if not isinstance(stickers, list) or not stickers:
            raise ValueError(
                f"У артикула `{article}` отсутствуют стикеры."
            )

        if not isinstance(order_ids, list):
            raise ValueError(
                f"Некорректный список заданий у артикула `{article}`."
            )

        if order_ids and len(order_ids) != len(stickers):
            raise ValueError(
                f"Количество заданий и стикеров не совпадает "
                f"у артикула `{article}`."
            )

        if include_group_separators:
            pages.append(
                {
                    "type": "separator",
                    "article": article,
                    "sticker_count": len(stickers),
                }
            )

        for png_bytes in stickers:
            pages.append(
                {
                    "type": "sticker",
                    "png_bytes": png_bytes,
                }
            )

    return pages


def make_pdf(
    groups: list[dict],
    width_mm: int = 58,
    height_mm: int = 40,
    include_group_separators: bool = False,
    reverse_page_order: bool = False,
) -> bytes:
    """
    Создаёт PDF для печати оригинальных WB-стикеров.

    Один стикер — одна страница PDF.

    Если `include_group_separators=True`, перед каждой группой,
    включая первую, добавляется служебная этикетка с артикулом.

    Если `reverse_page_order=True`, порядок PDF-страниц разворачивается.
    Это удобно для рулонной печати: при последующей размотке ленты
    сборщик видит обычный логический порядок групп.

    Изображения WB не отражаются и не меняются — меняется только
    очередность страниц PDF.
    """
    if (width_mm, height_mm) not in ALLOWED_SIZES:
        raise ValueError(
            "Допустимы только размеры 58×40 или 40×30 мм."
        )

    if not groups:
        raise ValueError(
            "Нельзя создать PDF без групп стикеров."
        )

    pages = build_pdf_pages(
        groups=groups,
        include_group_separators=include_group_separators,
    )

    if not pages:
        raise ValueError(
            "PDF не создан: в группах отсутствуют стикеры."
        )

    if reverse_page_order:
        pages.reverse()

    output = BytesIO()

    page_width = width_mm * mm
    page_height = height_mm * mm

    pdf = canvas.Canvas(
        output,
        pagesize=(page_width, page_height),
        pageCompression=1,
    )

    sticker_pages_count = 0

    for page in pages:
        page_type = page["type"]

        if page_type == "separator":
            draw_group_separator(
                pdf=pdf,
                article=page["article"],
                sticker_count=page["sticker_count"],
                page_width=page_width,
                page_height=page_height,
            )

        elif page_type == "sticker":
            draw_wb_sticker(
                pdf=pdf,
                png_bytes=page["png_bytes"],
                page_width=page_width,
                page_height=page_height,
            )
            sticker_pages_count += 1

        else:
            raise ValueError(
                "Неизвестный тип страницы при создании PDF."
            )

    if sticker_pages_count == 0:
        raise ValueError(
            "PDF не создан: в группах отсутствуют стикеры."
        )

    pdf.save()

    return output.getvalue()