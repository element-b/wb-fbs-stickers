from io import BytesIO

from PIL import Image
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def make_pdf(groups: list[dict], width_mm: int = 58, height_mm: int = 40) -> bytes:
    if (width_mm, height_mm) not in ((58, 40), (40, 30)):
        raise ValueError("Допустимы только размеры 58×40 или 40×30 мм.")

    output = BytesIO()
    page_w, page_h = width_mm * mm, height_mm * mm
    pdf = canvas.Canvas(output, pagesize=(page_w, page_h), pageCompression=1)

    for group in groups:
        for png_bytes in group["stickers"]:
            with Image.open(BytesIO(png_bytes)) as image:
                image_w, image_h = image.size
            if image_h == 0 or abs(image_w / image_h - page_w / page_h) > 0.01:
                raise ValueError(
                    "Пропорции стикера WB не соответствуют выбранному размеру."
                )

            # Сохраняем пропорции исходника, без обрезки и растяжения.
            pdf.drawImage(
                ImageReader(BytesIO(png_bytes)),
                0,
                0,
                width=page_w,
                height=page_h,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )
            pdf.showPage()

    pdf.save()
    return output.getvalue()