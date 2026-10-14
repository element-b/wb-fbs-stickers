from __future__ import annotations

import os
from functools import lru_cache
from io import BytesIO
from typing import Any

from pypdf import PdfReader, PdfWriter
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


SEPARATOR_FONT_NAME = "OzonTaskSeparatorFont"

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


@lru_cache(maxsize=1)
def _separator_font_name() -> str:
    """
    Возвращает Unicode-шрифт для служебных этикеток Ozon.

    Если Unicode-шрифт недоступен, используется Helvetica. На Streamlit
    Community Cloud обычно доступен DejaVu Sans с поддержкой кириллицы.
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


def _split_text_by_width(
    text: str,
    font_name: str,
    font_size: float,
    max_width: float,
) -> list[str]:
    """Разбивает строку на части, не меняя символы артикула."""
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


def _article_layout(
    article: str,
    font_name: str,
    max_width: float,
    max_lines: int = 3,
) -> tuple[float, list[str]]:
    """Подбирает размер шрифта и переносы длинного артикула."""
    clean_article = str(article or "").strip()

    for font_size in range(18, 6, -1):
        lines = _split_text_by_width(
            text=clean_article,
            font_name=font_name,
            font_size=float(font_size),
            max_width=max_width,
        )

        if len(lines) <= max_lines:
            return float(font_size), lines

    return 7.0, _split_text_by_width(
        text=clean_article,
        font_name=font_name,
        font_size=7.0,
        max_width=max_width,
    )


def _page_dimensions_from_pdf(
    pdf_bytes: bytes,
) -> tuple[float, float]:
    """
    Получает размер первой страницы оригинального PDF Ozon в points.

    Служебная этикетка создаётся того же размера, что и следующая
    оригинальная QR-этикетка Ozon.
    """
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        raise ValueError(
            "Не удалось получить PDF-этикетку Ozon."
        )

    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError(
            "Ozon вернул этикетку не в формате PDF."
        )

    try:
        reader = PdfReader(
            BytesIO(pdf_bytes),
            strict=False,
        )
    except Exception as error:
        raise ValueError(
            "Не удалось прочитать PDF-этикетку Ozon."
        ) from error

    if reader.is_encrypted:
        raise ValueError(
            "Ozon вернул защищённый PDF, который нельзя объединить."
        )

    if not reader.pages:
        raise ValueError(
            "Ozon вернул PDF без страниц."
        )

    page = reader.pages[0]
    media_box = page.mediabox

    width = float(media_box.width)
    height = float(media_box.height)

    if width <= 0 or height <= 0:
        raise ValueError(
            "Ozon вернул PDF с некорректным размером страницы."
        )

    return width, height


def _draw_centered_lines(
    pdf: canvas.Canvas,
    lines: list[str],
    font_name: str,
    font_size: float,
    page_width: float,
    start_y: float,
    line_height: float,
) -> float:
    """Рисует строки по центру и возвращает следующую координату Y."""
    pdf.setFont(font_name, font_size)

    current_y = start_y

    for line in lines:
        pdf.drawCentredString(
            page_width / 2,
            current_y,
            line,
        )
        current_y -= line_height

    return current_y


def _create_task_separator_pdf(
    title: str,
    items: list[tuple[str, int]],
    posting_count: int,
    page_width: float,
    page_height: float,
) -> bytes:
    """
    Создаёт одну служебную страницу перед оригинальными QR-этикетками.

    Для одной товарной группы выводится:

    - название артикула;
    - суммарное количество товара;
    - число QR-этикеток / отправлений.

    Для многотоварного FBS-отправления выводится список артикулов
    и количеств. Это предотвращает дублирование одного QR-кода в двух
    товарных группах.
    """
    if not items:
        raise ValueError(
            "Нельзя создать задание Ozon без артикулов."
        )

    if posting_count < 1:
        raise ValueError(
            "Количество QR-этикеток Ozon должно быть положительным."
        )

    output = BytesIO()

    pdf = canvas.Canvas(
        output,
        pagesize=(page_width, page_height),
        pageCompression=1,
    )

    font_name = _separator_font_name()

    margin = max(3 * mm, page_width * 0.06)
    usable_width = page_width - (margin * 2)

    title_text = str(title or "ЗАДАНИЕ OZON").strip()

    title_size = min(
        13.0,
        max(8.0, page_height / 16),
    )

    title_lines = _split_text_by_width(
        text=title_text,
        font_name=font_name,
        font_size=title_size,
        max_width=usable_width,
    )

    if len(title_lines) > 2:
        title_lines = title_lines[:2]

    single_article = len(items) == 1

    if single_article:
        article, quantity = items[0]

        article_size, article_lines = _article_layout(
            article=article,
            font_name=font_name,
            max_width=usable_width,
        )

        quantity_size = min(
            16.0,
            max(10.0, page_height / 10),
        )

        blocks_height = (
            len(title_lines) * title_size * 1.2
            + len(article_lines) * article_size * 1.16
            + quantity_size
            + (10 * mm)
        )

        current_y = (
            (page_height + blocks_height) / 2
            - title_size
        )

        current_y = _draw_centered_lines(
            pdf=pdf,
            lines=title_lines,
            font_name=font_name,
            font_size=title_size,
            page_width=page_width,
            start_y=current_y,
            line_height=title_size * 1.2,
        )

        current_y -= 2.5 * mm

        current_y = _draw_centered_lines(
            pdf=pdf,
            lines=article_lines,
            font_name=font_name,
            font_size=article_size,
            page_width=page_width,
            start_y=current_y,
            line_height=article_size * 1.16,
        )

        current_y -= 2.5 * mm

        pdf.setFont(font_name, quantity_size)
        pdf.drawCentredString(
            page_width / 2,
            current_y,
            f"QTY: {quantity}",
        )

        current_y -= quantity_size * 1.4

        label_size = min(
            9.0,
            max(6.0, page_height / 24),
        )

        pdf.setFont(font_name, label_size)
        pdf.drawCentredString(
            page_width / 2,
            current_y,
            (
                f"QR-этикеток Ozon: {posting_count}"
            ),
        )

    else:
        heading_size = min(
            12.0,
            max(8.0, page_height / 16),
        )

        item_font_size = min(
            9.0,
            max(6.0, page_height / 26),
        )

        max_visible_items = 7
        visible_items = items[:max_visible_items]

        lines: list[str] = []

        for article, quantity in visible_items:
            article_lines = _split_text_by_width(
                text=article,
                font_name=font_name,
                font_size=item_font_size,
                max_width=usable_width * 0.76,
            )

            if len(article_lines) > 2:
                article_lines = article_lines[:2]

            if len(article_lines) == 1:
                lines.append(
                    f"{article_lines[0]} × {quantity}"
                )
            else:
                lines.extend(article_lines)
                lines.append(f"× {quantity}")

        hidden_item_count = len(items) - len(visible_items)

        if hidden_item_count > 0:
            lines.append(
                f"и ещё {hidden_item_count} артикул(а)"
            )

        total_lines_height = (
            len(title_lines) * title_size * 1.2
            + heading_size * 1.2
            + len(lines) * item_font_size * 1.22
            + item_font_size
            + (8 * mm)
        )

        current_y = (
            (page_height + total_lines_height) / 2
            - title_size
        )

        current_y = _draw_centered_lines(
            pdf=pdf,
            lines=title_lines,
            font_name=font_name,
            font_size=title_size,
            page_width=page_width,
            start_y=current_y,
            line_height=title_size * 1.2,
        )

        current_y -= 1.5 * mm

        pdf.setFont(font_name, heading_size)
        pdf.drawCentredString(
            page_width / 2,
            current_y,
            "АРТИКУЛЫ В ОТПРАВЛЕНИИ",
        )

        current_y -= heading_size * 1.5

        current_y = _draw_centered_lines(
            pdf=pdf,
            lines=lines,
            font_name=font_name,
            font_size=item_font_size,
            page_width=page_width,
            start_y=current_y,
            line_height=item_font_size * 1.22,
        )

        current_y -= 1.5 * mm

        pdf.setFont(font_name, item_font_size)
        pdf.drawCentredString(
            page_width / 2,
            current_y,
            f"QR-этикеток Ozon: {posting_count}",
        )

    pdf.showPage()
    pdf.save()

    separator_pdf = output.getvalue()

    if not separator_pdf.startswith(b"%PDF-"):
        raise ValueError(
            "Не удалось подготовить служебную этикетку Ozon."
        )

    return separator_pdf


def _append_pdf_pages(
    writer: PdfWriter,
    pdf_bytes: bytes,
) -> None:
    """
    Добавляет оригинальные страницы Ozon в итоговый PDF без изменений.

    Оригинальные QR-коды не растеризуются, не масштабируются,
    не обрезаются и не перерисовываются.
    """
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        raise ValueError(
            "Не удалось получить PDF-этикетку Ozon."
        )

    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError(
            "Ozon вернул этикетку не в формате PDF."
        )

    try:
        reader = PdfReader(
            BytesIO(pdf_bytes),
            strict=False,
        )
    except Exception as error:
        raise ValueError(
            "Не удалось прочитать PDF-этикетку Ozon."
        ) from error

    if reader.is_encrypted:
        raise ValueError(
            "Ozon вернул защищённый PDF, который нельзя объединить."
        )

    if not reader.pages:
        raise ValueError(
            "Ozon вернул PDF без страниц этикеток."
        )

    for page in reader.pages:
        writer.add_page(page)


def make_ozon_grouped_labels_pdf(
    groups: list[dict[str, Any]],
) -> bytes:
    """
    Создаёт общий PDF для печати Ozon FBS.

    Порядок страниц:

    1. Служебная этикетка задания;
    2. Оригинальная QR-этикетка или набор оригинальных QR-этикеток Ozon;
    3. Следующее задание;
    4. Следующие оригинальные QR-этикетки.

    Формат одной группы:

    ```python
    {
        "title": "NAKL_EXAMPLE",
        "items": [("NAKL_EXAMPLE", 12)],
        "posting_count": 5,
        "label_pdfs": [b"%PDF-...", ...],
    }
    ```

    Для многотоварного отправления `items` содержит несколько артикулов.
    """
    if not groups:
        raise ValueError(
            "Нельзя создать PDF без заданий Ozon."
        )

    writer = PdfWriter()

    for group in groups:
        if not isinstance(group, dict):
            raise ValueError(
                "Некорректная группа при создании PDF Ozon."
            )

        title = str(group.get("title") or "").strip()
        raw_items = group.get("items")
        posting_count = group.get("posting_count")
        label_pdfs = group.get("label_pdfs")

        if not title:
            raise ValueError(
                "В задании Ozon отсутствует заголовок."
            )

        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError(
                f"У задания `{title}` отсутствуют артикулы."
            )

        if not isinstance(label_pdfs, list) or not label_pdfs:
            raise ValueError(
                f"У задания `{title}` отсутствуют QR-этикетки Ozon."
            )

        try:
            normalized_posting_count = int(posting_count)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Некорректное количество отправлений у `{title}`."
            ) from error

        if normalized_posting_count < 1:
            raise ValueError(
                f"Количество отправлений у `{title}` должно быть "
                "положительным."
            )

        normalized_items: list[tuple[str, int]] = []

        for raw_item in raw_items:
            if (
                not isinstance(raw_item, tuple)
                or len(raw_item) != 2
            ):
                raise ValueError(
                    f"Некорректный артикул в задании `{title}`."
                )

            article = str(raw_item[0] or "").strip()

            if not article:
                raise ValueError(
                    f"В задании `{title}` найден пустой артикул."
                )

            try:
                quantity = int(raw_item[1])
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"Некорректное количество у артикула `{article}`."
                ) from error

            if quantity <= 0:
                raise ValueError(
                    f"Количество у артикула `{article}` должно быть "
                    "положительным."
                )

            normalized_items.append(
                (
                    article,
                    quantity,
                )
            )

        first_label_pdf = label_pdfs[0]

        page_width, page_height = _page_dimensions_from_pdf(
            first_label_pdf
        )

        separator_pdf = _create_task_separator_pdf(
            title=title,
            items=normalized_items,
            posting_count=normalized_posting_count,
            page_width=page_width,
            page_height=page_height,
        )

        _append_pdf_pages(
            writer=writer,
            pdf_bytes=separator_pdf,
        )

        for label_pdf in label_pdfs:
            _append_pdf_pages(
                writer=writer,
                pdf_bytes=label_pdf,
            )

    output = BytesIO()
    writer.write(output)

    result_pdf = output.getvalue()

    if not result_pdf.startswith(b"%PDF-"):
        raise ValueError(
            "Не удалось подготовить итоговый PDF Ozon."
        )

    return result_pdf