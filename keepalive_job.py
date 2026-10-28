#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keepalive_job.py v3 — WAKE → RUN (verify THẬT bằng vùng output) → HOLD → fail thật.

Ba lỗi của v2 đã sửa (vì sao v2 báo SUCCESS mà cell vẫn không chạy):
  B1 · instantiated() dương giả: v2 đọc body.inner_text() tìm "boot.sh"/"keepalive.log"
       — nhưng CODE của cell (CodeMirror .cm-editor) cũng chứa đúng các chữ đó ⇒ v2
       luôn tưởng cell đã chạy ngay lập tức. v3 chỉ đọc text vùng OUTPUT (xoá toàn bộ
       .cm-editor khỏi DOM clone) và YÊU CẦU text mới xuất hiện so với snapshot
       chụp TRƯỚC khi bấm run (auto_instantiate=false ⇒ trước đó không có output).
  B2 · v2 exit 0 ở MỌI nhánh failure (ẩn danh/cookie chết, không thấy app-frame,
       bấm không ăn) ⇒ Actions hiện ✅ giả, không ai biết để sửa. v3 exit 1 ở mọi
       nhánh thất bại + GITHUB_STEP_SUMMARY ghi đúng chỗ cần sửa.
  B3 · v2 bấm 1 cách rồi chờ. v3 thử TUẦN TỰ palette → hotkey → nút run, sau MỖI
       cách chờ xem output có xuất hiện thật không; hết cả 3 cách mới kết luận lỗi.

Kết quả trung thực:
  exit 0  · stack khoẻ sẵn, hoặc đã bấm được run-all (output xuất hiện) và API khoẻ.
  exit 1  · không thấy app / ẩn danh / không chạy được cell / API chưa khoẻ sau HOLD.
  ⇒ Tab Actions giờ phản ánh đúng: ❌ = cần can thiệp (cookie, UI), ✅ = stack sống.

Secrets: MOLAB_NOTEBOOK_URL (bắt buộc) · MOLAB_STATE (cookie, JSON hoặc base64)
         MOLAB_API_URL (khuyến nghị — URL *.srv.us để verify stack hồi phục)
Env: WAKE_WAIT=90 · RUN_VERIFY=45 · HOLD_MAX=1500 · POLL=30 · HEADLESS=1
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

NOTEBOOK_URL = os.environ.get("MOLAB_NOTEBOOK_URL", "").strip()
STATE_RAW = os.environ.get("MOLAB_STATE", "").strip()
API_URL = os.environ.get("MOLAB_API_URL", "").strip().rstrip("/")
WAKE_WAIT = int(os.environ.get("WAKE_WAIT", "90"))
RUN_VERIFY = int(os.environ.get("RUN_VERIFY", "45"))
HOLD_MAX = int(os.environ.get("HOLD_MAX", "1500"))
POLL = int(os.environ.get("POLL", "30"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "")

# Marker CHỈ có trong OUTPUT sau khi cell chạy (trạng thái chưa chạy thì không có —
# vì auto_instantiate=false và các marker này nằm trong .cm-editor đã bị loại bỏ).
OUTPUT_MARKERS = (
    "Tự khôi phục",
    "Trạng thái",
    "llama-server (llama.cpp",
    "keepalive.log",
    "API công khai",
)


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
    """MOLAB_STATE: JSON gốc hoặc base64 — ghi /tmp rồi trả về đường dẫn."""
    if not STATE_RAW:
        return ""
    path = "/tmp/molab_state.json"
    if STATE_RAW.startswith("{"):
        open(path, "w").write(STATE_RAW)
    else:
        open(path, "wb").write(base64.b64decode(STATE_RAW))
    json.load(open(path))  # validate
    return path


def cookie_report(sf):
    """(min_days, max_days, tên cookie ngắn nhất, tên cookie dài nhất).

    Chỉ BÁO ĐỘNG khi cookie DÀI HẠN SÓNG NHẤT cũng sắp hết hạn (< 3 ngày) — storage_state
    thường kèm cookie phụ ngắn hạn (csrf/consent) hết trong vài giờ, hết riêng là bình thường."""
    try:
        data = json.load(open(sf))
        live = [
            (c.get("name", "?"), (c["expires"] - time.time()) / 86400)
            for c in data.get("cookies", [])
            if c.get("expires") and c["expires"] > 0
        ]
        if live:
            live.sort(key=lambda x: x[1])
            return live[0][1], live[-1][1], live[0][0], live[-1][0]
    except Exception:
        pass
    return None


def api_ok():
    """True nếu gateway công khai trả lời (401 = khoẻ: bắt buộc key)."""
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
            if "molab" in (f.url or "") or "marimo" in (f.url or "").lower():
                score += 2
        except Exception:
            pass
        if score > best_score:
            best, best_score = f, score
    return best if best_score > 0 else None


_OUTPUT_TEXT_JS = """
() => {
  const clone = document.body.cloneNode(true);
  clone.querySelectorAll('.cm-editor, .cm-content, .view-lines, .cm-line').forEach(e => e.remove());
  return (clone.innerText || '').slice(0, 20000);
}
"""


def output_text(app):
    """Text vùng OUTPUT — đã loại bỏ CODE (CodeMirror). Chốt chặn chống dương giả B1:
    chưa chạy (auto_instantiate=false) ⇒ text ngắn, không marker; cell chạy thật ⇒
    output render mới xuất hiện."""
    try:
        return app.evaluate(_OUTPUT_TEXT_JS) or ""
    except Exception:
        return ""


def outputs_appeared(app, before):
    after = output_text(app)
    grew = len(after) - len(before)
    new = [m for m in OUTPUT_MARKERS if m in after and m not in before]
    log(
        f"output-text: {len(before)}→{len(after)} ký tự ({'+' if grew >= 0 else ''}{grew})"
        f" · marker mới: {', '.join(new) if new else '—'}"
    )
    return (bool(new) or grew > 300), after


def open_editor(app, page):
    """Preview/Reader: 'Run or Edit' / 'Take over' để giành ghế editor."""
    for pat in ("take over", "run or edit", "open in editor", "switch to edit"):
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


def press_run_all(app, page, how):
    """Gửi lệnh run-all bằng 1 cách. Trả về True nếu lệnh đã gửi được."""
    try:
        if how == "palette":
            app.locator("body").click(timeout=3000)
            page.keyboard.press("Control+k")
            page.wait_for_timeout(700)
            page.keyboard.type("run all", delay=40)
            page.wait_for_timeout(700)
            item = app.get_by_text(re.compile("re-?run all cells", re.I)).first
            if item.count():
                item.click(timeout=3000)
            else:
                page.keyboard.press("Enter")
            log("run-all: command palette đã gửi")
            return True
        if how == "hotkey":
            app.locator("body").click(timeout=3000)
            page.keyboard.press("Control+Shift+KeyR")
            log("run-all: hotkey Ctrl+Shift+R đã gửi")
            return True
        if how == "run-buttons":
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
            return clicked > 0
    except Exception as e:
        log(f"run-all[{how}] lỗi:", type(e).__name__, str(e)[:120])
    return False


def run_and_verify(app, page, before):
    """Thử TUẦN TỰ cả 3 cách (B3); sau mỗi cách chờ RUN_VERIFY giây — chỉ tính thành
    công khi output THẬT xuất hiện (chống dương giả B1)."""
    page.on("dialog", lambda d: d.accept())
    for how in ("palette", "hotkey", "run-buttons"):
        if not press_run_all(app, page, how):
            continue
        t0 = time.time()
        while time.time() - t0 < RUN_VERIFY:
            page.wait_for_timeout(4000)
            seen, _after = outputs_appeared(app, before)
            if seen:
                log(f"✅ cách '{how}' — cell THẬT SỰ chạy (output xuất hiện sau {time.time() - t0:.0f}s)")
                return how
        log(f"⚠️ cách '{how}' không thấy output sau {RUN_VERIFY}s — thử cách kế…")
    return ""


def main():
    if not NOTEBOOK_URL:
        sys.exit("❌ thiếu secret MOLAB_NOTEBOOK_URL (trang notebook trên molab)")
    sf = state_file()
    log("notebook:", NOTEBOOK_URL)
    log("auth:", "có cookie" if sf else "KHÔNG (ẩn danh) — sẽ KHÔNG chạy được cell")
    log("api verify:", API_URL or "(chưa set MOLAB_API_URL)")
    if sf:
        _rep = cookie_report(sf)
        if _rep:
            _mn, _mx, _mn_name, _mx_name = _rep
            if _mx < 3:
                log(f"⚠️ cookie sắp hết hạn — dài hạn nhất '{_mx_name}' còn {_mx:.1f} ngày → chạy lại capture_login.py!")
            else:
                log(f"cookie: ngắn nhất '{_mn_name}' {_mn:+.1f} ngày · dài nhất '{_mx_name}' {_mx:+.1f} ngày (OK)")

    healthy_start = api_ok()
    if healthy_start:
        log(f"🟢 stack KHOẺ sẵn (API {API_URL}) — chỉ giữ phiên {WAKE_WAIT}s")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            ctx = browser.new_context(storage_state=sf or None)
            page = ctx.new_page()
            page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(WAKE_WAIT * 1000)
            browser.close()
        summary(["## molab keepalive v3", "- 🟢 stack khoẻ — giữ phiên, không đụng gì.", f"- API: `{API_URL}`"])
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(storage_state=sf or None)
        page = ctx.new_page()
        page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        app = find_app_frame(page)
        if app is None:
            log("⛔ KHÔNG tìm thấy app marimo — cookie chết (trang login) hoặc URL sai")
            summary([
                "## molab keepalive v3",
                "- ❌ Không tìm thấy app marimo (login page / URL sai).",
                "- Fix: chạy lại `capture_login.py` ở máy cá nhân → `gh secret set MOLAB_STATE …`",
            ])
            browser.close()
            sys.exit(1)  # v2 ở đây exit 0 ⇒ success giả

        before = output_text(app)  # snapshot TRƯỚC khi bấm (chưa có output nào)
        n_btn = app.locator('[data-testid="run-button"]').count()
        body_txt = ""
        try:
            body_txt = page.locator("body").inner_text(timeout=5000)
        except Exception:
            pass
        anon = n_btn == 0 and any(s in body_txt for s in ("Sign In", "Sign in", "Log in", "Đăng nhập"))
        log(f"app-frame: {app.url[:90]} · run-buttons={n_btn} · anonymous={anon} · output-trước={len(before)} ký tự")

        if anon:
            log("⛔ ẨN DANH (cookie thiếu/chết) — read-only preview, KHÔNG chạy được cell")
            summary([
                "## molab keepalive v3",
                "- ❌ Ẩn danh — cookie `MOLAB_STATE` thiếu hoặc đã hết hạn.",
                "- Fix: `python capture_login.py` (máy cá nhân) → `gh secret set MOLAB_STATE < <(base64 -w0 storage_state.json)`",
            ])
            browser.close()
            sys.exit(1)  # v2 ở đây exit 0 ⇒ success giả

        if n_btn == 0:  # đã login nhưng Reader/preview → giành ghế editor
            if open_editor(app, page):
                page.wait_for_timeout(3000)
                app = find_app_frame(page) or app
                n_btn = app.locator('[data-testid="run-button"]').count()
                before = output_text(app)
                if n_btn == 0:
                    log("vẫn 0 run-button → reload 1 lần rồi quét lại")
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(8000)
                    app = find_app_frame(page) or app
                    n_btn = app.locator('[data-testid="run-button"]').count()
                    before = output_text(app)
            log(f"sau editor-step: run-buttons={n_btn}")
            if n_btn == 0:
                summary([
                    "## molab keepalive v3",
                    "- ❌ Đã login nhưng vẫn không có nút run (UI molab có thể đã đổi).",
                    "- Fix: chạy tay 1 lần với `HEADLESS: 0` để xem UI thật, cập nhật selector.",
                ])
                browser.close()
                sys.exit(1)

        how = run_and_verify(app, page, before)
        if not how:
            log("⛔ KHÔNG chạy được cell nào sau cả 3 cách — cần debug UI (HEADLESS=0)")
            summary([
                "## molab keepalive v3",
                "- ❌ Đã gửi run-all (palette/hotkey/nút) nhưng KHÔNG thấy output nào xuất hiện.",
                "- Cell KHÔNG chạy ⇒ stack sẽ không tự phục hồi. Cần debug UI molab hiện tại.",
            ])
            browser.close()
            sys.exit(1)  # v2 ở đây vẫn hold 25 phút rồi exit 0 ⇒ success giả

        # HOLD: giữ phiên (websocket = hoạt động) + chờ boot.sh dựng lại stack (~8-13 phút)
        t0 = time.time()
        healthy = api_ok()
        while not healthy and time.time() - t0 < HOLD_MAX:
            log(f"… hold {time.time() - t0:.0f}s/{HOLD_MAX}s — cell đã chạy, chờ boot dựng lại stack")
            page.wait_for_timeout(POLL * 1000)
            healthy = api_ok()
        if healthy:
            log(f"✅ stack KHOẺ sau {time.time() - t0:.0f}s — {API_URL}")
            summary([
                "## molab keepalive v3",
                f"- ✅ Cell đã chạy (cách: `{how}`) → stack khoẻ sau {time.time() - t0:.0f}s.",
                f"- API: `{API_URL}`",
            ])
            browser.close()
            return
        log("⚠️ cell ĐÃ chạy nhưng API chưa khoẻ sau HOLD_MAX — boot có thể vẫn đang chạy;")
        log("   cron lần sau sẽ verify lại (boot.sh idempotent).")
        summary([
            "## molab keepalive v3",
            f"- ⚠️ Cell đã chạy (`{how}`) nhưng API chưa khoẻ sau {HOLD_MAX}s.",
            "- Có thể boot chưa xong (~13 phút) — cron lần sau sẽ verify lại.",
        ])
        browser.close()
        sys.exit(1)  # trung thực: chưa khoẻ = chưa xong


if __name__ == "__main__":
    main()
