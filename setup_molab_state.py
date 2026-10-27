#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_molab_state.py — 1 file duy nhất: lấy cookie molab → verify → set secret GitHub.

CHẠY TRÊN MÁY CÁ NHÂN (cần màn hình để đăng nhập):

    python setup_molab_state.py            # toàn bộ: cài deps → đăng nhập → verify → set secret
    python setup_molab_state.py --check    # chỉ kiểm tra cookie đã lưu còn sống không
    python setup_molab_state.py --secret-only   # không mở browser — chỉ set secret từ file có sẵn

Script làm gì:
  1. Tự cài thiếu gì bổ sung nấy: playwright + chromium (+ pynacl để mã hoá secret).
  2. Mở browser THẬT (có cửa sổ) ở trang notebook molab → bạn đăng nhập (GitHub) →
     mở notebook → quay lại terminal nhấn Enter.
  3. Xuất cookie + localStorage của phiên đó ra storage_state.json  ← "cookie đăng nhập".
  4. Verify: mở browser headless với cookie vừa lưu → trang phải KHÔNG còn "Sign In".
  5. Đặt secret MOLAB_STATE (base64 của storage_state.json) + MOLAB_API_URL lên repo
     laoto2/molab-keepalive qua GitHub API.

Cấu hình bằng env (đều có default — KHÔNG cần truyền gì):
  NOTEBOOK_URL   trang notebook molab
  REPO           repo chứa workflow keepalive
  GITHUB_TOKEN   token có quyền admin repo (đã nhúng default bên dưới)
  API_URL        URL công khai *.srv.us (để job verify stack đã sống)
  STATE_FILE     nơi lưu storage_state.json (mặc định cùng thư mục script)

Cookie hết hạn (job log `⛔ CHẾ ĐỘ ẨN DANH`) → chạy lại script này là xong.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.request

# ----------------- cấu hình mặc định -----------------
NOTEBOOK_URL = os.environ.get("NOTEBOOK_URL",
    "https://molab.marimo.io/notebooks/nb_ZeKzrEpoxzKWR2GD4a54vA")
REPO = os.environ.get("REPO", "laoto2/molab-keepalive")
API_URL = os.environ.get("API_URL", "https://pdwy46bx22auqauf4hcyjyumfa.srv.us")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN",
    "")  # để trống — export GITHUB_TOKEN=… (hoặc sửa dòng này) trước khi chạy
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "storage_state.json"))

log = lambda *a: print("[setup]", *a, flush=True)


# ----------------- 1) deps -----------------
def ensure_deps():
    missing = []
    for mod in ("playwright", "nacl"):
        try:
            __import__(mod)
        except ImportError:
            missing.append({"playwright": "playwright", "nacl": "pynacl"}[mod])
    if missing:
        log("cài thiếu:", ", ".join(missing))
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    log("deps OK")


# ----------------- 2+3) đăng nhập & xuất cookie -----------------
def capture_state():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)   # cửa sổ thật để bạn đăng nhập
        ctx = browser.new_context()
        page = ctx.new_page()
        log("mở", NOTEBOOK_URL)
        page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=90000)
        input("\n▶ Trong cửa sổ vừa mở: ĐĂNG NHẬP molab (GitHub) → mở đúng notebook\n"
              "  → quay lại đây nhấn Enter để chụp cookie… ")
        ctx.storage_state(path=STATE_FILE)
        browser.close()
    json.load(open(STATE_FILE))   # validate
    log("✅ đã lưu cookie:", STATE_FILE)


# ----------------- 4) verify cookie -----------------
def verify_state():
    from playwright.sync_api import sync_playwright
    log("verify cookie (headless, đúng như GitHub Actions sẽ thấy)…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=STATE_FILE)
        page = ctx.new_page()
        page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        body = page.locator("body").inner_text(timeout=8000)
        anon = "Sign In" in body
        n_btn = 0
        for f in page.frames:
            try:
                n_btn += f.locator('[data-testid="run-button"]').count()
            except Exception:
                pass
        browser.close()
    if anon:
        print("❌ cookie KHÔNG đăng nhập được (vẫn thấy 'Sign In'). "
              "Chạy lại script và đăng nhập đầy đủ.")
        sys.exit(1)
    log(f"✅ cookie sống — anonymous=False, run-buttons={n_btn} (preview có thể 0 — job v2 tự bấm 'Run or Edit')")


# ----------------- 5) set secret GitHub -----------------
def gh_api(path, method="GET", body=None):
    req = urllib.request.Request(f"https://api.github.com{path}", method=method,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "User-Agent": "setup-molab-state",
                 "Accept": "application/vnd.github+json"},
        data=json.dumps(body).encode() if body else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def set_secret(name, raw: bytes):
    pk = gh_api(f"/repos/{REPO}/actions/secrets/public-key")
    from nacl import encoding, public as nacl_public
    key = nacl_public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
    sealed = nacl_public.SealedBox(key).encrypt(raw)
    gh_api(f"/repos/{REPO}/actions/secrets/{name}", method="PUT", body={
        "encrypted_value": base64.b64encode(sealed).decode(), "key_id": pk["key_id"]})
    log(f"✅ secret {name} đã đặt lên {REPO} ({len(raw):,} B)")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "--secret-only":
        if not os.path.exists(STATE_FILE):
            sys.exit(f"❌ không thấy {STATE_FILE} — chạy không cờ để đăng nhập lấy cookie trước")
        set_secret("MOLAB_STATE", open(STATE_FILE, "rb").read())
        set_secret("MOLAB_API_URL", API_URL.encode())
        return

    if mode == "--check":
        if not os.path.exists(STATE_FILE):
            sys.exit(f"❌ không thấy {STATE_FILE}")
        ensure_deps()
        verify_state()
        return

    ensure_deps()
    if os.path.exists(STATE_FILE):
        log("đã có", STATE_FILE, "— sẽ GHI ĐÈ sau khi bạn đăng nhập lại (Ctrl+C nếu muốn giữ)")
    capture_state()
    verify_state()
    set_secret("MOLAB_STATE", open(STATE_FILE, "rb").read())
    set_secret("MOLAB_API_URL", API_URL.encode())

    print("\n" + "=" * 62)
    print("✅ XONG — GitHub Actions giờ có cookie để tự phục hồi sau relaunch.")
    print("   Test: repo → Actions → 'molab keepalive' → Run workflow →")
    print("   log phải thấy: auth: có (MOLAB_STATE) · stack 🟢/instantiate ✅")
    print("=" * 62)


if __name__ == "__main__":
    main()
