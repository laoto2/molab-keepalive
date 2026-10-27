# 🧪 GitHub Actions keep-alive cho sandbox molab (MIỄN PHÍ)

> Repo đang chạy: **https://github.com/laoto2/molab-keepalive** · xem log: tab **Actions** của repo.

Giữ phiên sandbox molab "có hoạt động" bằng cách mở trang notebook bằng Chromium headless
mỗi ~5 phút qua GitHub Actions (repo **public** → miễn phí unlimited minutes).

## Setup (10 phút)

1. Tạo **public** repo trên GitHub, đẩy lên: `keepalive_job.py` + `.github/workflows/keepalive.yml`
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `MOLAB_NOTEBOOK_URL`
   - Value: `https://molab.marimo.io/notebooks/nb_ZeKzrEpoxzKWR2GD4a54vA`
3. Tab **Actions** → workflow "molab keepalive" → **Run workflow** để test ngay.
   Log phải thấy `looks_like_marimo=True` (trang notebook tải thật, không phải trang chặn).
4. Để nó chạy. Cron 5 phút có thể trễ 5-15 phút — ổn với idle-timeout phổ biến 10-30 phút.

## Đánh giá sau 2-3 ngày

- Sandbox **không relaunch nữa** → nguyên nhân là idle-timeout → mục tiêu đạt ✅
- **Vẫn relaunch** → nền tảng recycle theo chu kỳ/hard-cap → tắt workflow;
  auto-recovery của notebook (~13 phút: cell 🚑 tự installer + boot.sh) vẫn là kế hoạch chính.

## Nếu GitHub Actions không đủ

| Phương án | Chi phí | Độ bền |
|---|---|---|
| HF Space Docker (bộ sẵn ở `deps/hf-space-keepalive/` — bị chặn bởi HF: Docker Space cần PRO) | HF **PRO** $9/th | browser mở LIÊN TỤC (websocket không ngắt) — bền nhất |
| VPS thường (`deps/molab_keepalive.py`) | $4-6/th | tương tự PRO Space |
| Oracle Cloud Always Free VPS | **$0** (cần thẻ khi đăng ký) | miễn phí trọn đời — chạy `molab_keepalive.py` liên tục |
