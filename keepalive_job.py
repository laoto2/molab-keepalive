#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot keep-alive cho GitHub Actions: mở trang notebook molab bằng Chromium
headless, giữ mở WAKE_WAIT giây (marimo nối websocket từ trang), rồi thoát.

Secret bắt buộc: MOLAB_NOTEBOOK_URL (trang notebook trên molab — KHÔNG phải *.srv.us)
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

URL = os.environ.get("MOLAB_NOTEBOOK_URL", "").strip()
WAIT = int(os.environ.get("WAKE_WAIT", "60"))
if not URL:
    sys.exit("❌ thiếu secret MOLAB_NOTEBOOK_URL")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(3000)
    html = page.content()
    title = (page.title() or "-")[:120]
    looks = ("marimo" in html.lower()) or ("molab" in html.lower())
    print(f"[keepalive] title={title!r} · looks_like_marimo={looks}")
    print(f"[keepalive] giữ phiên mở thêm {WAIT}s …")
    page.wait_for_timeout(WAIT * 1000)
    browser.close()
print("[keepalive] xong — session đã có hoạt động")
