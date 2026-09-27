from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


def make_mysklad_xlsx(groups: list[dict]) -> bytes:
    """
    Создаёт XLSX-файл для импорта в МойСклад.

    В файле три столбца:

    - Артикул;
    - Количество;
    - СЦ назначения.

    Количество равно числу сборочных заданий / стикеров WB.
    """
    if not groups:
        raise ValueError(
            "Нельзя создать XLSX: отсутствуют группы артикулов."
        )

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "WB FBS"

    header_fill = PatternFill(
        start_color="009B77",
        end_color="009B77",
        fill_type="solid",
    )

    header_font = Font(
        color="FFFFFF",
        bold=True,
    )

    worksheet.append(
        [
            "Артикул",
            "Количество",
            "СЦ назначения",
        ]
    )

    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
        )

    row_count = 0

    for group in groups:
        article = str(group.get("article") or "").strip()
        order_ids = group.get("order_ids", [])
        centers = group.get("distribution_centers", [])

        if not article:
            raise ValueError(
                "Нельзя создать XLSX: найден пустой артикул."
            )

        if not isinstance(order_ids, list) or not order_ids:
            raise ValueError(
                f"Нельзя создать XLSX: отсутствуют задания у `{article}`."
            )

        if not isinstance(centers, list):
            centers = []

        centers_text = ", ".join(
            str(center).strip()
            for center in centers
            if str(center).strip()
        )

        worksheet.append(
            [
                article,
                len(order_ids),
                centers_text or "—",
            ]
        )

        row_count += 1

    if row_count == 0:
        raise ValueError(
            "Нельзя создать XLSX без строк."
        )

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions

    worksheet.column_dimensions["A"].width = 42
    worksheet.column_dimensions["B"].width = 16
    worksheet.column_dimensions["C"].width = 34

    for row in worksheet.iter_rows(
        min_row=2,
        max_row=worksheet.max_row,
        min_col=1,
        max_col=3,
    ):
        row[0].alignment = Alignment(
            horizontal="left",
            vertical="center",
        )

        row[1].alignment = Alignment(
            horizontal="center",
            vertical="center",
        )

        row[2].alignment = Alignment(
            horizontal="left",
            vertical="center",
            wrap_text=True,
        )

    output = BytesIO()
    workbook.save(output)

    return output.getvalue()