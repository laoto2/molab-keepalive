# 🧪 GitHub Actions keep-alive v2 cho sandbox molab (MIỄN PHÍ)

> Repo đang chạy: **https://github.com/laoto2/molab-keepalive** · log: tab **Actions**.

## Vì sao phải v2 (bài học 2026-10-27)

Relaunch xảy ra (molab recycle/hard-cap — không chặn được từ ngoài), và khi đó
**toàn bộ stack chết**. Bản v1 chỉ mở trang notebook 60 s rồi đóng — tưởng là
"có người dùng", nhưng:

1. **molab ép `auto_instantiate = false`** (env `_MARIMO_CONFIG_OVERLOAD_RUNTIME_AUTO_INSTANTIATE=false`)
   ⇒ mở trang KHÔNG chạy cell nào, dù có đăng nhập hay không.
2. **Visit ẩn danh = read-only preview tĩnh** — không có kernel session, không bấm được gì.
3. Không verify gì sau khi mở ⇒ relaunch xong stack chết nằm đó cho tới khi người dùng mở.

⇒ Kết quả thực tế trước v2: chỉ **URL + key** sống sót (nhờ SSH key + key nhúng trong
`scripts_installer.py`), còn stack thì không tự dựng lại.

## v2 làm gì

```
mở trang (có cookie nếu có) → tìm app-frame marimo
   → nếu ẩn danh: log hướng dẫn fix, giữ phiên ngắn, thoát
   → nếu khoẻ sẵn (API trả lời): giữ phiên ngắn WAKE_WAIT, thoát
   → nếu cần khôi phục: CHẠY TOÀN BỘ CELL bằng 3 cách dự phòng:
        (1) Ctrl+K → gõ "run all" → chọn "Re-run all cells"   (command palette)
        (2) Ctrl+Shift+R  ("Run all stale cells" — hotkey mặc định marimo)
        (3) bấm lần lượt mọi nút [data-testid="run-button"]
     cell 🚑 boot chạy → boot.sh tự dựng lại toàn bộ (idempotent, ~8-13 phút)
   → HOLD: giữ phiên mở (websocket = hoạt động) + poll MOLAB_API_URL mỗi 30 s
     tới khi gateway trả lời (401/200) hoặc hết HOLD_MAX (25 phút)
```

Cron 5 phút ⇒ **sau mỗi relaunch, stack tự sống lại trong ~5-15 phút mà không cần ai mở**.
Khi khoẻ sẵn, mỗi run chỉ tốn ~2 phút (mở trang + giữ ngắn + thoát) — repo public = miễn phí.

## Setup / nâng cấp từ v1 (10 phút)

1. Đẩy 3 file lên repo: `keepalive_job.py` (thay bản cũ) + `.github/workflows/keepalive.yml`
   (thay bản cũ) + `.github/workflows/rearm.yml` (giữ nếu đã có).
2. Secrets (Settings → Secrets and variables → Actions):
   - `MOLAB_NOTEBOOK_URL` — đã có từ v1 (trang notebook trên molab).
   - `MOLAB_API_URL` — **mới**: URL công khai `https://xxxx.srv.us` (xem `/marimo/deps/tunnel_url.txt`).
   - `MOLAB_STATE` — **mới, quan trọng nhất**: cookie đăng nhập. Trên máy cá nhân:

     ```bash
     pip install playwright && playwright install chromium
     NOTEBOOK_URL="https://molab.marimo.io/notebooks/nb_XXX" python3 capture_login.py
     # trình duyệt mở ra → đăng nhập molab → mở notebook → nhấn Enter ở terminal
     gh secret set MOLAB_STATE --repo laoto2/molab-keepalive \
        --body "$(base64 -w0 storage_state.json)"
     ```

     (không có `gh` CLI: vào web GitHub → Secrets → dãy base64 vào ô value).
     Cookie hết hạn ⇒ chạy lại capture_login + set secret (job sẽ log rõ
     `⛔ CHẾ ĐỘ ẨN DANH` khi cookie chết).
3. Tab **Actions** → "molab keepalive" → **Run workflow** test ngay. Log tốt phải có:
   - `auth: có (MOLAB_STATE)` + `run-buttons: N>0`
   - `run-all: …` + `instantiate: ✅`
   - `stack 🟢 khoẻ` trong summary.

## Đọc log khi có sự cố

| Log | Nghĩa | Xử lý |
|---|---|---|
| `⛔ CHẾ ĐỘ ẨN DANH` | thiếu/chết cookie | chạy lại `capture_login.py` → set `MOLAB_STATE` |
| `instantiate: ⚠️` | bấm Run all xong nhưng chưa thấy cell boot render | tăng `RUN_WAIT`; xem molab có đổi UI không |
| `chưa khoẻ sau HOLD_MAX` | boot dài hơn 25 phút hoặc boot lỗi | mở boot.log trong notebook; cron sau tự thử lại |
| `không tìm thấy app-frame` | molab đổi layout/URL | cập nhật `find_app_frame()` |

## Nếu GitHub Actions không đủ

| Phương án | Chi phí | Độ bền |
|---|---|---|
| VPS thường (`deps/molab_keepalive.py` v2 — cùng logic run-all + hold) | $4-6/th | browser mở liên tục |
| Oracle Cloud Always Free VPS | $0 | miễn phí trọn đời |
| HF Space Docker (`deps/hf-space-keepalive/`) | PRO $9/th | bị chặn bởi HF free (402) — đã thử |
