from __future__ import annotations

import os
from functools import lru_cache
from io import BytesIO

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

# В Streamlit Community Cloud обычно доступен DejaVu Sans.
# Он поддерживает кириллицу, если артикул содержит русские символы.
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
    Возвращает шрифт для служебных этикеток.

    Если Unicode-шрифт не найден, используется Helvetica-Bold.
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
    Готовит артикул только для служебной этикетки-разделителя.

    Удаляется только начальный префикс NAKL_, без изменения исходных
    данных WB, таблицы артикулов и названий отдельных PDF.
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
    """
    Разбивает длинный текст на строки без изменения его символов.

    Перенос выполняется посимвольно, потому что артикулы часто состоят
    из длинных фрагментов с `_`, `-`, цифрами и латинскими буквами.
    """
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
    """
    Подбирает уменьшенный размер шрифта и переносы для артикула.

    Основной размер уменьшен: ранее поиск начинался с 22 pt,
    теперь — с 18 pt. Это даёт больше свободного места на этикетке.
    """
    article_text = article_for_separator(article)

    # Уменьшенный размер шрифта для компактной служебной этикетки.
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
    Рисует компактную служебную этикетку-разделитель.

    На этикетке печатаются:
    - артикул без начального префикса NAKL_;
    - количество оригинальных стикеров WB.

    Функция завершает текущую страницу PDF.
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

    # Ранее было максимум 18 pt. Уменьшено до 15 pt.
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
    Добавляет одну оригинальную PNG-этикетку WB на отдельную PDF-страницу.

    Изображение WB не обрезается, не растягивается и не перерисовывается.
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

        # Нельзя растягивать или обрезать QR/штрихкод.
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


def make_pdf(
    groups: list[dict],
    width_mm: int = 58,
    height_mm: int = 40,
    include_group_separators: bool = False,
) -> bytes:
    """
    Создаёт PDF для печати стикеров WB.

    Один оригинальный стикер WB — одна страница PDF.

    Если `include_group_separators=True`, перед каждой группой артикулов,
    включая первую, добавляется служебная этикетка с артикулом без
    начального префикса NAKL_ и количеством стикеров.

    Оригинальные стикеры WB не изменяются.
    """
    if (width_mm, height_mm) not in ALLOWED_SIZES:
        raise ValueError(
            "Допустимы только размеры 58×40 или 40×30 мм."
        )

    if not groups:
        raise ValueError(
            "Нельзя создать PDF без групп стикеров."
        )

    output = BytesIO()

    page_width = width_mm * mm
    page_height = height_mm * mm

    pdf = canvas.Canvas(
        output,
        pagesize=(page_width, page_height),
        pageCompression=1,
    )

    sticker_pages_count = 0

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

        sticker_count = len(stickers)

        if include_group_separators:
            draw_group_separator(
                pdf=pdf,
                article=article,
                sticker_count=sticker_count,
                page_width=page_width,
                page_height=page_height,
            )

        for png_bytes in stickers:
            draw_wb_sticker(
                pdf=pdf,
                png_bytes=png_bytes,
                page_width=page_width,
                page_height=page_height,
            )

            sticker_pages_count += 1

    if sticker_pages_count == 0:
        raise ValueError(
            "PDF не создан: в группах отсутствуют стикеры."
        )

    pdf.save()

    return output.getvalue()