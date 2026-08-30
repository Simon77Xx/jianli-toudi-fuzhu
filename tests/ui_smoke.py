from __future__ import annotations

from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright


artifact = Path("output") / "playwright" / "landing.png"
artifact.parent.mkdir(parents=True, exist_ok=True)
base_url = os.getenv("TEST_BASE_URL", "http://127.0.0.1:7863")

with sync_playwright() as playwright:
    print(f"UI_SMOKE_START base_url={base_url}", flush=True)
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    # Gradio keeps a live event connection, so networkidle is not a stable
    # readiness signal for this app. DOMContentLoaded plus a short render wait
    # verifies the actual page without waiting forever for the socket to quiet.
    page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_timeout(1000)
    assert page.get_by_text("投递前，把事实变成证据").is_visible()
    assert page.get_by_text("完整 LaTeX 项目 ZIP").is_visible()
    assert page.get_by_text("当前 PDF 简历").is_visible()
    assert page.get_by_text("粘贴一份 JD").is_visible()
    assert page.get_by_role("button", name="开始证据分析").is_visible()
    assert page.get_by_text("读取 GitHub 公开证据（默认关闭）").is_visible()
    upload_boxes = page.locator('button[aria-dropeffect="copy"] .wrap')
    assert upload_boxes.count() >= 2
    pseudo_content = upload_boxes.first.evaluate("element => getComputedStyle(element, '::after').content")
    assert "将文件拖放到这里" in pseudo_content
    assert page.locator("input[type=file]").count() >= 2
    page.screenshot(path=str(artifact), full_page=True)
    browser.close()

print(f"UI_SMOKE=OK screenshot={artifact}")
