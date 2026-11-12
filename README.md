# AI Video Studio

**AI Video Studio** là nền tảng web kiến trúc modular (plugin-based) hỗ trợ sản xuất và bản địa hóa video bằng AI theo mô hình **Cloud-First**, **Human-in-the-Loop (`AWAITING_REVIEW`)** và xử lý media với **FFmpeg**.

> **Trạng thái hiện tại: Stage 1 (Giai đoạn 1 — Foundation & Architecture)**
> Giai đoạn này thiết lập bộ khung Clean/Modular, cấu hình môi trường, phụ thuộc Python, lược đồ Supabase và tài liệu kiến trúc. Logic nghiệp vụ Stage 2/3/4 sẽ được triển khai ở các giai đoạn tiếp theo.

---

## Hai Pipeline Cốt Lõi

1. **AI Video Translator & Auto-Dubber**
   - Nhận URL đơn lẻ hoặc hàng loạt (Batch qua `yt-dlp`) và file tải lên.
   - Tách giọng nói / nhạc nền (`fast_mode` hoặc Replicate Demucs).
   - Nhận diện giọng nói chính xác từng từ (`groq` `whisper-large-v3` hoặc `faster_whisper`).
   - Dịch thuật theo ngữ cảnh & khớp độ dài âm tiết bằng LLM (`gemini`), tạm dừng ở `AWAITING_REVIEW` để hiệu đính / nghe thử từng dòng / đổi giọng theo nhân vật.
   - Lồng tiếng thích ứng (`edge_tts`, `elevenlabs`, `fpt_ai`, `viettel_ai`) với tốc độ `+0%..+25%` và trộn nhạc nền.

2. **AI Faceless Video Generator**
   - Đầu vào từ chủ đề, kịch bản hoặc URL bài báo (`trafilatura` / `beautifulsoup4`).
   - Sinh JSON Storyboard chia cảnh 3–5 giây qua LLM.
   - Tự động tìm/sinh hình ảnh & video (`pexels`, `pixabay`, `flux`, `stable_diffusion`, hoặc `custom_upload` khi review).
   - Sinh phụ đề Karaoke `.ass` nổi bật từng từ, ghép cảnh Ken Burns, audio ducking (`sidechaincompress`) và chèn watermark cho tài khoản `free`.

---

## Kiến Trúc & Công Nghệ

- **Backend**: Python >= 3.10, FastAPI (`asyncio`), SSE (`sse-starlette`), Strict Provider + Factory Pattern.
- **Media Engine**: `FFmpeg` (ưu tiên `hwaccel`, tự động fallback `libx264 -preset fast`), `MoviePy`, `yt-dlp`.
- **Frontend**: HTML5, TailwindCSS, Vanilla ES6+ JS (Dark Mode Studio).
- **Database, Auth & Storage**: Supabase (PostgreSQL + RLS + Atomic RPCs + Storage bucket `video-studio-assets`).
- **Billing**: Thanh toán tự động SePay / Casso qua VietQR (`/api/webhooks/payment`) & hệ thống Credit.

Chi tiết thiết kế xem tại: [docs/STAGE1_ARCHITECTURE.md](docs/STAGE1_ARCHITECTURE.md)

---

## Hướng Dẫn Thiết Lập Stage 1

### 1. Yêu cầu hệ thống
- **Python** `>= 3.10`
- **FFmpeg & FFprobe** đã cài đặt và nằm trong `PATH` (kiểm tra bằng `ffmpeg -version` và `ffprobe -version`).
- Tài khoản **Supabase** (PostgreSQL, Auth, Storage).

### 2. Tạo môi trường Python & cài dependencies
```bash
python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Cấu hình biến môi trường
Sao chép `.env.example` thành `.env` và điền thông tin API keys, Supabase và VietQR của bạn:
```bash
# Windows (PowerShell)
Copy-Item .env.example .env
# macOS / Linux
# cp .env.example .env
```

### 4. Khởi tạo Cơ sở dữ liệu Supabase
1. Mở **Supabase Dashboard -> SQL Editor**.
2. Dán và chạy toàn bộ nội dung tệp `supabase/schema.sql` để tạo bảng, trigger, atomic RPCs và chính sách RLS.
3. Kiểm tra Storage bucket `video-studio-assets` đã được cấu hình đúng với `SUPABASE_STORAGE_BUCKET` trong `.env`.

---

## Lộ Trình Triển Khai

- **Stage 1 (Hiện tại)**: Cấu trúc thư mục Clean/Modular, `.gitignore`, `.env.example`, `requirements.txt`, `supabase/schema.sql`, `docs/STAGE1_ARCHITECTURE.md`.
- **Stage 2**: Core config, Pydantic models, Provider ABCs & Factory, Supabase repository, Background task queue & Auto-Cleanup worker.
- **Stage 3**: Media Engine (`yt-dlp`, `FFmpeg`, `.ass` Karaoke, `MoviePy`), 2 Pipelines hoàn chỉnh, `AWAITING_REVIEW` endpoints, SePay/VietQR webhook.
- **Stage 4**: Dark Mode Studio UI (`frontend/`), SSE live progress console, Review Editor & VietQR Top-Up Modal.
