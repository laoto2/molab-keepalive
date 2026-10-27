#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keepalive_job.py v2 — WAKE → RUN → HOLD cho sandbox molab.

V2 khác v1 thế nào (v1 chỉ mở trang 60 s rồi đóng — KHÔNG chạy được cell nào):
  * molab ÉP auto_instantiate=false (_MARIMO_CONFIG_OVERLOAD_RUNTIME_AUTO_INSTANTIATE=false)
    ⇒ mở trang không bao giờ chạy cell; và visit ẩn danh chỉ là read-only preview tĩnh.
  * v2 đăng nhập bằng cookie (secret MOLAB_STATE, lấy 1 lần bằng deps/capture_login.py),
    tìm app-frame của marimo, rồi CHẠY TOÀN BỘ CELL bằng 3 cách dự phòng:
      1) Command palette (Ctrl+K → "run all" → Enter/chọn "Re-run all cells")
      2) Hotkey Ctrl+Shift+R ("Run all stale cells" — default của marimo)
      3) Bấm lần lượt mọi nút [data-testid="run-button"]
    ⇒ cell 🚑 boot chạy = boot.sh dựng lại toàn bộ stack sau relaunch (idempotent).
  * Sau đó HOLD: giữ phiên mở (websocket = hoạt động) và poll MOLAB_API_URL
    (URL công khai *.srv.us) tới khi gateway trả lời (HTTP 401/200) ⇒ stack đã sống.
    Stack khoẻ sẵn thì thoát nhanh (~2 phút) — cron 5 phút vẫn rất rẻ.

Secrets (GitHub → Settings → Secrets → Actions):
  MOLAB_NOTEBOOK_URL  (bắt buộc)  trang notebook trên molab — KHÔNG phải *.srv.us
  MOLAB_STATE         (khuyến nghị) storage_state.json — gốc, hoặc base64 của nó
  MOLAB_API_URL       (khuyến nghị) https://xxxx.srv.us — để verify stack đã hồi phục

Env tuỳ chọn: WAKE_WAIT (giữ tối thiểu, 90s) · HOLD_MAX (giữ tối đa khi đang hồi phục,
1500s) · POLL (chu kỳ poll API, 30s) · RUN_WAIT (chờ cell chạy, 90s) · HEADLESS (1)
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

from playwright.sync_api import sync_playwright

NOTEBOOK_URL = os.environ.get("MOLAB_NOTEBOOK_URL", "").strip()
STATE_RAW = os.environ.get("MOLAB_STATE", "").strip()
API_URL = os.environ.get("MOLAB_API_URL", "").strip().rstrip("/")
WAKE_WAIT = int(os.environ.get("WAKE_WAIT", "90"))
HOLD_MAX = int(os.environ.get("HOLD_MAX", "1500"))
POLL = int(os.environ.get("POLL", "30"))
RUN_WAIT = int(os.environ.get("RUN_WAIT", "90"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "")


def log(*a):
    print("[keepalive]", *a, flush=True)


def summary(lines):
    if not SUMMARY:
        return
    try:
        with open(SUMMARY, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def state_file():
    """MOLAB_STATE: JSON gốc hoặc base64 — ghi ra /tmp rồi trả về đường dẫn."""
    if not STATE_RAW:
        return ""
    path = "/tmp/molab_state.json"
    if STATE_RAW.startswith("{"):
        open(path, "w").write(STATE_RAW)
    else:
        import base64
        open(path, "wb").write(base64.b64decode(STATE_RAW))
    json.load(open(path))  # validate
    return path


def api_ok():
    """True nếu gateway công khai đã trả lời (401 = khoẻ: bắt buộc key)."""
    if not API_URL:
        return None
    try:
        req = urllib.request.Request(f"{API_URL}/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 401, 403)
    except urllib.error.HTTPError as e:
        return e.code in (200, 401, 403)
    except Exception:
        return False


def find_app_frame(page):
    """Chọn frame chứa app marimo (ưu tiên frame có run-button / #App)."""
    best, best_score = None, -1
    for f in page.frames:
        score = 0
        try:
            if f.locator('[data-testid="run-button"]').count():
                score += 10
            if f.locator("#App").count():
                score += 5
            if "sb.molab" in f.url or "marimo" in (f.url or "").lower():
                score += 2
        except Exception:
            pass
        if score > best_score:
            best, best_score = f, score
    return best if best_score > 0 else None


def open_editor(app, page):
    """Nếu đang ở preview (0 run-button): bấm nút "Run or Edit"/"Edit" để vào editor."""
    for pat in ("run or edit", "open in editor", "switch to edit"):
        try:
            loc = app.get_by_text(re.compile(pat.replace(" ", r"\s+"), re.I))
            if loc.count():
                loc.first.click(timeout=4000)
                log(f'bấm "{pat}" để vào editor…')
                page.wait_for_timeout(8000)
                return True
        except Exception:
            pass
    return False


def press_run_all(app, page):
    """Chạy toàn bộ cell bằng 3 cách dự phòng. Trả về tên cách dùng được."""
    page.on("dialog", lambda d: d.accept())
    # 1) Command palette: Ctrl+K → gõ "run all" → click/Enter
    try:
        app.locator("body").click(timeout=3000)
        page.keyboard.press("Control+k")
        page.wait_for_timeout(700)
        page.keyboard.type("run all", delay=40)
        page.wait_for_timeout(700)
        item = app.get_by_text(re.compile("re-?run all cells", re.I)).first
        if item.count():
            item.click(timeout=3000)
            log("run-all: command palette (click item)")
            return "palette"
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        log("run-all: command palette (Enter)")
        return "palette-enter"
    except Exception as e:
        log("run-all: palette lỗi:", type(e).__name__, str(e)[:120])
    # 2) Hotkey mặc định của marimo: Ctrl+Shift+R = "Run all stale cells"
    try:
        app.locator("body").click(timeout=3000)
        page.keyboard.press("Control+Shift+KeyR")
        page.wait_for_timeout(500)
        log("run-all: hotkey Ctrl+Shift+R")
        return "hotkey"
    except Exception as e:
        log("run-all: hotkey lỗi:", type(e).__name__, str(e)[:120])
    # 3) Bấm mọi nút run của cell
    try:
        btns = app.locator('[data-testid="run-button"]')
        n = btns.count()
        clicked = 0
        for i in range(n):
            try:
                btns.nth(i).click(timeout=2000)
                clicked += 1
            except Exception:
                pass
        log(f"run-all: bấm {clicked}/{n} nút run-button")
        if clicked:
            return "run-buttons"
    except Exception as e:
        log("run-all: run-button lỗi:", type(e).__name__, str(e)[:120])
    return ""


def instantiated(app):
    """Heuristic: cell 🚑 đã render output (có chữ boot/Khôi phục/stack)."""
    try:
        txt = app.locator("body").inner_text(timeout=5000)
        return any(k in txt for k in ("boot.sh", "Khôi phục", "keepalive.log", "llama-server"))
    except Exception:
        return False


def main():
    if not NOTEBOOK_URL:
        sys.exit("❌ thiếu secret MOLAB_NOTEBOOK_URL (trang notebook trên molab)")
    sf = state_file()
    log("notebook:", NOTEBOOK_URL)
    log("auth:", "có (MOLAB_STATE)" if sf else "KHÔNG (ẩn danh) — cần secret MOLAB_STATE để chạy cell")
    log("api verify:", API_URL or "(chưa set MOLAB_API_URL)")

    healthy_start = api_ok()
    if healthy_start:
        log(f"🟢 stack ĐÃ KHOẺ trước khi mở trang (API {API_URL} trả lời) — chỉ giữ phiên {WAKE_WAIT}s")
    else:
        log("🔴 stack chưa khoẻ (hoặc chưa cấu hình API_URL) — sẽ cố chạy cell + hold")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(storage_state=sf or None)
        page = ctx.new_page()
        static_mode = []
        page.on("console", lambda m: static_mode.append(1) if "static notebook" in (m.text or "") else None)

        page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        app = find_app_frame(page)
        title = page.title()
        log("title:", title)
        if app is None:
            log("⚠️ không tìm thấy app-frame marimo — trang có thể là login/preview chặn")
            summary(["## molab keepalive", "- ❌ không tìm thấy app marimo (kiểm tra URL/auth)"])
            page.wait_for_timeout(min(WAKE_WAIT, 60) * 1000)
            browser.close()
            return

        n_btn = app.locator('[data-testid="run-button"]').count()
        # "Sign In" chỉ coi là ẩn danh khi đồng thời KHÔNG có nút run (tránh false-positive)
        anon = n_btn == 0 and "Sign In" in page.locator("body").inner_text(timeout=5000)
        log(f"app-frame: {app.url[:90]}")
        log(f"run-buttons: {n_btn} · anonymous={anon} · static_console={bool(static_mode)}")

        # đã đăng nhập nhưng vẫn preview (0 run-button) → bấm "Run or Edit" vào editor
        if sf and n_btn == 0 and not anon:
            if open_editor(app, page):
                app = find_app_frame(page) or app
                n_btn = app.locator('[data-testid="run-button"]').count()
                log(f"sau Run-or-Edit: run-buttons={n_btn}")

        if anon:
            log("⛔ CHẾ ĐỘ ẨN DANH (thiếu/chết cookie): read-only preview — KHÔNG thể chạy cell.")
            log("   →Fix: chạy deps/capture_login.py ở máy cá nhân để lấy cookie,")
            log("     rồi `gh secret set MOLAB_STATE < <(base64 -w0 storage_state.json)`")
            summary(["## molab keepalive",
                     "- ⚠️ Ẩn danh (read-only preview) — không chạy được cell.",
                     "- Cần secret `MOLAB_STATE` (cookie) + khuyến nghị `MOLAB_API_URL`.",
                     f"- API: {'khoẻ ✅' if healthy_start else 'chưa khoẻ ❌'}"])
            page.wait_for_timeout(min(WAKE_WAIT, 120) * 1000)
            browser.close()
            return

        if n_btn == 0 and not static_mode:
            log("⚠️ đã vào app nhưng không thấy nút run — molab có thể đổi UI; giữ phiên ngắn rồi thoát.")
            summary(["## molab keepalive", "- ⚠️ app-load nhưng 0 run-button (UI đổi?) — chỉ giữ phiên ngắn."])
            page.wait_for_timeout(min(WAKE_WAIT, 120) * 1000)
            browser.close()
            return

        if healthy_start:
            # Stack khoẻ — không cần chạy lại cell (tránh làm phiền phiên đang sống)
            summary(["## molab keepalive", "- 🟢 stack khoẻ — giữ phiên ngắn.", f"- API: `{API_URL}`"])
            page.wait_for_timeout(WAKE_WAIT * 1000)
            browser.close()
            return

        # --- cần khôi phục: chạy toàn bộ cell ---
        how = press_run_all(app, page)
        t0 = time.time()
        ok = False
        while time.time() - t0 < RUN_WAIT:
            page.wait_for_timeout(5000)
            if instantiated(app):
                ok = True
                break
        log("instantiate:", "✅ cell boot đã render" if ok else "⚠️ chưa thấy marker sau", f"{time.time()-t0:.0f}s",
            f"(cách: {how or 'không'})")

        # --- HOLD: giữ phiên tới khi API khoẻ hoặc hết thời gian ---
        t0 = time.time()
        healthy = api_ok()
        while not healthy and time.time() - t0 < HOLD_MAX:
            log(f"… hold {time.time()-t0:.0f}s/{HOLD_MAX}s — API chưa khoẻ, giữ phiên mở")
            page.wait_for_timeout(POLL * 1000)
            healthy = api_ok()
        if healthy:
            log(f"✅ stack ĐÃ KHOẾ sau {time.time()-t0:.0f}s — {API_URL}")
        else:
            log("⚠️ hết HOLD_MAX mà API chưa khoẻ — cron lần sau sẽ thử lại (boot.sh idempotent)")
        summary(["## molab keepalive",
                 f"- instantiate: {'✅' if ok else '⚠️'} ({how or '?'})",
                 f"- stack: {'🟢 khoẻ' if healthy else '🔴 chưa khoẻ sau HOLD_MAX'}",
                 f"- API: `{API_URL or '(chưa set)'}`"])
        browser.close()


if __name__ == "__main__":
    main()
