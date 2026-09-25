from __future__ import annotations

import base64
import html
import json

import streamlit.components.v1 as components


def render_open_pdf_button(
    article: str,
    pdf_bytes: bytes,
) -> None:
    """
    Рисует кнопку, открывающую PDF в новой вкладке браузера.

    PDF не сохраняется на диск и не публикуется по постоянной ссылке.
    Он передаётся из памяти текущей Streamlit-сессии в браузер только
    для открытия пользователем.

    В новой вкладке откроется встроенный просмотрщик PDF браузера,
    из которого можно распечатать стикеры.
    """
    if not pdf_bytes:
        raise ValueError(
            f"Не удалось подготовить PDF для артикула `{article}`."
        )

    pdf_base64 = base64.b64encode(pdf_bytes).decode("ascii")

    # json.dumps безопасно экранирует строку для вставки в JavaScript.
    article_json = json.dumps(str(article), ensure_ascii=False)
    pdf_base64_json = json.dumps(pdf_base64)

    article_html = html.escape(str(article))

    component_html = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                margin: 0;
                padding: 0;
                font-family: -apple-system, BlinkMacSystemFont,
                    "Segoe UI", sans-serif;
                background: transparent;
            }}

            .open-button {{
                width: 100%;
                min-height: 42px;
                border: 0;
                border-radius: 7px;
                background: #009B77;
                color: #FFFFFF;
                cursor: pointer;
                font-size: 14px;
                font-weight: 600;
                padding: 9px 14px;
            }}

            .open-button:hover {{
                background: #008268;
            }}

            .message {{
                display: none;
                margin-top: 7px;
                color: #B42318;
                font-size: 12px;
                line-height: 1.35;
            }}
        </style>
    </head>
    <body>
        <button
            class="open-button"
            id="open-pdf-button"
            type="button"
        >
            ↗ Открыть стикеры артикула в новой вкладке
        </button>

        <div class="message" id="popup-message">
            Браузер заблокировал новую вкладку. Разрешите всплывающие окна
            для этого сайта и нажмите кнопку ещё раз.
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

                        for (let index = 0; index < binary.length; index += 1) {{
                            bytes[index] = binary.charCodeAt(index);
                        }}

                        const pdfBlob = new Blob(
                            [bytes],
                            {{ type: "application/pdf" }}
                        );

                        const pdfUrl = URL.createObjectURL(pdfBlob);

                        const openedWindow = window.open(
                            pdfUrl,
                            "_blank"
                        );

                        if (!openedWindow) {{
                            document.getElementById(
                                "popup-message"
                            ).style.display = "block";

                            URL.revokeObjectURL(pdfUrl);
                            return;
                        }}

                        openedWindow.document.title =
                            "Стикеры WB — " + article;

                        // Даём браузеру время открыть PDF, затем
                        // освобождаем временный URL.
                        window.setTimeout(function () {{
                            URL.revokeObjectURL(pdfUrl);
                        }}, 120000);

                    }} catch (error) {{
                        document.getElementById(
                            "popup-message"
                        ).style.display = "block";
                    }}
                }});
        </script>
    </body>
    </html>
    """

    components.html(
        component_html,
        height=54,
        scrolling=False,
    )