from __future__ import annotations

import base64
import json

import streamlit.components.v1 as components


def render_open_pdf_button(
    article: str,
    pdf_bytes: bytes,
) -> None:
    """
    Показывает компактную кнопку для открытия PDF в новой вкладке.

    PDF передаётся из памяти текущей Streamlit-сессии в браузер.
    Постоянная публичная ссылка и файлы на сервере не создаются.
    """
    if not pdf_bytes:
        raise ValueError(
            f"Не удалось подготовить PDF для артикула `{article}`."
        )

    pdf_base64 = base64.b64encode(pdf_bytes).decode("ascii")

    article_json = json.dumps(
        str(article),
        ensure_ascii=False,
    )

    pdf_base64_json = json.dumps(pdf_base64)

    component_html = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                margin: 0;
                padding: 0;
                background: transparent;
                font-family: Arial, sans-serif;
            }}

            #open-pdf-button {{
                width: 100%;
                min-height: 34px;
                padding: 7px 9px;
                border: none;
                border-radius: 6px;
                background: #009B77;
                color: #FFFFFF;
                cursor: pointer;
                font-size: 12px;
                font-weight: 600;
                white-space: nowrap;
            }}

            #open-pdf-button:hover {{
                background: #008268;
            }}

            #error-message {{
                display: none;
                margin-top: 4px;
                color: #B42318;
                font-size: 10px;
                line-height: 1.25;
            }}
        </style>
    </head>
    <body>
        <button id="open-pdf-button" type="button">
            ↗ Открыть PDF
        </button>

        <div id="error-message">
            Разрешите всплывающие окна для этого сайта.
        </div>

        <script>
            const article = {article_json};
            const pdfBase64 = {pdf_base64_json};

            document
                .getElementById("open-pdf-button")
                .addEventListener("click", function () {{
                    try {{
                        const binary = window.atob(pdfBase64);
                        const bytes = new Uint8Array(binary.length);

                        for (
                            let index = 0;
                            index < binary.length;
                            index += 1
                        ) {{
                            bytes[index] = binary.charCodeAt(index);
                        }}

                        const pdfBlob = new Blob(
                            [bytes],
                            {{
                                type: "application/pdf"
                            }}
                        );

                        const pdfUrl = URL.createObjectURL(pdfBlob);

                        const openedWindow = window.open(
                            pdfUrl,
                            "_blank"
                        );

                        if (!openedWindow) {{
                            document.getElementById(
                                "error-message"
                            ).style.display = "block";

                            URL.revokeObjectURL(pdfUrl);
                            return;
                        }}

                        window.setTimeout(function () {{
                            URL.revokeObjectURL(pdfUrl);
                        }}, 120000);

                    }} catch (error) {{
                        document.getElementById(
                            "error-message"
                        ).style.display = "block";
                    }}
                }});
        </script>
    </body>
    </html>
    """

    components.html(
        component_html,
        height=40,
        scrolling=False,
    )