/**
 * Translator & Auto-Dubber Batch Studio View
 */

import { store, showToast } from '../core/state.js';
import { api } from '../core/api.js';
import { renderBgmMixer } from '../components/bgm_mixer.js';

const TARGET_LANGUAGES = [
    { value: 'vi', label: 'Tiếng Việt (vi)' },
    { value: 'en', label: 'English (en)' },
    { value: 'ja', label: '日本語 (ja)' },
    { value: 'ko', label: '한국어 (ko)' },
    { value: 'zh', label: '中文 (zh)' },
    { value: 'fr', label: 'Français (fr)' },
    { value: 'es', label: 'Español (es)' },
];

const TRANSLATION_STYLES = [
    { value: 'natural', label: 'Đời thường (Tự nhiên)' },
    { value: 'movie_review', label: 'Review phim' },
    { value: 'news', label: 'Tin tức / Bản tin' },
    { value: 'humorous', label: 'Hài hước / Bắt trend' },
    { value: 'technical', label: 'Chuyên ngành' },
];

const ASPECT_RATIOS = [
    { value: '9:16', label: '9:16 (TikTok / Reels / Shorts)' },
    { value: '16:9', label: '16:9 (YouTube Landscape)' },
    { value: '1:1', label: '1:1 (Square Social)' },
];

const SEPARATION_PROVIDERS = [
    { value: 'fast_mode', label: 'Fast Mode (Duck BGM nhanh)' },
    { value: 'replicate', label: 'Replicate Demucs (Cloud AI)' },
    { value: 'local_demucs', label: 'Local Demucs' },
];

const STT_PROVIDERS = [
    { value: 'groq', label: 'Groq Whisper Large v3' },
    { value: 'faster_whisper', label: 'Faster-Whisper (Local)' },
];

const LLM_PROVIDERS = [
    { value: 'gemini', label: 'Google Gemini' },
];

const TTS_PROVIDERS = [
    { value: 'edge_tts', label: 'Edge TTS (Miễn phí / Đa ngôn ngữ)' },
    { value: 'elevenlabs', label: 'ElevenLabs' },
    { value: 'fpt_ai', label: 'FPT.AI Voices' },
    { value: 'viettel_ai', label: 'Viettel AI Voices' },
];

const ALLOWED_EXTENSIONS = ['.mp4', '.mov', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac'];

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function deriveTitleFromUrl(url, index) {
    try {
        const parsed = new URL(url);
        const host = parsed.hostname.replace(/^www\./, '');
        const tail = parsed.pathname.split('/').filter(Boolean).pop() || '';
        const cleanTail = tail.slice(0, 24);
        return cleanTail ? `Dịch ${host} - ${cleanTail}` : `Dịch video ${host} #${index + 1}`;
    } catch {
        return `Translator Video #${index + 1}`;
    }
}

function deriveTitleFromFile(filename) {
    return filename.replace(/\.[^/.]+$/, '').slice(0, 120) || 'Uploaded Media';
}

function parseUrlLines(rawText) {
    return rawText
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);
}

function isValidHttpUrl(str) {
    try {
        const u = new URL(str);
        return u.protocol === 'http:' || u.protocol === 'https:';
    } catch {
        return false;
    }
}

export function renderTranslatorView(container, handlers = {}) {
    if (!container) return;

    const state = store.getState();
    const maxBatchSize = Number(state.publicConfig?.max_batch_size || 10);
    const maxUploadMb = Number(state.publicConfig?.max_upload_mb || 500);
    const defaults = state.providers?.defaults || {};
    const prefs = state.preferences || {};
    const prefLang = prefs.targetLang || 'vi';
    const prefStyle = (!prefs.translationStyle || prefs.translationStyle === 'casual') ? 'natural' : prefs.translationStyle;
    const prefAspect = prefs.aspectRatio || '9:16';
    const prefBurn = prefs.burnSubtitles !== undefined ? Boolean(prefs.burnSubtitles) : true;
    const ffmpegMissing = state.diagnostics?.tools?.ffmpeg?.available === false;
    const isSandboxMode = Boolean(state.publicConfig?.local_sandbox_mode || state.diagnostics?.providers?.local_sandbox_mode);
    const diagProviders = state.diagnostics?.providers || {};
    const showSandboxAiNotice = isSandboxMode && (!diagProviders.gemini || !diagProviders.groq);

    let inputTab = 'url'; // 'url' | 'upload'
    let isSubmitting = false;
    let isUploadingQueue = false;
    let activeUploadBatchPromise = null;
    const uploadedQueue = []; // { id, file, status: 'pending'|'uploading'|'done'|'error', progressPct, uploadedBytes, uploadStage, source_file_key, error, _uploadPromise }

    const defaultTts = defaults.tts || 'edge_tts';
    const defaultSep = defaults.separation || 'fast_mode';
    const defaultStt = defaults.stt || 'groq';
    const defaultLlm = defaults.llm || 'gemini';

    container.innerHTML = `
        <div class="space-y-6">
            ${ffmpegMissing ? `
                <div id="tr-ffmpeg-warning" class="rounded-xl bg-amber-500/10 border border-amber-500/35 px-4 py-3 flex items-center justify-between gap-3 text-xs text-amber-200">
                    <div class="flex items-center gap-2 min-w-0">
                        <span>⚠️</span>
                        <span>Chưa phát hiện <strong>FFmpeg</strong> trên máy chủ — quá trình tách âm/render video cần FFmpeg. <a href="#/settings" class="underline font-semibold text-amber-300 hover:text-white">Kiểm tra trong Hệ thống →</a></span>
                    </div>
                    <button type="button" data-dismiss-ffmpeg class="text-amber-300 hover:text-white px-1.5 py-0.5 shrink-0">✕</button>
                </div>
            ` : ''}
            ${showSandboxAiNotice ? `
                <div id="tr-sandbox-ai-notice" class="rounded-xl bg-cyan-500/10 border border-cyan-500/35 px-4 py-3 flex items-center justify-between gap-3 text-xs text-cyan-100">
                    <div class="flex items-center gap-2 min-w-0">
                        <span class="shrink-0">✨</span>
                        <span><strong>Chế độ Sandbox AI Offline đang bật:</strong> Bạn có thể bấm Tạo Video ngay để chạy thử toàn bộ quy trình (Storyboard / Chỉnh sửa Review / Lồng tiếng Edge-TTS / Xuất .MP4) mà chưa cần API key, hoặc vào <a href="#/settings" class="underline font-semibold text-cyan-300 hover:text-white">Cài đặt (#/settings)</a> để thêm Gemini / Groq / Pexels key.</span>
                    </div>
                    <button type="button" data-dismiss-sandbox-ai class="text-cyan-300 hover:text-white px-1.5 py-0.5 shrink-0">✕</button>
                </div>
            ` : ''}
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <div>
                    <div class="flex items-center gap-2">
                        <span class="px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">
                            Pipeline 1
                        </span>
                        <h2 class="text-xl font-bold text-white">AI Video Translator & Auto-Dubber</h2>
                    </div>
                    <p class="text-xs sm:text-sm text-slate-400 mt-1">
                        Dịch video đa ngôn ngữ theo lô (tối đa <strong class="text-cyan-300">${maxBatchSize}</strong> video/lần), giữ nhạc nền và đồng bộ giọng đọc AI.
                    </p>
                </div>
                <div class="flex items-center gap-2 self-start sm:self-auto">
                    <button id="tr-sample-btn" type="button"
                        class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-cyan-500/15 hover:bg-cyan-500/25 border border-cyan-500/35 text-cyan-300 transition">
                        ✨ Dùng mẫu thử nhanh
                    </button>
                    <button id="tr-back-dashboard" type="button"
                        class="px-3 py-1.5 rounded-lg text-xs font-medium bg-[#191F2E] hover:bg-[#222A3E] border border-[#262E42] text-slate-300 transition">
                        ← Quay lại Dashboard
                    </button>
                </div>
            </div>

            <form id="translator-studio-form" class="grid grid-cols-1 xl:grid-cols-12 gap-6">
                <!-- Left Column: Source Input (7 cols) -->
                <div class="xl:col-span-7 space-y-5">
                    <div class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-4">
                        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <h3 class="text-sm font-bold text-white">1. Nguồn Video / Audio</h3>
                            <div class="inline-flex p-1 rounded-xl bg-[#0B0D12] border border-[#262E42]">
                                <button type="button" data-input-tab="url"
                                    class="tr-tab-btn px-3 py-1.5 rounded-lg text-xs font-semibold transition bg-cyan-500 text-slate-950">
                                    🔗 Dán danh sách URL (Batch)
                                </button>
                                <button type="button" data-input-tab="upload"
                                    class="tr-tab-btn px-3 py-1.5 rounded-lg text-xs font-semibold transition text-slate-400 hover:text-white">
                                    📁 Upload nhiều File
                                </button>
                            </div>
                        </div>

                        <div>
                            <label for="tr-project-title" class="block text-xs font-medium text-slate-300 mb-1.5">
                                Tên dự án / Tên lô (Tùy chọn)
                            </label>
                            <input id="tr-project-title" type="text" maxlength="200"
                                placeholder="VD: Review phim hành động lô 01 (để trống sẽ tự đặt theo link/file)"
                                class="w-full px-3.5 py-2.5 rounded-xl bg-[#0B0D12] border border-[#262E42] text-sm text-white placeholder-slate-500 focus:outline-none focus:border-cyan-400" />
                        </div>

                        <!-- Tab 1: URL Batch -->
                        <div id="tr-panel-url" class="space-y-3">
                            <div class="flex items-center justify-between">
                                <label for="tr-urls-textarea" class="text-xs font-medium text-slate-300">
                                    Danh sách URL (TikTok, Douyin, YouTube Shorts, Instagram Reels) — mỗi dòng 1 URL
                                </label>
                                <span id="tr-url-count-badge" class="text-[11px] font-mono px-2 py-0.5 rounded bg-[#191F2E] text-slate-300 border border-[#262E42]">
                                    0 / ${maxBatchSize} URLs
                                </span>
                            </div>
                            <textarea id="tr-urls-textarea" rows="7"
                                placeholder="https://www.tiktok.com/@creator/video/123456789&#10;https://www.youtube.com/shorts/abcdef123&#10;https://v.douyin.com/xyz123/"
                                class="w-full px-3.5 py-3 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs sm:text-sm font-mono text-slate-100 placeholder-slate-600 focus:outline-none focus:border-cyan-400 leading-relaxed"></textarea>
                            <p id="tr-url-validation-msg" class="text-xs text-slate-400">
                                Hỗ trợ tạo 1 dự án đơn hoặc Batch nhiều URL chạy song song.
                            </p>
                        </div>

                        <!-- Tab 2: Multi-file Upload -->
                        <div id="tr-panel-upload" class="hidden space-y-3">
                            <div id="tr-dropzone"
                                class="rounded-xl border-2 border-dashed border-[#262E42] hover:border-cyan-400/70 bg-[#0B0D12]/80 p-6 text-center cursor-pointer transition">
                                <input id="tr-file-input" type="file" multiple accept=".mp4,.mov,.mkv,.webm,.mp3,.wav,.m4a,.aac,.ogg,.flac" class="hidden" />
                                <div class="text-3xl mb-2">📤</div>
                                <p class="text-sm font-semibold text-slate-200">
                                    Kéo thả hoặc bấm để chọn nhiều file Video / Audio
                                </p>
                                <p class="text-xs text-slate-400 mt-1">
                                    Định dạng: .mp4, .mov, .mkv, .mp3, .wav · Tối đa ${maxBatchSize} file · ≤ ${maxUploadMb}MB/file
                                </p>
                            </div>

                            <div class="flex items-center justify-between text-xs text-slate-400">
                                <span id="tr-upload-summary">Chưa có file nào được chọn</span>
                                <button id="tr-upload-all-btn" type="button"
                                    class="hidden px-3 py-1 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 border border-cyan-500/30 font-medium transition">
                                    ⬆️ Tải lên ngay
                                </button>
                            </div>

                            <div id="tr-file-queue-list" class="space-y-2 max-h-64 overflow-y-auto pr-1"></div>
                        </div>
                    </div>

                    <!-- Karaoke Style & Burn Subtitles -->
                    <div class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-4">
                        <div class="flex items-center justify-between">
                            <div>
                                <h3 class="text-sm font-bold text-white">2. Phụ đề & Karaoke (.ass)</h3>
                                <p class="text-xs text-slate-400">Tùy chỉnh giao diện chữ chạy highlight từng từ trên video</p>
                            </div>
                            <label class="inline-flex items-center gap-2 cursor-pointer">
                                <input id="tr-burn-subtitles" type="checkbox" ${prefBurn ? 'checked' : ''}
                                    class="w-4 h-4 rounded border-[#262E42] bg-[#0B0D12] text-cyan-500 focus:ring-cyan-500" />
                                <span class="text-xs font-medium text-slate-200">Ghi cứng phụ đề (Burn .ass)</span>
                            </label>
                        </div>

                        <div id="tr-sub-style-controls" class="grid grid-cols-2 sm:grid-cols-3 gap-3 ${prefBurn ? '' : 'opacity-45 pointer-events-none'}">
                            <div>
                                <label for="tr-sub-font" class="block text-[11px] text-slate-400 mb-1">Font chữ</label>
                                <select id="tr-sub-font"
                                    class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                    <option value="Arial">Arial</option>
                                    <option value="Montserrat">Montserrat</option>
                                    <option value="Roboto">Roboto</option>
                                    <option value="Be Vietnam Pro">Be Vietnam Pro</option>
                                    <option value="Impact">Impact</option>
                                </select>
                            </div>
                            <div>
                                <label for="tr-sub-size" class="block text-[11px] text-slate-400 mb-1">Cỡ chữ (px)</label>
                                <input id="tr-sub-size" type="number" min="16" max="140" value="56"
                                    class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400" />
                            </div>
                            <div>
                                <label for="tr-sub-pos" class="block text-[11px] text-slate-400 mb-1">Vị trí</label>
                                <select id="tr-sub-pos"
                                    class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                    <option value="bottom" selected>Dưới (Bottom)</option>
                                    <option value="middle">Giữa (Middle)</option>
                                    <option value="top">Trên (Top)</option>
                                </select>
                            </div>
                            <div>
                                <label for="tr-sub-primary" class="block text-[11px] text-slate-400 mb-1">Màu chữ gốc</label>
                                <div class="flex items-center gap-2 bg-[#0B0D12] border border-[#262E42] rounded-lg px-2 py-1.5">
                                    <input id="tr-sub-primary" type="color" value="#FFFFFF" class="w-6 h-6 rounded cursor-pointer bg-transparent border-0" />
                                    <span id="tr-sub-primary-hex" class="text-[11px] font-mono text-slate-300">#FFFFFF</span>
                                </div>
                            </div>
                            <div>
                                <label for="tr-sub-highlight" class="block text-[11px] text-slate-400 mb-1">Màu Highlight</label>
                                <div class="flex items-center gap-2 bg-[#0B0D12] border border-[#262E42] rounded-lg px-2 py-1.5">
                                    <input id="tr-sub-highlight" type="color" value="#FACC15" class="w-6 h-6 rounded cursor-pointer bg-transparent border-0" />
                                    <span id="tr-sub-highlight-hex" class="text-[11px] font-mono text-slate-300">#FACC15</span>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- BGM Mood Library & Auto-Ducking Mixer -->
                    <div id="tr-bgm-mixer-root"></div>
                </div>

                <!-- Right Column: Pipeline & Provider Config (5 cols) -->
                <div class="xl:col-span-5 space-y-5">
                    <div class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-4">
                        <h3 class="text-sm font-bold text-white">3. Cấu hình Dịch & Lồng tiếng</h3>

                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                                <label for="tr-target-lang" class="block text-xs font-medium text-slate-300 mb-1">Ngôn ngữ đích</label>
                                <select id="tr-target-lang"
                                    class="w-full px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs sm:text-sm text-white focus:outline-none focus:border-cyan-400">
                                    ${TARGET_LANGUAGES.map((l) => `<option value="${l.value}" ${l.value === prefLang ? 'selected' : ''}>${escapeHtml(l.label)}</option>`).join('')}
                                </select>
                            </div>
                            <div>
                                <label for="tr-style-tone" class="block text-xs font-medium text-slate-300 mb-1">Văn phong dịch</label>
                                <select id="tr-style-tone"
                                    class="w-full px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs sm:text-sm text-white focus:outline-none focus:border-cyan-400">
                                    ${TRANSLATION_STYLES.map((s) => `<option value="${s.value}" ${s.value === prefStyle ? 'selected' : ''}>${escapeHtml(s.label)}</option>`).join('')}
                                </select>
                            </div>
                        </div>

                        <div>
                            <label for="tr-aspect-ratio" class="block text-xs font-medium text-slate-300 mb-1">Tỷ lệ khung hình</label>
                            <select id="tr-aspect-ratio"
                                class="w-full px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs sm:text-sm text-white focus:outline-none focus:border-cyan-400">
                                ${ASPECT_RATIOS.map((ar) => `<option value="${ar.value}" ${ar.value === prefAspect ? 'selected' : ''}>${escapeHtml(ar.label)}</option>`).join('')}
                            </select>
                        </div>

                        <div class="pt-3 border-t border-[#262E42] space-y-3">
                            <div class="text-xs font-semibold text-slate-300 uppercase tracking-wider">
                                Provider Overrides (Plugin Factory)
                            </div>

                            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                <div>
                                    <label for="tr-sep-provider" class="block text-[11px] text-slate-400 mb-1">Tách Vocal / BGM</label>
                                    <select id="tr-sep-provider"
                                        class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                        ${SEPARATION_PROVIDERS.map((p) => `<option value="${p.value}" ${p.value === defaultSep ? 'selected' : ''}>${escapeHtml(p.label)}</option>`).join('')}
                                    </select>
                                </div>
                                <div>
                                    <label for="tr-stt-provider" class="block text-[11px] text-slate-400 mb-1">Nhận diện giọng (STT)</label>
                                    <select id="tr-stt-provider"
                                        class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                        ${STT_PROVIDERS.map((p) => `<option value="${p.value}" ${p.value === defaultStt ? 'selected' : ''}>${escapeHtml(p.label)}</option>`).join('')}
                                    </select>
                                </div>
                                <div>
                                    <label for="tr-llm-provider" class="block text-[11px] text-slate-400 mb-1">LLM Dịch thuật</label>
                                    <select id="tr-llm-provider"
                                        class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                        ${LLM_PROVIDERS.map((p) => `<option value="${p.value}" ${p.value === defaultLlm ? 'selected' : ''}>${escapeHtml(p.label)}</option>`).join('')}
                                    </select>
                                </div>
                                <div>
                                    <label for="tr-tts-provider" class="block text-[11px] text-slate-400 mb-1">Giọng đọc (TTS)</label>
                                    <select id="tr-tts-provider"
                                        class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                        ${TTS_PROVIDERS.map((p) => `<option value="${p.value}" ${p.value === defaultTts ? 'selected' : ''}>${escapeHtml(p.label)}</option>`).join('')}
                                    </select>
                                </div>
                            </div>

                            <div>
                                <div class="flex items-center justify-between mb-1">
                                    <label for="tr-voice-select" class="text-[11px] text-slate-400">Giọng đọc mặc định</label>
                                    <span id="tr-voice-loading" class="text-[11px] text-cyan-400 hidden">Đang tải giọng...</span>
                                </div>
                                <select id="tr-voice-select"
                                    class="w-full px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                    <option value="">Mặc định theo Provider</option>
                                </select>
                            </div>
                        </div>

                        <div class="pt-4 border-t border-[#262E42] space-y-3">
                            <div class="rounded-xl bg-[#191F2E]/70 border border-[#262E42] p-3 text-xs text-slate-300 space-y-1">
                                <div class="font-semibold text-cyan-300">✨ Quy trình Human-in-the-Loop</div>
                                <p class="text-slate-400 leading-relaxed">
                                    Pipeline sẽ dừng ở bước <code class="text-amber-300">AWAITING_REVIEW</code> sau khi dịch xong để bạn chỉnh sửa từng câu thoại, nghe thử giọng TTS và gán giọng theo từng nhân vật trước khi Render.
                                </p>
                            </div>

                            <button id="tr-submit-btn" type="submit"
                                class="w-full py-3 px-4 rounded-xl font-bold text-sm bg-gradient-to-r from-cyan-500 to-violet-500 hover:from-cyan-400 hover:to-violet-400 text-slate-950 shadow-lg shadow-cyan-500/20 transition flex items-center justify-center gap-2">
                                <span>🚀</span>
                                <span id="tr-submit-label">Bắt đầu Dịch & Auto-Dub</span>
                            </button>
                        </div>
                    </div>
                </div>
            </form>
        </div>
    `;

    const form = container.querySelector('#translator-studio-form');
    const backBtn = container.querySelector('#tr-back-dashboard');
    const tabBtns = container.querySelectorAll('.tr-tab-btn');
    const panelUrl = container.querySelector('#tr-panel-url');
    const panelUpload = container.querySelector('#tr-panel-upload');
    const urlsTextarea = container.querySelector('#tr-urls-textarea');
    const urlCountBadge = container.querySelector('#tr-url-count-badge');
    const urlValidationMsg = container.querySelector('#tr-url-validation-msg');
    const dropzone = container.querySelector('#tr-dropzone');
    const fileInput = container.querySelector('#tr-file-input');
    const fileQueueList = container.querySelector('#tr-file-queue-list');
    const uploadSummary = container.querySelector('#tr-upload-summary');
    const uploadAllBtn = container.querySelector('#tr-upload-all-btn');
    const burnToggle = container.querySelector('#tr-burn-subtitles');
    const subStyleControls = container.querySelector('#tr-sub-style-controls');
    const primaryColorInput = container.querySelector('#tr-sub-primary');
    const primaryHexLabel = container.querySelector('#tr-sub-primary-hex');
    const highlightColorInput = container.querySelector('#tr-sub-highlight');
    const highlightHexLabel = container.querySelector('#tr-sub-highlight-hex');
    const bgmMixerRoot = container.querySelector('#tr-bgm-mixer-root');
    const bgmMixer = renderBgmMixer(bgmMixerRoot, {
        bgm_volume: 0.25,
        auto_ducking: true,
    });
    const targetLangSelect = container.querySelector('#tr-target-lang');
    const ttsProviderSelect = container.querySelector('#tr-tts-provider');
    const voiceSelect = container.querySelector('#tr-voice-select');
    const voiceLoading = container.querySelector('#tr-voice-loading');
    const submitBtn = container.querySelector('#tr-submit-btn');
    const submitLabel = container.querySelector('#tr-submit-label');

    backBtn?.addEventListener('click', () => {
        if (handlers?.onNavigate) {
            handlers.onNavigate('dashboard');
        } else {
            document.querySelector('[data-nav="dashboard"]')?.click();
        }
    });

    container.querySelector('[data-dismiss-ffmpeg]')?.addEventListener('click', () => {
        container.querySelector('#tr-ffmpeg-warning')?.remove();
    });

    container.querySelector('[data-dismiss-sandbox-ai]')?.addEventListener('click', () => {
        container.querySelector('#tr-sandbox-ai-notice')?.remove();
    });

    container.querySelector('#tr-sample-btn')?.addEventListener('click', () => {
        const urlTabBtn = container.querySelector('[data-input-tab="url"]');
        urlTabBtn?.click();
        const titleInput = container.querySelector('#tr-project-title');
        if (titleInput) titleInput.value = 'Demo Dịch Video Ngắn AI (Mẫu thử)';
        urlsTextarea.value = 'https://www.youtube.com/shorts/dQw4w9WgXcQ';
        updateUrlCounter();
        showToast('Đã điền mẫu thử Translator nhanh!', 'info');
    });

    function updateUrlCounter() {
        const lines = parseUrlLines(urlsTextarea.value);
        const invalid = lines.filter((u) => !isValidHttpUrl(u));
        urlCountBadge.textContent = `${lines.length} / ${maxBatchSize} URLs`;

        if (lines.length > maxBatchSize) {
            urlCountBadge.className = 'text-[11px] font-mono px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/40';
            urlValidationMsg.textContent = `Vượt quá giới hạn MAX_BATCH_SIZE (${maxBatchSize} URL mỗi lần).`;
            urlValidationMsg.className = 'text-xs text-rose-400';
        } else if (invalid.length > 0) {
            urlCountBadge.className = 'text-[11px] font-mono px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40';
            urlValidationMsg.textContent = `Phát hiện ${invalid.length} dòng chưa phải URL hợp lệ (http/https).`;
            urlValidationMsg.className = 'text-xs text-amber-300';
        } else {
            urlCountBadge.className = 'text-[11px] font-mono px-2 py-0.5 rounded bg-[#191F2E] text-cyan-300 border border-[#262E42]';
            urlValidationMsg.textContent = lines.length > 1
                ? `Sẽ tạo Batch ${lines.length} dự án chạy song song.`
                : 'Hỗ trợ tạo 1 dự án đơn hoặc Batch nhiều URL chạy song song.';
            urlValidationMsg.className = 'text-xs text-slate-400';
        }
    }

    function renderFileQueue() {
        const doneCount = uploadedQueue.filter((item) => item.status === 'done').length;
        const uploadingCount = uploadedQueue.filter((item) => item.status === 'uploading').length;
        const errorCount = uploadedQueue.filter((item) => item.status === 'error').length;
        const pendingCount = uploadedQueue.filter((item) => item.status === 'pending').length;

        uploadSummary.textContent = uploadedQueue.length === 0
            ? 'Chưa có file nào được chọn'
            : `${uploadedQueue.length} / ${maxBatchSize} file · ${uploadingCount > 0 ? `${uploadingCount} đang tải · ` : ''}${doneCount} đã tải lên`;

        if (!isUploadingQueue && (errorCount > 0 || pendingCount > 0)) {
            uploadAllBtn.textContent = errorCount > 0 && pendingCount === 0 ? '🔄 Tải lại file lỗi' : '⬆️ Tải lên ngay';
            uploadAllBtn.disabled = false;
            uploadAllBtn.classList.remove('hidden');
        } else {
            uploadAllBtn.classList.add('hidden');
        }

        fileQueueList.innerHTML = uploadedQueue.map((item) => {
            const statusText = item.status === 'done'
                ? 'Đã tải lên · Sẵn sàng'
                : item.status === 'uploading' && item.uploadStage === 'processing'
                ? `Đang lưu trên máy chủ... ${item.progressPct || 96}%`
                : item.status === 'uploading' && item.uploadStage === 'starting'
                ? 'Đang khởi tạo tải lên...'
                : item.status === 'uploading'
                ? `Đang tải lên ${item.progressPct}% (${formatBytes(item.uploadedBytes || 0)} / ${formatBytes(item.file.size)})`
                : item.status === 'error'
                ? `Lỗi: ${item.error || 'Upload thất bại'}`
                : isUploadingQueue
                ? 'Chờ tới lượt tải lên...'
                : 'Chờ tải lên';
            const statusColor = item.status === 'done'
                ? 'text-emerald-400'
                : item.status === 'uploading'
                ? 'text-cyan-300'
                : item.status === 'error'
                ? 'text-rose-400'
                : 'text-slate-400';

            return `
                <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3 space-y-2">
                    <div class="flex items-center justify-between gap-2">
                        <div class="min-w-0">
                            <p class="text-xs font-medium text-white truncate">${escapeHtml(item.file.name)}</p>
                            <p class="text-[11px] text-slate-500">${formatBytes(item.file.size)} · <span class="${statusColor}">${escapeHtml(statusText)}</span></p>
                        </div>
                        <button type="button" data-remove-file="${escapeHtml(item.id)}"
                            class="px-2 py-1 rounded-md text-xs text-slate-400 hover:text-rose-400 hover:bg-rose-500/10 transition"
                            ${item.status === 'uploading' ? 'disabled' : ''}>
                            ✕
                        </button>
                    </div>
                    <div class="w-full h-1 rounded-full bg-[#191F2E] overflow-hidden">
                        <div class="h-full transition-all duration-300 ${item.status === 'error' ? 'bg-rose-500' : item.status === 'done' ? 'bg-emerald-500' : 'bg-cyan-400'}"
                            style="width: ${item.progressPct}%"></div>
                    </div>
                </div>
            `;
        }).join('');
    }

    async function uploadSingleQueueItem(item) {
        if (item.status === 'done' && item.source_file_key) return true;
        if (item._uploadPromise) return await item._uploadPromise;

        item.status = 'uploading';
        item.progressPct = 1;
        item.uploadedBytes = 0;
        item.uploadStage = 'starting';
        item.error = null;
        renderFileQueue();

        let gotRealProgress = false;
        let lastRenderAt = 0;
        const fallbackTimer = setInterval(() => {
            if (!gotRealProgress && item.status === 'uploading' && item.progressPct < 15) {
                item.progressPct = Math.min(15, item.progressPct + 3);
                renderFileQueue();
            }
        }, 350);

        const uploadPromise = (async () => {
            try {
                const res = await api.uploadSourceFile(item.file, {
                    onProgress: ({ loaded, total, percent, stage } = {}) => {
                        if (item.status !== 'uploading') return;
                        gotRealProgress = true;
                        const computedPct = typeof percent === 'number'
                            ? Math.max(1, Math.min(99, Math.round(percent)))
                            : total > 0 && typeof loaded === 'number'
                            ? Math.max(1, Math.min(95, Math.round((loaded / total) * 95)))
                            : item.progressPct;
                        const nextStage = stage || 'uploading';
                        const stageChanged = nextStage !== item.uploadStage;
                        const pctChanged = computedPct !== item.progressPct;
                        item.progressPct = computedPct;
                        if (typeof loaded === 'number') {
                            item.uploadedBytes = loaded;
                        }
                        item.uploadStage = nextStage;
                        const now = Date.now();
                        if (stageChanged || (pctChanged && now - lastRenderAt >= 120)) {
                            lastRenderAt = now;
                            renderFileQueue();
                        }
                    },
                });
                item.source_file_key = res.source_file_key;
                item.status = 'done';
                item.progressPct = 100;
                item.uploadedBytes = item.file.size;
                item.uploadStage = 'done';
                renderFileQueue();
                return true;
            } catch (err) {
                item.status = 'error';
                item.progressPct = 0;
                item.uploadStage = 'error';
                item.error = err?.message || 'Upload lỗi';
                renderFileQueue();
                return false;
            } finally {
                clearInterval(fallbackTimer);
                item._uploadPromise = null;
            }
        })();

        item._uploadPromise = uploadPromise;
        return await uploadPromise;
    }

    async function uploadPendingFiles() {
        if (activeUploadBatchPromise) {
            return await activeUploadBatchPromise;
        }
        const initialTargets = uploadedQueue.filter((item) => item.status !== 'done');
        if (initialTargets.length === 0) return true;
        if (!store.getState().accessToken) {
            showToast('Vui lòng đăng nhập trước khi upload file', 'warning');
            return false;
        }

        isUploadingQueue = true;
        renderFileQueue();

        activeUploadBatchPromise = (async () => {
            const attemptedIds = new Set();
            let allOk = true;
            try {
                while (true) {
                    const nextItem = uploadedQueue.find(
                        (item) =>
                            !attemptedIds.has(item.id) &&
                            (item.status === 'pending' || item.status === 'error' || item.status === 'uploading')
                    );
                    if (!nextItem) break;
                    attemptedIds.add(nextItem.id);
                    const ok = await uploadSingleQueueItem(nextItem);
                    if (!ok) allOk = false;
                }
                return allOk && uploadedQueue.every((item) => item.status === 'done');
            } finally {
                isUploadingQueue = false;
                activeUploadBatchPromise = null;
                renderFileQueue();
            }
        })();

        return await activeUploadBatchPromise;
    }

    function addFilesToQueue(fileList) {
        const incoming = Array.from(fileList || []);
        let addedCount = 0;
        for (const file of incoming) {
            if (uploadedQueue.length >= maxBatchSize) {
                showToast(`Tối đa ${maxBatchSize} file trong một lô`, 'warning');
                break;
            }
            const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
            if (!ALLOWED_EXTENSIONS.includes(ext)) {
                showToast(`Định dạng không hỗ trợ: ${file.name}`, 'error');
                continue;
            }
            if (file.size > maxUploadMb * 1024 * 1024) {
                showToast(`File ${file.name} vượt quá ${maxUploadMb}MB`, 'error');
                continue;
            }
            uploadedQueue.push({
                id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                file,
                status: 'pending',
                progressPct: 0,
                uploadedBytes: 0,
                uploadStage: 'pending',
                source_file_key: null,
                error: null,
                _uploadPromise: null,
            });
            addedCount += 1;
        }
        renderFileQueue();
        if (addedCount > 0) {
            if (store.getState().accessToken) {
                void uploadPendingFiles();
            } else {
                showToast('Vui lòng đăng nhập để tải file lên (hoặc đăng nhập rồi bấm Tải lên ngay)', 'warning');
            }
        }
    }

    async function loadVoicesForSelection() {
        const provider = ttsProviderSelect.value;
        const lang = targetLangSelect.value;
        const cacheKey = `${provider}:${lang}`;
        const cached = store.getState().ttsVoices?.[cacheKey];

        const populate = (voices) => {
            const prevValue = voiceSelect.value;
            const list = Array.isArray(voices) ? voices : [];
            voiceSelect.innerHTML = `<option value="">Mặc định theo ${escapeHtml(provider)}</option>` +
                list.map((v) => `
                    <option value="${escapeHtml(v.voice_id)}">
                        ${escapeHtml(v.name || v.voice_id)} (${escapeHtml(v.language || lang)}${v.gender ? ` · ${escapeHtml(v.gender)}` : ''})
                    </option>
                `).join('');
            if (prevValue && list.some((v) => v.voice_id === prevValue)) {
                voiceSelect.value = prevValue;
            }
        };

        if (cached) {
            populate(cached);
            return;
        }

        voiceLoading.classList.remove('hidden');
        try {
            const res = await api.getTtsVoices(provider, lang);
            const voices = Array.isArray(res?.voices) ? res.voices : [];
            store.setState({
                ttsVoices: {
                    ...(store.getState().ttsVoices || {}),
                    [cacheKey]: voices,
                },
            });
            populate(voices);
        } catch (err) {
            populate([]);
            console.warn('[translator_view] Failed to load voices:', err);
        } finally {
            voiceLoading.classList.add('hidden');
        }
    }

    tabBtns.forEach((btn) => {
        btn.addEventListener('click', () => {
            inputTab = btn.dataset.inputTab;
            tabBtns.forEach((b) => {
                const active = b.dataset.inputTab === inputTab;
                b.className = `tr-tab-btn px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                    active ? 'bg-cyan-500 text-slate-950' : 'text-slate-400 hover:text-white'
                }`;
            });
            panelUrl.classList.toggle('hidden', inputTab !== 'url');
            panelUpload.classList.toggle('hidden', inputTab !== 'upload');
        });
    });

    urlsTextarea?.addEventListener('input', updateUrlCounter);

    dropzone?.addEventListener('click', () => fileInput?.click());
    dropzone?.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('border-cyan-400', 'bg-cyan-500/5');
    });
    dropzone?.addEventListener('dragleave', () => {
        dropzone.classList.remove('border-cyan-400', 'bg-cyan-500/5');
    });
    dropzone?.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('border-cyan-400', 'bg-cyan-500/5');
        addFilesToQueue(e.dataTransfer?.files);
    });
    fileInput?.addEventListener('change', (e) => {
        addFilesToQueue(e.target.files);
        fileInput.value = '';
    });

    fileQueueList?.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-remove-file]');
        if (!btn) return;
        const idx = uploadedQueue.findIndex((i) => i.id === btn.dataset.removeFile);
        if (idx >= 0 && uploadedQueue[idx].status !== 'uploading') {
            uploadedQueue.splice(idx, 1);
            renderFileQueue();
        }
    });

    uploadAllBtn?.addEventListener('click', () => {
        if (!isUploadingQueue) {
            void uploadPendingFiles();
        }
    });

    burnToggle?.addEventListener('change', () => {
        const disabled = !burnToggle.checked;
        subStyleControls.classList.toggle('opacity-45', disabled);
        subStyleControls.classList.toggle('pointer-events-none', disabled);
    });

    primaryColorInput?.addEventListener('input', () => {
        primaryHexLabel.textContent = primaryColorInput.value.toUpperCase();
    });
    highlightColorInput?.addEventListener('input', () => {
        highlightHexLabel.textContent = highlightColorInput.value.toUpperCase();
    });

    ttsProviderSelect?.addEventListener('change', loadVoicesForSelection);
    targetLangSelect?.addEventListener('change', loadVoicesForSelection);

    form?.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (isSubmitting) return;

        if (!store.getState().accessToken) {
            showToast('Vui lòng đăng nhập trước khi tạo dự án', 'warning');
            document.getElementById('auth-action-btn')?.click();
            return;
        }

        const customTitle = container.querySelector('#tr-project-title').value.trim();
        const aspectRatio = container.querySelector('#tr-aspect-ratio').value;
        const burnSubs = Boolean(burnToggle.checked);

        const bgmConfig = bgmMixer.getConfig();
        const pipelineConfig = {
            separation_provider: container.querySelector('#tr-sep-provider').value || null,
            stt_provider: container.querySelector('#tr-stt-provider').value || null,
            llm_provider: container.querySelector('#tr-llm-provider').value || null,
            tts_provider: ttsProviderSelect.value || null,
            target_lang: targetLangSelect.value,
            translation_style: container.querySelector('#tr-style-tone').value,
            voice_id: voiceSelect.value || null,
            ...bgmConfig,
            burn_subtitles: burnSubs,
            subtitle_style: {
                font_name: container.querySelector('#tr-sub-font').value,
                font_size: Number(container.querySelector('#tr-sub-size').value) || 56,
                primary_color: primaryColorInput.value.toUpperCase(),
                highlight_color: highlightColorInput.value.toUpperCase(),
                outline_color: '#000000',
                outline_width: 3,
                position: container.querySelector('#tr-sub-pos').value,
                burn_subtitles: burnSubs,
            },
        };

        let items = [];
        isSubmitting = true;
        submitBtn.disabled = true;
        submitBtn.classList.add('opacity-70');

        try {
            if (inputTab === 'url') {
                const urls = parseUrlLines(urlsTextarea.value);
                if (urls.length === 0) {
                    showToast('Vui lòng dán ít nhất 1 URL video', 'warning');
                    return;
                }
                if (urls.length > maxBatchSize) {
                    showToast(`Số URL (${urls.length}) vượt quá MAX_BATCH_SIZE (${maxBatchSize})`, 'error');
                    return;
                }
                const invalid = urls.find((u) => !isValidHttpUrl(u));
                if (invalid) {
                    showToast(`URL không hợp lệ: ${invalid}`, 'error');
                    return;
                }

                items = urls.map((url, idx) => ({
                    mode: 'translator',
                    title: customTitle
                        ? (urls.length > 1 ? `${customTitle} #${idx + 1}` : customTitle)
                        : deriveTitleFromUrl(url, idx),
                    source_url: url,
                    aspect_ratio: aspectRatio,
                    pipeline_config: pipelineConfig,
                }));
            } else {
                if (uploadedQueue.length === 0) {
                    showToast('Vui lòng chọn ít nhất 1 file video/audio', 'warning');
                    return;
                }
                if (uploadedQueue.length > maxBatchSize) {
                    showToast(`Số file vượt quá MAX_BATCH_SIZE (${maxBatchSize})`, 'error');
                    return;
                }

                submitLabel.textContent = 'Đang upload file lên Storage...';
                const uploadOk = await uploadPendingFiles();
                if (!uploadOk) {
                    showToast('Một số file upload thất bại. Vui lòng kiểm tra lại.', 'error');
                    return;
                }

                items = uploadedQueue.map((item, idx) => ({
                    mode: 'translator',
                    title: customTitle
                        ? (uploadedQueue.length > 1 ? `${customTitle} #${idx + 1}` : customTitle)
                        : deriveTitleFromFile(item.file.name),
                    source_file_key: item.source_file_key,
                    aspect_ratio: aspectRatio,
                    pipeline_config: pipelineConfig,
                }));
            }

            submitLabel.textContent = 'Đang khởi tạo Pipeline...';

            let createdResult;
            if (items.length === 1) {
                createdResult = await api.createProject(items[0]);
                showToast(`Đã tạo dự án "${createdResult.title}"`, 'success');
            } else {
                createdResult = await api.createBatchProjects({
                    batch_title: customTitle || `Translator Batch (${items.length} videos)`,
                    items,
                });
                showToast(`Đã khởi chạy Batch ${createdResult.count} dự án`, 'success');
            }

            try {
                const updatedList = await api.listProjects({ limit: 50 });
                store.setState({ projects: Array.isArray(updatedList) ? updatedList : [] });
            } catch {
                // Non-fatal if list refresh fails
            }

            if (handlers?.onProjectCreated) {
                handlers.onProjectCreated(createdResult);
            }
        } catch (err) {
            showToast(err?.message || 'Không thể khởi tạo dự án Translator', 'error');
        } finally {
            isSubmitting = false;
            submitBtn.disabled = false;
            submitBtn.classList.remove('opacity-70');
            submitLabel.textContent = 'Bắt đầu Dịch & Auto-Dub';
        }
    });

    loadVoicesForSelection();
}
