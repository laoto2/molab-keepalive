#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keepalive_job.py v3.5 — WAKE (giữ sandbox khỏi bị thu hồi) + HOLD (chờ stack sống) + báo động thật.

Lịch sử ngắn (mỗi bản là hệ quả của một phép đo THẬT):
  v2   · «success giả»: đọc cả CODE cell (`boot.sh` nằm trong editor) ⇒ tưởng cell đã chạy;
         mọi nhánh lỗi đều `exit 0` ⇒ tab Actions toàn ✅ mà stack vẫn chết.
  v3   · chỉ đọc vùng OUTPUT (bỏ `.cm-editor`) + snapshot before/after; exit 1 khi lỗi thật.
  v3.2 · network keepalive: khi giữ phiên, tự `fetch()` định kỳ tới chính trang notebook
         (tương đương giữ tab DevTools→Network mở) để platform thấy còn hoạt động.
  v3.3/v3.4 · thêm bước bấm «Run it now» (UI molab mới). **ĐO THẬT 30-10: không dùng được** —
         bấm nút/menu đó mở NOTEBOOK KHÁC trên một sandbox TẠM (`Server runs on a temporary
         cloud instance`): app-frame đổi thành `sb-<hash>-session.sb.molab.run/session/<jwt>`
         với `notebook_id` KHÁC ⇒ cell chạy trong container mới, không phải sandbox này.
         Kiểm chứng: mtime các file mà cell ghi (`deps/gh_token.txt`, `keepalive_job_v3.py`)
         KHÔNG đổi sau 2 lần bấm; `/marimo/logs/boot.log` cũng không có lần boot nào.
  v3.5 · bỏ hẳn phần "tự chạy cell" vì từ ngoài KHÔNG thể (browser chỉ có Preview / Server-tạm).
         Vai trò thật của job:
           ① giữ sandbox khỏi bị thu hồi do idle — mở trang + giữ phiên + ping mạng định kỳ;
           ② HOLD: poll `MOLAB_API_URL` — khi người/agent bấm ▶ Run all (cell 🚑 tự gọi `boot.sh`)
              thì stack sống lại và job báo ✅ **thật**;
           ③ hết thời gian mà API vẫn chết ⇒ **exit 1 + Summary hướng dẫn cụ thể** (không im lặng).

Secrets: MOLAB_NOTEBOOK_URL (bắt buộc) · MOLAB_STATE (cookie: JSON hoặc base64) · MOLAB_API_URL
Env: WAKE_WAIT=90 · HOLD_MAX=1500 · POLL=30 · NET_PING_INTERVAL=15 · HEADLESS=1
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

NOTEBOOK_URL = os.environ.get("MOLAB_NOTEBOOK_URL", "").strip()
STATE_RAW = os.environ.get("MOLAB_STATE", "").strip()
API_URL = os.environ.get("MOLAB_API_URL", "").strip().rstrip("/")
WAKE_WAIT = int(os.environ.get("WAKE_WAIT", "90"))
HOLD_MAX = int(os.environ.get("HOLD_MAX", "1500"))
POLL = int(os.environ.get("POLL", "30"))
NET_PING_INTERVAL = int(os.environ.get("NET_PING_INTERVAL", "15"))
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
    thường kèm cookie phụ ngắn hạn (csrf/consent) hết trong vài giờ, hết riêng là bình thường.
    """
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
    """True nếu gateway công khai trả lời (401/403 = khoẻ: đang bắt buộc key)."""
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


def anonymous(page, app, n_btn):
    """Ẩn danh / chưa đăng nhập ⇒ trang read-only, cookie hỏng."""
    txt = ""
    try:
        txt = page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass
    return n_btn == 0 and any(s in txt for s in ("Sign In", "Sign in", "Log in", "Đăng nhập"))


def network_ping(page, n):
    """Mô phỏng việc mở DevTools → tab Network: phát một request nhẹ tới chính sandbox để
    platform thấy còn hoạt động mạng (chống idle-timeout / thu hồi sandbox)."""
    try:
        res = page.evaluate(
            """async (u) => {
                const t0 = Date.now();
                try {
                    const r = await fetch(u, {cache: 'no-store', credentials: 'include', redirect: 'follow'});
                    const b = await r.text().catch(() => '');
                    return {ok: r.ok, status: r.status, bytes: b.length, ms: Date.now() - t0};
                } catch (e) {
                    return {ok: false, err: String(e).slice(0, 100), ms: Date.now() - t0};
                }
            }""",
            NOTEBOOK_URL,
        )
        log(f"[net] ping #{n}: status={res.get('status')} bytes={res.get('bytes')} {res.get('ms')}ms")
    except Exception as e:
        log(f"[net] ping #{n} lỗi: {type(e).__name__} {str(e)[:100]}")


def hold_with_net(page, seconds, poll=15):
    """Giữ phiên + phát request mạng định kỳ (thay wait_for_timeout thụ động).
    Trả về số ping đã gửi — như bảng Network của DevTools đếm request."""
    t0 = time.time()
    i = 0
    while time.time() - t0 < seconds:
        page.wait_for_timeout(poll * 1000)
        i += 1
        network_ping(page, i)
    return i


MANUAL_FIX = (
    "**Cách khôi phục (1 phút):** mở notebook → bấm ▶ **Run all** (hoặc `Ctrl+Shift+R`) — "
    "cell 🚑 *boot* tự gọi `boot.sh` ở nền (~1-4 phút khi build+model đã có, ~13 phút nếu "
    "relaunch xoá cả build/model) → 4 tầng + embeddings + keepalive sống lại. "
    "Trong sandbox có thể chạy trực tiếp: `bash /marimo/boot.sh`. "
    "Chi tiết: `/marimo/BOOT.md` · `SETUP-qwen38-api.md` §0.1."
)


def main():
    if not NOTEBOOK_URL:
        sys.exit("❌ thiếu secret MOLAB_NOTEBOOK_URL (trang notebook trên molab)")
    sf = state_file()
    log("notebook:", NOTEBOOK_URL)
    log("auth:", "có cookie" if sf else "KHÔNG (ẩn danh) — sẽ KHÔNG dùng được phiên thật")
    log("api verify:", API_URL or "(chưa set MOLAB_API_URL)")
    if sf:
        _rep = cookie_report(sf)
        if _rep:
            _mn, _mx, _mn_name, _mx_name = _rep
            if _mx < 3:
                log(f"⚠️ cookie sắp hết hạn — dài hạn nhất '{_mx_name}' còn {_mx:.1f} ngày → chạy lại capture_login.py!")
            else:
                log(f"cookie: ngắn nhất '{_mn_name}' {_mn:+.1f} ngày · dài nhất '{_mx_name}' {_mx:+.1f} ngày (OK)")

    healthy = api_ok()
    log("API:", "🟢 KHOẺ" if healthy else ("🔴 CHẾT" if healthy is False else "(không kiểm tra)"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(storage_state=sf or None)
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())
        page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(6000)
        app = find_app_frame(page)
        if app is None:
            log("⛔ KHÔNG tìm thấy app marimo — cookie chết (trang login) hoặc URL sai")
            summary([
                "## molab keepalive v3.5",
                "- ❌ Không tìm thấy app marimo (login page / URL sai).",
                "- Fix: chạy lại `capture_login.py` ở máy cá nhân → `gh secret set MOLAB_STATE …`",
            ])
            browser.close()
            sys.exit(1)

        n_btn = app.locator('[data-testid="run-button"]').count()
        if anonymous(page, app, n_btn):
            log("⛔ ẨN DANH (cookie thiếu/chết) — read-only preview, không dùng được phiên thật")
            summary([
                "## molab keepalive v3.5",
                "- ❌ Ẩn danh — cookie `MOLAB_STATE` thiếu hoặc đã hết hạn.",
                "- Fix: `python capture_login.py` (máy cá nhân) → `gh secret set MOLAB_STATE < <(base64 -w0 storage_state.json)`",
            ])
            browser.close()
            sys.exit(1)

        log(f"app-frame: {app.url[:110]} · run-buttons={n_btn} (0 = trang share ở chế độ chỉ đọc)")

        if healthy:
            log(f"🟢 stack KHOẺ sẵn — chỉ giữ phiên {WAKE_WAIT}s + ping mạng (chống thu hồi sandbox)")
            _n = hold_with_net(page, WAKE_WAIT, poll=NET_PING_INTERVAL)
            log(f"🟢 giữ phiên {WAKE_WAIT}s — {_n} request mạng (DevTools→Network)")
            summary([
                "## molab keepalive v3.5",
                "- 🟢 stack khoẻ — giữ phiên + network keepalive (chống idle-timeout).",
                f"- API: `{API_URL}` · {_n} request mạng trong {WAKE_WAIT}s.",
            ])
            browser.close()
            return

        # ── Stack chết: từ ngoài KHÔNG thể chạy cell (xem docstring v3.5) ──────────────────
        log("🔴 stack CHẾT. Job này KHÔNG tự chạy được cell trong sandbox (UI molab chỉ có "
            "Preview / Server-tạm ⇒ mở notebook khác trên container khác).")
        log("→ Giữ phiên + HOLD, chờ người/agent bấm ▶ Run all (cell 🚑 tự gọi boot.sh).")
        t0 = time.time()
        i = 0
        while not healthy and time.time() - t0 < HOLD_MAX:
            page.wait_for_timeout(POLL * 1000)
            i += 1
            network_ping(page, i)
            healthy = api_ok()
            log(f"… hold {time.time() - t0:.0f}s/{HOLD_MAX}s — API {'🟢 sống' if healthy else '🔴 chết'}")
        if healthy:
            log(f"✅ stack KHOẺ sau {time.time() - t0:.0f}s — {API_URL}")
            summary([
                "## molab keepalive v3.5",
                f"- ✅ Stack sống lại sau {time.time() - t0:.0f}s (ai đó đã bấm ▶ Run all / chạy `boot.sh`).",
                f"- API: `{API_URL}`",
            ])
            browser.close()
            return

        log("❌ hết HOLD mà API vẫn chết — cần người/agent khôi phục (Summary ghi cách làm)")
        summary([
            "## molab keepalive v3.5",
            f"- ❌ API `{API_URL}` vẫn chết sau {HOLD_MAX}s; job đã giữ phiên suốt thời gian đó "
            "(sandbox không bị thu hồi thêm).",
            "- ⚠️ Job **không thể tự chạy cell** trong sandbox: trang share của molab chỉ có "
            "**Preview** (output tĩnh) và **Server** («runs on a temporary cloud instance») — "
            "cả hai mở notebook khác trên container tạm (`sb-*-session`, `notebook_id` khác), "
            "đã đo thật 30-10 (mtime file các cell ghi không đổi, `boot.log` không có lần boot nào).",
            f"- {MANUAL_FIX}",
        ])
        browser.close()
        sys.exit(1)


if __name__ == "__main__":
    main()
