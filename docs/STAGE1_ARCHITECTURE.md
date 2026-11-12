# Tài Liệu Kiến Trúc Giai Đoạn 1 (Stage 1 Architecture) — AI Video Studio

> **Phạm vi Stage 1**: Thiết lập nền tảng Clean/Modular Architecture, cấu trúc thư mục, cấu hình môi trường, lược đồ cơ sở dữ liệu Supabase và đặc tả kiến trúc cho toàn bộ hệ thống. Chưa triển khai logic nghiệp vụ Stage 2/3/4.

---

## 1. Tổng Quan & Nguyên Tắc Kiến Trúc

**AI Video Studio** là nền tảng web kiến trúc modular (plugin-based) gồm hai pipeline sản xuất video AI chủ lực, vận hành theo giao diện **Dark Mode Studio** (tương tự CapCut Web / RunwayML):

1. **AI Video Translator & Auto-Dubber**: Tải/nhận video đơn lẻ hoặc hàng loạt (Batch URL/File), tách lời & nhạc nền, nhận diện giọng nói chi tiết từng từ (word-level STT), dịch thuật đồng bộ độ dài âm tiết theo ngữ cảnh, và lồng tiếng thích ứng (Adaptive TTS) hòa âm với nhạc nền.
2. **AI Faceless Video Generator**: Chuyển đổi chủ đề, kịch bản, hoặc URL bài báo thành video ngắn/dài hoàn chỉnh với Storyboard JSON (cảnh 3–5 giây), hình ảnh/video đa nguồn, phụ đề `.ass` Karaoke từng từ, hiệu ứng Ken Burns, audio ducking (`sidechaincompress`) và watermark theo gói tài khoản.

### 4 Nguyên Tắc Thiết Kế Cốt Lõi
- **Cloud-First AI Offloading**: Tác vụ nặng (Whisper STT, Gemini LLM, Replicate Demucs, Flux/SD, ElevenLabs/FPT/Viettel TTS) mặc định đẩy qua API đám mây để server backend nhẹ, khởi chạy nhanh trên CPU; giữ sẵn adapter dự phòng chạy local (`faster_whisper`, `local_demucs`).
- **Strict Provider & Factory Pattern**: Tách biệt hoàn toàn logic pipeline khỏi SDK của từng nhà cung cấp AI/Storage qua các Abstract Base Classes (ABCs). Chuyển đổi provider linh hoạt qua `.env` hoặc tùy chọn từ UI trên từng job.
- **Human-in-the-Loop (`AWAITING_REVIEW`)**: Tạm dừng pipeline trước bước render cuối để người dùng hiệu đính lời thoại, nghe thử TTS từng dòng, gán giọng theo nhân vật (`speaker_label`), hoặc thay thế visual từng cảnh mà không tốn chi phí chạy lại từ đầu.
- **Zero-Bloat Temp Lifecycle**: Mọi tệp trung gian nằm cô lập trong `temp/{job_id}/`, tự động giải phóng ngay sau khi upload kết quả hoặc qua Auto-Cleanup Worker sau 24 giờ.

---

## 2. Cấu Trúc Thư Mục (Clean / Modular Directory Structure)

### 2.1 Cây Thư Mục Tổng Thể

```text
du-an-moi/
├── .env.example                          # Mẫu biến môi trường (Server, Providers, Supabase, VietQR, FFmpeg)
├── .gitignore                            # Loại trừ .env, temp/*, __pycache__, .venv, media output (*.mp4, *.wav...)
├── README.md                             # Tổng quan dự án & hướng dẫn thiết lập Stage 1
├── requirements.txt                      # Danh sách thư viện Python (>= 3.10) theo hướng Cloud-First
├── docs/
│   └── STAGE1_ARCHITECTURE.md            # Tài liệu đặc tả kiến trúc Stage 1 (tài liệu này)
├── supabase/
│   └── schema.sql                        # Lược đồ PostgreSQL, Triggers, Atomic RPCs & RLS Policies
├── temp/                                 # Thư mục làm việc tạm thời cho từng job: temp/{job_id}/
│   └── .gitkeep
├── frontend/                             # Giao diện Dark Mode Studio (HTML5 + TailwindCSS + Vanilla ES6+ JS)
│   ├── .gitkeep                          # (index.html sẽ bổ sung ở Stage 4)
│   ├── assets/                           # Icon, font, watermark mẫu
│   │   └── .gitkeep
│   ├── css/                              # Custom studio styles & Tailwind tokens
│   │   └── .gitkeep
│   └── js/
│       ├── .gitkeep
│       ├── core/                         # [Stage 4] api.js, sse_client.js, state.js, auth.js
│       │   └── .gitkeep
│       ├── components/                   # [Stage 4] pipeline_stepper.js, review_editor.js, progress_console.js, vietqr_modal.js, batch_queue.js
│       │   └── .gitkeep
│       └── views/                        # [Stage 4] translator_view.js, faceless_view.js, dashboard_view.js, billing_view.js
│           └── .gitkeep
└── backend/                              # FastAPI Backend (Python >= 3.10, asyncio)
    ├── __init__.py                       # [Stage 2+] main.py (FastAPI app entrypoint & lifespan)
    ├── api/                              # Tầng HTTP & SSE Endpoints
    │   ├── __init__.py
    │   └── v1/                           # [Stage 2+] projects.py, translator.py, faceless.py, review.py, webhooks.py, credits.py, health.py
    │       └── __init__.py
    ├── core/                             # Cấu hình lõi, bảo mật JWT, logging, exceptions & SSE event bus
    │   └── __init__.py                   # [Stage 2+] config.py, security.py, logging.py, exceptions.py, sse.py, events.py
    ├── models/                           # Pydantic v2 Schemas, DTOs & State Enums
    │   └── __init__.py                   # [Stage 2+] enums.py, project.py, scene.py, payment.py, provider.py
    ├── providers/                        # Tầng Adapter AI & Storage (Strict Provider Pattern)
    │   ├── __init__.py                   # [Stage 2+] factory.py (ProviderFactory)
    │   ├── base/                         # [Stage 2] Abstract Base Classes (ABCs)
    │   │   └── __init__.py               # stt.py, tts.py, llm.py, visual.py, storage.py, separation.py
    │   ├── stt/                          # [Stage 2] groq_whisper.py, faster_whisper.py
    │   │   └── __init__.py
    │   ├── tts/                          # [Stage 2] edge_tts.py, elevenlabs.py, fpt_ai.py, viettel_ai.py
    │   │   └── __init__.py
    │   ├── llm/                          # [Stage 2] gemini.py
    │   │   └── __init__.py
    │   ├── visual/                       # [Stage 2] pexels.py, pixabay.py, flux_sd.py, custom_upload.py
    │   │   └── __init__.py
    │   ├── separation/                   # [Stage 2] replicate_demucs.py, fast_ffmpeg.py
    │   │   └── __init__.py
    │   └── storage/                      # [Stage 2] supabase_storage.py, local_storage.py
    │       └── __init__.py
    ├── services/                         # Tầng dịch vụ hạ tầng & miền nghiệp vụ dùng chung
    │   ├── __init__.py
    │   ├── media/                        # [Stage 3] downloader.py (yt-dlp), ffmpeg_engine.py, ass_karaoke.py, moviepy_composer.py, watermark.py
    │   │   └── __init__.py
    │   ├── scraper/                      # [Stage 3] article_scraper.py (trafilatura + beautifulsoup4)
    │   │   └── __init__.py
    │   ├── billing/                      # [Stage 3] credit_service.py, sepay_webhook.py, vietqr.py
    │   │   └── __init__.py
    │   └── supabase/                     # [Stage 2] client.py, repository.py
    │       └── __init__.py
    ├── pipelines/                        # Điều phối luồng xử lý 2 Pipeline chính
    │   ├── __init__.py
    │   ├── translator/                   # [Stage 3] orchestrator.py, stage1_separate.py, stage2_stt.py, stage3_translate.py, stage4_dub_mix.py
    │   │   └── __init__.py
    │   └── faceless/                     # [Stage 3] orchestrator.py, stage1_storyboard.py, stage2_visuals.py, stage3_captions_tts.py, stage4_assemble.py
    │       └── __init__.py
    ├── workers/                          # Background Task Queue & Auto-Cleanup Worker
    │   └── __init__.py                   # [Stage 2/3] task_queue.py, cleanup_worker.py
    └── tests/                            # Unit tests & Provider contract tests
        └── __init__.py
```

### 2.2 Trách Nhiệm Từng Module Chính

| Thư mục / Tầng | Trách nhiệm cốt lõi |
| :--- | :--- |
| `backend/api/v1/` | Tiếp nhận HTTP request, xác thực Supabase JWT, kiểm tra đầu vào qua Pydantic, kích hoạt pipeline bất đồng bộ và mở luồng SSE stream (`sse-starlette`). |
| `backend/core/` | Nạp cấu hình từ `.env` (`pydantic-settings`), quản lý SSE Event Bus theo `job_id`, xử lý lỗi chuẩn hóa và logging có cấu trúc. |
| `backend/models/` | Định nghĩa các Enum (`ProjectStatus`, `AspectRatio`, `VisualSource`), schema request/response cho API, cấu trúc dữ liệu `Scene`/`SubtitleSegment` và `WordTimestamp`. |
| `backend/providers/base/` | Khai báo các **Abstract Base Classes (ABC)** và kiểu dữ liệu chuẩn hóa vào/ra để mọi adapter tuân thủ một hợp đồng thống nhất. |
| `backend/providers/{domain}/` | Các cài đặt cụ thể cho từng nhà cung cấp (`groq`, `faster_whisper`, `edge_tts`, `elevenlabs`, `fpt_ai`, `viettel_ai`, `gemini`, `pexels`, `pixabay`, `flux`/`stable_diffusion`, `replicate`/`fast_mode`, `supabase`/`local`). |
| `backend/services/media/` | Engine xử lý media bằng `yt-dlp`, `FFmpeg` (ưu tiên `hwaccel` như `h264_nvenc`, fallback tự động về `libx264 -preset fast`), `MoviePy`, bộ sinh file `.ass` Karaoke word-level và bộ chèn Watermark. |
| `backend/services/scraper/` | Bóc tách nội dung sạch từ URL bài báo bằng `trafilatura` với fallback sang `beautifulsoup4` + `lxml`. |
| `backend/services/billing/` | Tính toán chi phí credit dự kiến/thực tế, tạo mã QR động chuẩn VietQR, xác thực webhook từ SePay/Casso (`/api/webhooks/payment`) và gọi RPC Supabase atomic. |
| `backend/services/supabase/` | Quản lý Supabase client (`service_role` cho backend worker, user-scoped token khi cần) và các thao tác CRUD với `projects`, `project_scenes`, `profiles`, `user_credits`. |
| `backend/pipelines/` | Chứa state machine điều phối từng bước của **Translator Pipeline** và **Faceless Pipeline**, hỗ trợ tạm dừng ở `AWAITING_REVIEW` và resume render cuối. |
| `backend/workers/` | Hàng đợi tác vụ giới hạn đồng thời (`MAX_CONCURRENT_JOBS`) và **Auto-Cleanup Worker** dọn dẹp `temp/{job_id}/`. |
| `frontend/` | SPA tĩnh Dark Mode Studio bằng HTML5 + TailwindCSS + Vanilla ES6+ JS, kết nối SSE để cập nhật thanh `%`, console log và giao diện review trực quan. |

---

## 3. Đặc Tả Strict Provider Pattern & Factory Architecture

Mọi kết nối tới AI bên thứ ba hoặc hệ thống lưu trữ **tuyệt đối không** được gọi trực tiếp trong pipeline hay API route. Pipeline chỉ làm việc với interface trừu tượng tại `backend/providers/base/`.

### 3.1 Danh Sách Các Interface Cốt Lõi (ABCs)

1. **`BaseSTTProvider`** (`backend/providers/base/stt.py`)
   - **Contract**: `async transcribe(audio_path: Path, language: str | None = None, word_timestamps: bool = True) -> TranscriptResult`
   - **Đầu ra chuẩn hóa**: Danh sách segment kèm `start_ms`, `end_ms`, `text`, `speaker_label` (nếu có) và mảng `words: list[WordTimestamp]` (`word`, `start_ms`, `end_ms`, `confidence`) để phục vụ `.ass` Karaoke và đồng bộ dịch thuật.
   - **Adapters**: `GroqWhisperProvider` (`STT_PROVIDER=groq`, model `whisper-large-v3`), `FasterWhisperProvider` (`STT_PROVIDER=faster_whisper`, optional local fallback).

2. **`BaseTTSProvider`** (`backend/providers/base/tts.py`)
   - **Contract**:
     - `async synthesize(text: str, voice_id: str, rate_pct: int = 0, output_path: Path = ...) -> AudioSynthesisResult`
     - `async list_voices(language: str | None = None) -> list[VoiceOption]`
   - **Hỗ trợ Adaptive Sync**: Nhận tham số `rate_pct` (`0` đến `+25`, giới hạn qua `EDGE_TTS_MAX_SPEED_PCT=25`) hoặc tính toán tốc độ phát để khớp `end_ms - start_ms` của đoạn gốc; trả về `duration_ms` thực tế của file audio sinh ra.
   - **Adapters**: `EdgeTTSProvider` (`edge_tts` — mặc định miễn phí), `ElevenLabsProvider` (`elevenlabs`), `FptAiTTSProvider` (`fpt_ai`), `ViettelAiTTSProvider` (`viettel_ai`).

3. **`BaseLLMProvider`** (`backend/providers/base/llm.py`)
   - **Contract**:
     - `async translate_time_synced(segments: list[SubtitleSegment], target_lang: str, style_tone: str) -> list[TranslatedSegment]`
     - `async generate_storyboard(source_text: str, aspect_ratio: AspectRatio, scene_duration_target: tuple[int, int] = (3, 5), tone: str = "viral") -> StoryboardResult`
   - **Yêu cầu**: Ép đầu ra Structured JSON (Pydantic validation + `tenacity` retry) đảm bảo độ dài âm tiết bản dịch tương thích khung thời gian gốc và storyboard chia cảnh chuẩn 3–5 giây.
   - **Adapters**: `GeminiProvider` (`LLM_PROVIDER=gemini`, model mặc định `gemini-2.5-flash`).

4. **`BaseVisualProvider`** (`backend/providers/base/visual.py`)
   - **Contract**: `async fetch_visual(query: str, prompt: str | None, aspect_ratio: AspectRatio, media_type: Literal["video", "image"] = "video") -> VisualAsset`
   - **Adapters**: `PexelsProvider` (`pexels`), `PixabayProvider` (`pixabay`), `FluxVisualProvider` (`flux`), `StableDiffusionProvider` (`stable_diffusion`), cùng cơ chế xử lý `custom_upload` khi người dùng tải ảnh/clip riêng ở bước Review.

5. **`BaseSeparationProvider`** (`backend/providers/base/separation.py`)
   - **Contract**: `async separate(media_path: Path, work_dir: Path) -> SeparationResult(vocals_path, bgm_path, mode)`
   - **Adapters**:
     - `FastFFmpegSeparationProvider` (`SEPARATION_PROVIDER=fast_mode`): Không tách Demucs, giữ luồng gốc và áp dụng suy giảm âm lượng (`SEPARATION_FAST_MODE_DUCK_DB=-14`) hoặc sidechain ducking khi trộn giọng lồng tiếng.
     - `ReplicateDemucsProvider` (`SEPARATION_PROVIDER=replicate`): Tách vocal/accompaniment chất lượng cao qua Replicate API.
     - `LocalDemucsProvider` (`SEPARATION_PROVIDER=local_demucs`): Fallback chạy local nếu cài optional PyTorch/Demucs.

6. **`BaseStorageProvider`** (`backend/providers/base/storage.py`)
   - **Contract**:
     - `async upload_file(local_path: Path, remote_key: str, content_type: str) -> StoredObject`
     - `async get_signed_url(remote_key: str, expires_in: int) -> str`
     - `async delete_object(remote_key: str) -> None`
   - **Adapters**: `SupabaseStorageProvider` (`STORAGE_PROVIDER=supabase`, bucket `video-studio-assets` phân vùng theo `{user_id}/{project_id}/...`), `LocalStorageProvider` (`local` cho dev offline).

### 3.2 Cơ Chế `ProviderFactory`

```text
[Job pipeline_config (UI override)] ──(ưu tiên cao hơn)──┐
                                                         ├──> ProviderFactory.get_<domain>(override) ──> Concrete Provider (ABC)
[.env Defaults (STT_PROVIDER, ...)] ──(fallback)─────────┘
```

- Mỗi domain có registry ánh xạ tên định danh (string key trong `.env` và `pipeline_config` JSONB) sang class Adapter tương ứng.
- Cho phép cấu hình mặc định cấp hệ thống qua `.env` nhưng **vẫn cho phép ghi đè linh hoạt theo từng project hoặc từng scene** (ví dụ: job dùng `edge_tts` mặc định, nhưng 1 scene cao cấp hoặc user Pro chọn `elevenlabs` / `fpt_ai` trong lúc Review).

---

## 4. Kiến Trúc Chi Tiết Hai Pipeline Lõi

### 4.1 Pipeline 1: AI Video Translator & Auto-Dubber

- **Đầu vào**:
  - Single hoặc Batch (tối đa `MAX_BATCH_SIZE=10`) từ URL (YouTube, TikTok, Douyin, Facebook...) qua `yt-dlp`, hoặc file tải lên trực tiếp (`MAX_UPLOAD_MB=500`).
  - Tham số cấu hình: ngôn ngữ đích, phong cách dịch (`chuyên nghiệp`, `hài hước`, `gen-z`, `kể chuyện`...), lựa chọn Separation/STT/TTS provider.
- **4 Giai Đoạn Xử Lý**:
  1. **Ingest & Vocal/BGM Separation**: `yt-dlp` chuẩn hóa video/audio về `temp/{job_id}/source.mp4` & `audio.wav` (16kHz mono cho STT + stereo gốc cho mix). Chạy `BaseSeparationProvider` (`fast_mode` hoặc `replicate` Demucs) tách `vocals.wav` và `bgm.wav`.
  2. **High-Accuracy Word-Level STT**: Gửi `vocals.wav` tới `BaseSTTProvider` (`groq` `whisper-large-v3` hoặc `faster_whisper`), lưu từng câu kèm `word_timestamps` JSONB vào bảng `project_scenes`.
  3. **Time-Synced Context Translation**: `BaseLLMProvider` (`gemini`) dịch theo ngữ cảnh toàn đoạn nhưng ràng buộc **độ dài âm tiết (syllable-length matching)** để lời dịch đọc vừa khung `start_ms`–`end_ms`.
     - ⏸ **Tạm dừng tại `AWAITING_REVIEW`**: Đẩy dữ liệu `project_scenes` lên UI để người dùng chỉnh chữ, đổi voice theo từng `speaker_label`, nghe thử TTS 1 dòng.
  4. **Adaptive TTS & BGM Mixing (Final Render)**:
     - Sinh audio từng câu bằng `BaseTTSProvider`. Nếu audio dài hơn khung gốc, tự động tăng tốc độ phát hợp lý (`+0%` đến `+25%`), nếu vượt ngưỡng thì xử lý time-stretch nhẹ hoặc nới khoảng lặng an toàn.
     - Trộn giọng lồng tiếng mới với `bgm.wav` bằng FFmpeg `sidechaincompress` (hoặc ducking theo `FFMPEG_DUCKING_*`), burn/mux phụ đề tùy chọn, chèn watermark nếu tài khoản là `free`, mã hóa qua `FFMPEG_VIDEO_CODEC` (tự động fallback `libx264 -preset fast`).

### 4.2 Pipeline 2: AI Faceless Video Generator

- **Đầu vào**: Chủ đề ngắn, kịch bản văn bản có sẵn, hoặc URL bài báo (scrape nội dung sạch qua `trafilatura` / `beautifulsoup4`). Tỷ lệ khung hình `9:16`, `16:9`, hoặc `1:1`.
- **4 Giai Đoạn Xử Lý**:
  1. **JSON Storyboard Generator**: `BaseLLMProvider` phân tích nội dung thành các scene dài **3–5 giây**, mỗi scene có: `narration_text`, `search_keywords`, `image_prompt`, `emotion_tone`, và nhịp chuyển cảnh.
  2. **Multi-Source Visual Engine**: Với mỗi scene, `BaseVisualProvider` tìm video/ảnh dọc-ngang phù hợp tỷ lệ (`pexels` / `pixabay`) hoặc tạo ảnh AI (`flux` / `stable_diffusion`) lưu tạm tại `temp/{job_id}/scenes/`.
     - ⏸ **Tạm dừng tại `AWAITING_REVIEW`**: Người dùng xem storyboard, sửa lời thoại, đổi provider hoặc upload ảnh/video riêng (`custom_upload`) cho bất kỳ scene nào mà không phải sinh lại toàn bộ storyboard.
  3. **TTS & Dynamic Viral `.ass` Karaoke Captions**:
     - Sinh voiceover theo từng scene (hoặc cả đoạn) qua `BaseTTSProvider`, chạy lại bước căn thời gian từ (word-level alignment/STT nhẹ nếu TTS không trả word timestamps) để tạo `word_timestamps`.
     - Bộ sinh `.ass` (`ass_karaoke.py`) tạo hiệu ứng Karaoke nổi bật từng từ đang đọc (`\k` / `\kf` highlight màu, font đậm, outline/shadow chuẩn video ngắn).
  4. **Smart Auto-Assembly & Mastering**:
     - `MoviePy` / `FFmpeg` ghép cảnh với hiệu ứng **Ken Burns** (zoom/pan mượt cho ảnh tĩnh), crossfade/wipe transition giữa các scene.
     - Trộn BGM với voiceover qua FFmpeg filter `sidechaincompress` (`threshold=0.03`, `ratio=6`, `attack=20`, `release=250`) để nhạc tự hạ khi có tiếng nói.
     - Chèn **Free-Tier Watermark** (text hoặc PNG overlay ở góc cấu hình `WATERMARK_POSITION`) nếu `profiles.subscription_tier == 'free'`, bỏ qua cho `pro` / `enterprise`.

---

## 5. Pipeline State Machine & Luồng `AWAITING_REVIEW`

Trạng thái của `projects.status` trong PostgreSQL tuân theo máy trạng thái hữu hạn (FSM) chặt chẽ:

```text
                    ┌──────────────────────────────────────────┐
                    ▼                                          │ (User Retry)
[PENDING] ──> [PROCESSING] ──> [AWAITING_REVIEW] ──> [RENDERING] ──> [COMPLETED]
   │               │                  │                   │
   │               ▼                  ▼                   ▼
   └──────────> [FAILED] <────────────┴───────────────────┘
   │               ▲
   ▼               │
[CANCELLED] ───────┘ (Cancel hợp lệ ở PENDING / PROCESSING / AWAITING_REVIEW -> hoàn credit chưa dùng)
```

### Chi Tiết Luồng Human-in-the-Loop (`AWAITING_REVIEW`)
1. Khi kết thúc Giai đoạn 3 (Translator) hoặc Giai đoạn 2 (Faceless), orchestrator cập nhật `projects.status = 'AWAITING_REVIEW'`, lưu toàn bộ `project_scenes` xuống Supabase và phát sự kiện SSE `awaiting_review`.
2. Trên UI Review Editor, người dùng có thể:
   - **Sửa nội dung** `edited_text` / `narration_text` và `start_ms` / `end_ms` của từng dòng/scene (`PATCH /api/v1/projects/{id}/scenes/{scene_id}`).
   - **Nghe thử TTS đơn lẻ** (`POST /api/v1/projects/{id}/scenes/{scene_id}/preview-tts`): gọi trực tiếp `BaseTTSProvider` cho duy nhất 1 dòng/scene, trả về audio stream hoặc file tạm ngắn để kiểm tra độ tự nhiên và độ khớp thời lượng trước khi render hàng loạt.
   - **Đổi giọng theo nhân vật (`speaker_label`)** hoặc từng scene (`voice_id`, `tts_rate_pct`).
   - **Thay thế visual từng scene** (tìm lại trên Pexels/Pixabay, sinh lại bằng Flux/SD, hoặc upload file `custom_upload` lên Supabase Storage / `temp/{job_id}/`).
   - **Khóa scene (`is_locked`)** để giữ nguyên khi tái tạo các scene khác.
3. Khi người dùng bấm **"Confirm & Render"**, API `POST /api/v1/projects/{id}/render` chuyển trạng thái sang `RENDERING`, trừ phần credit render (hoặc xác nhận quyết toán credit) và đẩy vào worker hoàn tất video cuối.

---

## 6. Real-Time Progress & SSE Event Flow

Hệ thống ưu tiên **Server-Sent Events (SSE)** qua `sse-starlette` (`GET /api/v1/projects/{project_id}/events`) vì nhẹ, tự động reconnect trên trình duyệt và phù hợp luồng dữ liệu một chiều từ server xuống Studio Console.

```text
[Pipeline Worker] ──emit(event)──> [In-Memory SSE EventBus (asyncio.Queue per project)] ──SSE──> [Frontend EventSource]
        │                                                                                               │
        └── cập nhật progress_pct & current_stage xuống bảng `projects` (phòng trường hợp F5 reload) ────┘
```

### Quy Ước Sự Kiện SSE Chuẩn
- `stage_started`: `{ "stage": "stt", "label": "Đang nhận diện giọng nói...", "progress_pct": 25 }`
- `progress`: `{ "stage": "tts", "progress_pct": 68, "completed_items": 12, "total_items": 18 }`
- `log`: `{ "level": "INFO", "timestamp": "...", "message": "[FFmpeg] Fallback sang libx264 preset fast" }`
- `awaiting_review`: `{ "project_id": "...", "progress_pct": 60, "scenes_count": 14 }`
- `completed`: `{ "project_id": "...", "progress_pct": 100, "output_video_url": "<signed_url>" }`
- `error`: `{ "project_id": "...", "code": "PROVIDER_TIMEOUT", "message": "...", "refunded": true }`

Khi người dùng tải lại trang (F5), frontend đọc trạng thái hiện tại qua `GET /api/v1/projects/{id}` rồi mới mở lại kết nối SSE để không mất ngữ cảnh.

---

## 7. Luồng Thanh Toán Tự Động (SePay / Casso VietQR) & Hệ Thống Credit

### 7.1 Luồng Nạp Tiền VietQR & Webhook (`/api/webhooks/payment`)

```text
[User mở Top-Up Modal]
   │  Chọn gói VND (VD: 50,000 VND = 500 Credits theo VND_PER_CREDIT=100)
   ▼
[Backend POST /api/v1/billing/topup-intent]
   │  Tạo bản ghi `payment_transactions` (status='pending', reference_code='AVS<SHORT_ID>')
   │  Trả về URL ảnh VietQR động (VIETQR_BANK_BIN, VIETQR_ACCOUNT_NO, amount, addInfo=reference_code)
   ▼
[User quét QR chuyển khoản ngân hàng]
   │
   ▼
[SePay / Casso phát hiện giao dịch -> POST /api/webhooks/payment]
   │  1. Xác thực Authorization/Webhook Secret (SEPAY_WEBHOOK_SECRET / CASSO_WEBHOOK_SECRET)
   │  2. Bóc tách `reference_code` từ nội dung chuyển khoản & lấy `external_tx_id`
   │  3. Gọi Supabase RPC `process_payment_webhook(...)` (SECURITY DEFINER):
   │     - Idempotency check theo `external_tx_id` (chống cộng trùng khi webhook retry)
   │     - Đối chiếu số tiền & cập nhật `payment_transactions.status = 'completed'`
   │     - Cộng `user_credits.balance` (atomic) + ghi log bất biến vào `credit_ledger`
   ▼
[Frontend nhận cập nhật số dư (SSE / polling nhẹ) & đóng modal]
```

### 7.2 Bảo Toàn & Khấu Trừ Credit
- **Chống gian lận phía Client**: RLS trên Supabase chỉ cho phép người dùng `SELECT` trên `user_credits`, `credit_ledger` và `payment_transactions`. Mọi thao tác cộng/trừ/hoàn credit chỉ đi qua RPC `SECURITY DEFINER` (`deduct_user_credits`, `refund_user_credits`, `process_payment_webhook`) sử dụng khóa hàng `SELECT ... FOR UPDATE` và ràng buộc `CHECK (balance >= 0)`.
- **Công thức tính linh hoạt**: Tính theo thời lượng video (`CREDITS_PER_SECOND_TRANSLATOR` / `CREDITS_PER_SECOND_FACELESS`) cộng phụ phí tùy chọn provider cao cấp (`CREDITS_SURCHARGE_SEPARATION`, `CREDITS_SURCHARGE_PREMIUM_TTS`, `CREDITS_SURCHARGE_AI_VISUAL`).
- **Hoàn tiền tự động**: Nếu pipeline gặp lỗi hệ thống ở trạng thái `FAILED` trước khi hoàn tất hoặc người dùng hủy hợp lệ, backend gọi `refund_user_credits` để hoàn credit và ghi nhận vào `credit_ledger`.

---

## 8. Vòng Đời Auto-Cleanup & Quản Lý `temp/{job_id}/`

Mọi pipeline chỉ ghi file làm việc (video gốc tải về, audio tách, ảnh/clip scene, file `.ass`, file render tạm) vào một thư mục duy nhất: `temp/{job_id}/`.

1. **Immediate Cleanup (`CLEANUP_AFTER_RENDER=true`)**:
   - Ngay sau khi video thành phẩm được upload lên Supabase Storage bucket `video-studio-assets` (đường dẫn `{user_id}/{project_id}/final.mp4`) và xác nhận lưu `output_video_url` vào bảng `projects`, worker chạy khối `finally` để xóa đệ quy `temp/{job_id}/` và ghi dấu `temp_cleaned_at`.
2. **Scheduled Janitor Worker (`AUTO_CLEANUP_ENABLED=true`)**:
   - Chạy nền trong FastAPI lifespan mỗi `AUTO_CLEANUP_INTERVAL_MINUTES=30` phút.
   - Quét toàn bộ thư mục con trong `TEMP_DIR` (`./temp`): nếu tuổi thư mục vượt quá `TEMP_TTL_HOURS=24` (do job bị crash, server restart giữa chừng, hoặc người dùng bỏ dở ở `AWAITING_REVIEW` quá lâu), worker tự động xóa thư mục đó và cập nhật `temp_cleaned_at` trên DB nếu job tương ứng chưa dọn.
   - Đồng thời, các tệp trên Supabase Storage có thể áp dụng chính sách hết hạn theo `expires_at` / signed URL TTL (`SUPABASE_SIGNED_URL_TTL_SECONDS=86400`) để kiểm soát dung lượng lưu trữ dài hạn.
