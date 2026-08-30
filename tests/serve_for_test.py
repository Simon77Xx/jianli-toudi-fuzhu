from __future__ import annotations

import sys
from pathlib import Path
import gradio as gr
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


demo = app.build_app()
demo.launch(
    server_name="127.0.0.1",
    server_port=int(os.getenv("TEST_PORT", "7863")),
    inbrowser=False,
    prevent_thread_lock=False,
    theme=gr.themes.Base(),
    css=app.CSS,
    i18n=app.CHINESE_I18N,
    footer_links=[],
)
