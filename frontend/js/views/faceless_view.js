/**
 * Faceless AI Video Generator Studio View
 */

import { store, showToast } from '../core/state.js';
import { api } from '../core/api.js';
import { renderBgmMixer } from '../components/bgm_mixer.js';

const ASPECT_RATIOS = [
    { value: '9:16', label: '9:16', sub: 'TikTok / Reels / Shorts' },
    { value: '16:9', label: '16:9', sub: 'YouTube Landscape' },
    { value: '1:1', label: '1:1', sub: 'Square Feed' },
];

const DURATION_PRESETS = [
    { value: '15', label: '15s' },
    { value: '30', label: '30s' },
    { value: '45', label: '45s' },
    { value: '60', label: '60s' },
    { value: 'custom', label: 'Tùy chỉnh' },
];

const TONE_OPTIONS = [
    { value: 'viral', label: 'Viral / Cuốn hút' },
    { value: 'storytelling', label: 'Kể chuyện (Storytelling)' },
    { value: 'documentary', label: 'Tài liệu (Documentary)' },
    { value: 'cinematic', label: 'Điện ảnh (Cinematic)' },
    { value: 'educational', label: 'Giáo dục / Kiến thức' },
];

const LANGUAGE_OPTIONS = [
    { value: 'vi', label: 'Tiếng Việt (vi)' },
    { value: 'en', label: 'English (en)' },
    { value: 'ja', label: '日本語 (ja)' },
    { value: 'ko', label: '한국어 (ko)' },
    { value: 'zh', label: '中文 (zh)' },
];

const LLM_PROVIDERS = [
    { value: 'gemini', label: 'Google Gemini' },
];

const VISUAL_PROVIDERS = [
    { value: 'pexels', label: 'Pexels Stock HD' },
    { value: 'pixabay', label: 'Pixabay Stock' },
    { value: 'flux', label: 'Flux AI (Tạo ảnh AI)' },
    { value: 'stable_diffusion', label: 'Stable Diffusion' },
];

const TTS_PROVIDERS = [
    { value: 'edge_tts', label: 'Edge TTS (Miễn phí)' },
    { value: 'elevenlabs', label: 'ElevenLabs' },
    { value: 'fpt_ai', label: 'FPT.AI' },
    { value: 'viettel_ai', label: 'Viettel AI' },
];

const PREVIEW_WORDS = ['Bí', 'mật', 'tạo', 'Video', 'Triệu', 'View', 'bằng', 'AI'];

let previewIntervalId = null;

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function isValidHttpUrl(str) {
    try {
        const u = new URL(str);
        return u.protocol === 'http:' || u.protocol === 'https:';
    } catch {
        return false;
    }
}

function deriveDefaultTitle(sourceMode, content) {
    const clean = (content || '').trim();
    if (!clean) return 'Untitled Faceless Video';
    if (sourceMode === 'article_url') {
        try {
            const parsed = new URL(clean);
            return `Faceless · ${parsed.hostname.replace(/^www\./, '')}`;
        } catch {
            return 'Faceless Article Video';
        }
    }
    const firstLine = clean.split(/\r?\n/)[0].trim();
    return firstLine.length > 56 ? `${firstLine.slice(0, 56)}...` : firstLine;
}

export function renderFacelessView(container, handlers = {}) {
    if (!container) return;

    if (previewIntervalId) {
        clearInterval(previewIntervalId);
        previewIntervalId = null;
    }

    const state = store.getState();
    const defaults = state.providers?.defaults || {};
    const prefs = state.preferences || {};
    const diagProviders = state.diagnostics?.providers || {};
    const hasStock = Boolean(diagProviders.pexels || diagProviders.pixabay);
    const defaultLlm = defaults.llm || 'gemini';
    const defaultVisual = (diagProviders.pixabay && !diagProviders.pexels)
        ? 'pixabay'
        : (defaults.visual || 'pexels');
    const defaultTts = defaults.tts || 'edge_tts';
    const prefLang = prefs.targetLang || 'vi';
    const prefBurn = prefs.burnSubtitles !== undefined ? Boolean(prefs.burnSubtitles) : true;
    const ffmpegMissing = state.diagnostics?.tools?.ffmpeg?.available === false;
    const isSandboxMode = Boolean(state.publicConfig?.local_sandbox_mode || state.diagnostics?.providers?.local_sandbox_mode);
    const showSandboxAiNotice = isSandboxMode && (!diagProviders.gemini || !hasStock);

    let sourceMode = 'topic'; // 'topic' | 'script' | 'article_url'
    let aspectRatio = prefs.aspectRatio || '9:16';
    let durationPreset = '30';
    let isSubmitting = false;
    let activeWordIdx = 2;

    container.innerHTML = `
        <div class="space-y-6">
            ${ffmpegMissing ? `
                <div id="fl-ffmpeg-warning" class="rounded-xl bg-amber-500/10 border border-amber-500/35 px-4 py-3 flex items-center justify-between gap-3 text-xs text-amber-200">
                    <div class="flex items-center gap-2 min-w-0">
                        <span>⚠️</span>
                        <span>Chưa phát hiện <strong>FFmpeg</strong> trên máy chủ — bước ghép video và đốt phụ đề cần FFmpeg. <a href="#/settings" class="underline font-semibold text-amber-300 hover:text-white">Kiểm tra trong Hệ thống →</a></span>
                    </div>
                    <button type="button" data-dismiss-ffmpeg class="text-amber-300 hover:text-white px-1.5 py-0.5 shrink-0">✕</button>
                </div>
            ` : ''}
            ${showSandboxAiNotice ? `
                <div id="fl-sandbox-ai-notice" class="rounded-xl bg-cyan-500/10 border border-cyan-500/35 px-4 py-3 flex items-center justify-between gap-3 text-xs text-cyan-100">
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
                        <span class="px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-violet-500/15 text-violet-300 border border-violet-500/30">
                            Pipeline 2
                        </span>
                        <h2 class="text-xl font-bold text-white">AI Faceless Video Generator</h2>
                    </div>
                    <p class="text-xs sm:text-sm text-slate-400 mt-1">
                        Tự động viết Storyboard từng cảnh 3–5s, tìm video stock / ảnh AI, lồng tiếng & dựng hiệu ứng Karaoke chuyên nghiệp.
                    </p>
                </div>
                <div class="flex items-center gap-2 self-start sm:self-auto">
                    <button id="fl-sample-btn" type="button"
                        class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-violet-500/15 hover:bg-violet-500/25 border border-violet-500/35 text-violet-300 transition">
                        ✨ Dùng mẫu thử nhanh
                    </button>
                    <button id="fl-back-dashboard" type="button"
                        class="px-3 py-1.5 rounded-lg text-xs font-medium bg-[#191F2E] hover:bg-[#222A3E] border border-[#262E42] text-slate-300 transition">
                        ← Quay lại Dashboard
                    </button>
                </div>
            </div>

            <form id="faceless-studio-form" class="grid grid-cols-1 xl:grid-cols-12 gap-6">
                <!-- Left Column: Source & Storyboard Settings (7 cols) -->
                <div class="xl:col-span-7 space-y-5">
                    <div class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-4">
                        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <h3 class="text-sm font-bold text-white">1. Nguồn ý tưởng đầu vào</h3>
                            <div class="inline-flex flex-wrap p-1 rounded-xl bg-[#0B0D12] border border-[#262E42] gap-1">
                                <button type="button" data-source-mode="topic"
                                    class="fl-mode-tab px-3 py-1.5 rounded-lg text-xs font-semibold transition bg-violet-500 text-white">
                                    💡 Từ Chủ Đề (Topic)
                                </button>
                                <button type="button" data-source-mode="script"
                                    class="fl-mode-tab px-3 py-1.5 rounded-lg text-xs font-semibold transition text-slate-400 hover:text-white">
                                    📝 Từ Kịch Bản (Script)
                                </button>
                                <button type="button" data-source-mode="article_url"
                                    class="fl-mode-tab px-3 py-1.5 rounded-lg text-xs font-semibold transition text-slate-400 hover:text-white">
                                    🌐 Từ Link Bài Báo
                                </button>
                            </div>
                        </div>

                        <div>
                            <label for="fl-title" class="block text-xs font-medium text-slate-300 mb-1.5">
                                Tên dự án (Tùy chọn)
                            </label>
                            <input id="fl-title" type="text" maxlength="200"
                                placeholder="Để trống để tự động đặt theo chủ đề hoặc link bài báo"
                                class="w-full px-3.5 py-2.5 rounded-xl bg-[#0B0D12] border border-[#262E42] text-sm text-white placeholder-slate-500 focus:outline-none focus:border-violet-400" />
                        </div>

                        <div id="fl-panel-topic" class="space-y-2">
                            <label for="fl-topic-input" class="block text-xs font-medium text-slate-300">
                                Chủ đề / Ý tưởng video
                            </label>
                            <textarea id="fl-topic-input" rows="4"
                                placeholder="VD: 5 thói quen buổi sáng giúp não bộ tỉnh táo và tăng gấp đôi hiệu suất làm việc..."
                                class="w-full px-3.5 py-3 rounded-xl bg-[#0B0D12] border border-[#262E42] text-sm text-white placeholder-slate-600 focus:outline-none focus:border-violet-400"></textarea>
                            <p class="text-[11px] text-slate-400">AI sẽ tự động mở rộng thành lời dẫn (narration) và chia cảnh 3–5 giây.</p>
                        </div>

                        <div id="fl-panel-script" class="hidden space-y-2">
                            <label for="fl-script-input" class="block text-xs font-medium text-slate-300">
                                Kịch bản chi tiết / Lời bình có sẵn
                            </label>
                            <textarea id="fl-script-input" rows="6"
                                placeholder="Dán toàn bộ kịch bản hoặc nội dung thuyết minh của bạn vào đây. AI sẽ chia nhịp Storyboard và khớp từ khóa hình ảnh cho từng cảnh..."
                                class="w-full px-3.5 py-3 rounded-xl bg-[#0B0D12] border border-[#262E42] text-sm text-white placeholder-slate-600 focus:outline-none focus:border-violet-400"></textarea>
                        </div>

                        <div id="fl-panel-article" class="hidden space-y-2">
                            <label for="fl-article-input" class="block text-xs font-medium text-slate-300">
                                Link bài báo / Blog (Article URL)
                            </label>
                            <input id="fl-article-input" type="url"
                                placeholder="https://vnexpress.net/..."
                                class="w-full px-3.5 py-2.5 rounded-xl bg-[#0B0D12] border border-[#262E42] text-sm font-mono text-white placeholder-slate-600 focus:outline-none focus:border-violet-400" />
                            <p class="text-[11px] text-slate-400">Hệ thống sẽ trích xuất nội dung bài viết, tóm tắt thành Storyboard ngắn gọn súc tích.</p>
                        </div>
                    </div>

                    <!-- Format, Duration, Tone & Assembly Effects -->
                    <div class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-5">
                        <h3 class="text-sm font-bold text-white">2. Định dạng, Thời lượng & Hiệu ứng dựng</h3>

                        <!-- Aspect Ratio Pills -->
                        <div>
                            <label class="block text-xs font-medium text-slate-300 mb-2">Tỷ lệ khung hình</label>
                            <div class="grid grid-cols-3 gap-2.5">
                                ${ASPECT_RATIOS.map((ar) => `
                                    <button type="button" data-aspect="${ar.value}"
                                        class="fl-aspect-btn rounded-xl border p-3 text-left transition ${
                                            ar.value === aspectRatio
                                                ? 'bg-violet-500/15 border-violet-400 text-white'
                                                : 'bg-[#0B0D12] border-[#262E42] text-slate-400 hover:border-slate-500'
                                        }">
                                        <div class="text-sm font-bold font-mono">${escapeHtml(ar.label)}</div>
                                        <div class="text-[11px] opacity-80 truncate">${escapeHtml(ar.sub)}</div>
                                    </button>
                                `).join('')}
                            </div>
                        </div>

                        <!-- Target Duration & Scene Range -->
                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                            <div>
                                <label class="block text-xs font-medium text-slate-300 mb-2">Thời lượng mục tiêu</label>
                                <div class="flex flex-wrap gap-1.5">
                                    ${DURATION_PRESETS.map((d) => `
                                        <button type="button" data-duration="${d.value}"
                                            class="fl-dur-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition ${
                                                d.value === durationPreset
                                                    ? 'bg-violet-500 text-white border-violet-400'
                                                    : 'bg-[#0B0D12] border-[#262E42] text-slate-300 hover:border-slate-500'
                                            }">
                                            ${escapeHtml(d.label)}
                                        </button>
                                    `).join('')}
                                </div>
                                <div id="fl-custom-dur-wrap" class="hidden mt-2">
                                    <input id="fl-custom-dur" type="number" min="5" max="600" value="90"
                                        placeholder="Số giây (5 - 600)"
                                        class="w-full px-3 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-violet-400" />
                                </div>
                            </div>

                            <div>
                                <label class="block text-xs font-medium text-slate-300 mb-2">Độ dài mỗi cảnh (Scene 3–5s)</label>
                                <div class="grid grid-cols-2 gap-2">
                                    <div class="flex items-center gap-2 bg-[#0B0D12] border border-[#262E42] rounded-lg px-2.5 py-1.5">
                                        <span class="text-[11px] text-slate-400">Min</span>
                                        <input id="fl-scene-min" type="number" min="1" max="15" value="3"
                                            class="w-full bg-transparent text-xs font-mono text-white focus:outline-none text-right" />
                                        <span class="text-[11px] text-slate-500">s</span>
                                    </div>
                                    <div class="flex items-center gap-2 bg-[#0B0D12] border border-[#262E42] rounded-lg px-2.5 py-1.5">
                                        <span class="text-[11px] text-slate-400">Max</span>
                                        <input id="fl-scene-max" type="number" min="2" max="30" value="5"
                                            class="w-full bg-transparent text-xs font-mono text-white focus:outline-none text-right" />
                                        <span class="text-[11px] text-slate-500">s</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                                <label for="fl-tone" class="block text-xs font-medium text-slate-300 mb-1">Tone / Phong cách kể</label>
                                <select id="fl-tone"
                                    class="w-full px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs sm:text-sm text-white focus:outline-none focus:border-violet-400">
                                    ${TONE_OPTIONS.map((t) => `<option value="${t.value}">${escapeHtml(t.label)}</option>`).join('')}
                                </select>
                            </div>
                            <div>
                                <label for="fl-target-lang" class="block text-xs font-medium text-slate-300 mb-1">Ngôn ngữ Video</label>
                                <select id="fl-target-lang"
                                    class="w-full px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs sm:text-sm text-white focus:outline-none focus:border-violet-400">
                                    ${LANGUAGE_OPTIONS.map((l) => `<option value="${l.value}" ${l.value === prefLang ? 'selected' : ''}>${escapeHtml(l.label)}</option>`).join('')}
                                </select>
                            </div>
                        </div>

                        <!-- Provider Overrides -->
                        <div class="pt-3 border-t border-[#262E42] space-y-3">
                            <div class="text-xs font-semibold text-slate-300 uppercase tracking-wider">
                                Provider Overrides
                            </div>
                            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                                <div>
                                    <label for="fl-llm-provider" class="block text-[11px] text-slate-400 mb-1">LLM Storyboard</label>
                                    <select id="fl-llm-provider"
                                        class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-violet-400">
                                        ${LLM_PROVIDERS.map((p) => `<option value="${p.value}" ${p.value === defaultLlm ? 'selected' : ''}>${escapeHtml(p.label)}</option>`).join('')}
                                    </select>
                                </div>
                                <div>
                                    <label for="fl-visual-provider" class="block text-[11px] text-slate-400 mb-1">Visual Engine</label>
                                    <select id="fl-visual-provider"
                                        class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-violet-400">
                                        ${VISUAL_PROVIDERS.map((p) => `<option value="${p.value}" ${p.value === defaultVisual ? 'selected' : ''}>${escapeHtml(p.label)}</option>`).join('')}
                                    </select>
                                </div>
                                <div>
                                    <label for="fl-tts-provider" class="block text-[11px] text-slate-400 mb-1">TTS Voice Engine</label>
                                    <select id="fl-tts-provider"
                                        class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-violet-400">
                                        ${TTS_PROVIDERS.map((p) => `<option value="${p.value}" ${p.value === defaultTts ? 'selected' : ''}>${escapeHtml(p.label)}</option>`).join('')}
                                    </select>
                                </div>
                            </div>

                            <div>
                                <div class="flex items-center justify-between mb-1">
                                    <label for="fl-voice-select" class="text-[11px] text-slate-400">Giọng đọc (Voice)</label>
                                    <span id="fl-voice-loading" class="text-[11px] text-violet-400 hidden">Đang tải giọng...</span>
                                </div>
                                <select id="fl-voice-select"
                                    class="w-full px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-violet-400">
                                    <option value="">Mặc định theo Provider</option>
                                </select>
                            </div>
                        </div>

                        <!-- Ken Burns Toggle -->
                        <div class="pt-2">
                            <label class="flex items-start gap-3 rounded-xl bg-[#0B0D12] border border-[#262E42] p-3 cursor-pointer hover:border-violet-500/40 transition">
                                <input id="fl-ken-burns" type="checkbox" checked
                                    class="mt-0.5 w-4 h-4 rounded border-[#262E42] bg-[#121620] text-violet-500 focus:ring-violet-500" />
                                <div>
                                    <div class="text-xs font-semibold text-white">Hiệu ứng Ken Burns</div>
                                    <div class="text-[11px] text-slate-400">Zoom / Pan mượt mà cho ảnh tĩnh</div>
                                </div>
                            </label>
                        </div>
                    </div>

                    <!-- BGM Mood Library & Auto-Ducking Mixer -->
                    <div id="fl-bgm-mixer-root"></div>
                </div>

                <!-- Right Column: .ass Karaoke Style Builder + Live Preview (5 cols) -->
                <div class="xl:col-span-5 space-y-5">
                    <div class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-4">
                        <div class="flex items-center justify-between">
                            <div>
                                <h3 class="text-sm font-bold text-white">3. .ass Karaoke Style Builder</h3>
                                <p class="text-[11px] text-slate-400">Xem trước phụ đề động trực tiếp</p>
                            </div>
                            <label class="inline-flex items-center gap-2 cursor-pointer">
                                <input id="fl-burn-subtitles" type="checkbox" ${prefBurn ? 'checked' : ''}
                                    class="w-4 h-4 rounded border-[#262E42] bg-[#0B0D12] text-violet-500 focus:ring-violet-500" />
                                <span class="text-xs font-medium text-slate-200">Bật phụ đề</span>
                            </label>
                        </div>

                        <!-- Live Visual Preview Stage -->
                        <div class="flex justify-center py-2">
                            <div id="fl-preview-stage"
                                class="karaoke-preview-stage w-52 aspect-[9/16] flex flex-col justify-end p-3 transition-all duration-300 shadow-inner">
                                <div class="absolute top-2.5 left-2.5 right-2.5 flex items-center justify-between text-[10px] font-mono text-slate-400/80">
                                    <span id="fl-preview-ratio-badge" class="px-1.5 py-0.5 rounded bg-black/50 border border-white/10">9:16</span>
                                    <span class="px-1.5 py-0.5 rounded bg-black/50 border border-white/10">LIVE .ASS</span>
                                </div>
                                <div id="fl-preview-caption-wrap" class="w-full flex justify-center text-center my-3">
                                    <div id="fl-preview-caption-line" class="karaoke-subtitle-line"></div>
                                </div>
                            </div>
                        </div>

                        <!-- Karaoke Style Controls -->
                        <div id="fl-karaoke-controls" class="grid grid-cols-2 gap-3">
                            <div>
                                <label for="fl-sub-font" class="block text-[11px] text-slate-400 mb-1">Font chữ</label>
                                <select id="fl-sub-font"
                                    class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-violet-400">
                                    <option value="Arial">Arial</option>
                                    <option value="Montserrat" selected>Montserrat</option>
                                    <option value="Roboto">Roboto</option>
                                    <option value="Be Vietnam Pro">Be Vietnam Pro</option>
                                    <option value="Impact">Impact</option>
                                </select>
                            </div>
                            <div>
                                <label for="fl-sub-pos" class="block text-[11px] text-slate-400 mb-1">Vị trí hiển thị</label>
                                <select id="fl-sub-pos"
                                    class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-violet-400">
                                    <option value="bottom" selected>Dưới (Bottom)</option>
                                    <option value="middle">Giữa (Middle)</option>
                                    <option value="top">Trên (Top)</option>
                                </select>
                            </div>

                            <div>
                                <label for="fl-sub-size" class="block text-[11px] text-slate-400 mb-1">
                                    Cỡ chữ: <strong id="fl-sub-size-val" class="text-violet-300">56px</strong>
                                </label>
                                <input id="fl-sub-size" type="range" min="24" max="110" value="56"
                                    class="w-full studio-range mt-1.5 cursor-pointer" />
                            </div>
                            <div>
                                <label for="fl-sub-outline-w" class="block text-[11px] text-slate-400 mb-1">
                                    Viền chữ: <strong id="fl-sub-outline-val" class="text-violet-300">3px</strong>
                                </label>
                                <input id="fl-sub-outline-w" type="range" min="0" max="8" value="3"
                                    class="w-full studio-range mt-1.5 cursor-pointer" />
                            </div>

                            <div>
                                <label for="fl-sub-primary" class="block text-[11px] text-slate-400 mb-1">Màu chữ gốc</label>
                                <div class="flex items-center gap-2 bg-[#0B0D12] border border-[#262E42] rounded-lg px-2 py-1.5">
                                    <input id="fl-sub-primary" type="color" value="#FFFFFF" class="w-6 h-6 rounded cursor-pointer bg-transparent border-0" />
                                    <span id="fl-sub-primary-hex" class="text-[11px] font-mono text-slate-300">#FFFFFF</span>
                                </div>
                            </div>
                            <div>
                                <label for="fl-sub-highlight" class="block text-[11px] text-slate-400 mb-1">Màu Highlight</label>
                                <div class="flex items-center gap-2 bg-[#0B0D12] border border-[#262E42] rounded-lg px-2 py-1.5">
                                    <input id="fl-sub-highlight" type="color" value="#FACC15" class="w-6 h-6 rounded cursor-pointer bg-transparent border-0" />
                                    <span id="fl-sub-highlight-hex" class="text-[11px] font-mono text-slate-300">#FACC15</span>
                                </div>
                            </div>

                            <div class="col-span-2">
                                <label for="fl-sub-outline-c" class="block text-[11px] text-slate-400 mb-1">Màu viền (Outline)</label>
                                <div class="flex items-center gap-2 bg-[#0B0D12] border border-[#262E42] rounded-lg px-2 py-1.5">
                                    <input id="fl-sub-outline-c" type="color" value="#000000" class="w-6 h-6 rounded cursor-pointer bg-transparent border-0" />
                                    <span id="fl-sub-outline-hex" class="text-[11px] font-mono text-slate-300">#000000</span>
                                </div>
                            </div>
                        </div>

                        <div class="pt-3 border-t border-[#262E42]">
                            <button id="fl-submit-btn" type="submit"
                                class="w-full py-3 px-4 rounded-xl font-bold text-sm bg-gradient-to-r from-violet-500 to-cyan-500 hover:from-violet-400 hover:to-cyan-400 text-white shadow-lg shadow-violet-500/25 transition flex items-center justify-center gap-2">
                                <span>✨</span>
                                <span id="fl-submit-label">Tạo Storyboard & Video Faceless</span>
                            </button>
                        </div>
                    </div>
                </div>
            </form>
        </div>
    `;

    const form = container.querySelector('#faceless-studio-form');
    const backBtn = container.querySelector('#fl-back-dashboard');
    const modeTabs = container.querySelectorAll('.fl-mode-tab');
    const panelTopic = container.querySelector('#fl-panel-topic');
    const panelScript = container.querySelector('#fl-panel-script');
    const panelArticle = container.querySelector('#fl-panel-article');
    const aspectBtns = container.querySelectorAll('.fl-aspect-btn');
    const durBtns = container.querySelectorAll('.fl-dur-btn');
    const customDurWrap = container.querySelector('#fl-custom-dur-wrap');
    const customDurInput = container.querySelector('#fl-custom-dur');
    const sceneMinInput = container.querySelector('#fl-scene-min');
    const sceneMaxInput = container.querySelector('#fl-scene-max');
    const targetLangSelect = container.querySelector('#fl-target-lang');
    const ttsProviderSelect = container.querySelector('#fl-tts-provider');
    const voiceSelect = container.querySelector('#fl-voice-select');
    const voiceLoading = container.querySelector('#fl-voice-loading');
    const bgmMixerRoot = container.querySelector('#fl-bgm-mixer-root');
    const bgmMixer = renderBgmMixer(bgmMixerRoot, {
        bgm_volume: 0.25,
        auto_ducking: true,
    });

    const burnSubtitlesInput = container.querySelector('#fl-burn-subtitles');
    const karaokeControls = container.querySelector('#fl-karaoke-controls');
    const previewStage = container.querySelector('#fl-preview-stage');
    const previewRatioBadge = container.querySelector('#fl-preview-ratio-badge');
    const previewCaptionWrap = container.querySelector('#fl-preview-caption-wrap');
    const previewCaptionLine = container.querySelector('#fl-preview-caption-line');

    const fontSelect = container.querySelector('#fl-sub-font');
    const posSelect = container.querySelector('#fl-sub-pos');
    const sizeRange = container.querySelector('#fl-sub-size');
    const sizeVal = container.querySelector('#fl-sub-size-val');
    const outlineWRange = container.querySelector('#fl-sub-outline-w');
    const outlineWVal = container.querySelector('#fl-sub-outline-val');
    const primaryInput = container.querySelector('#fl-sub-primary');
    const primaryHex = container.querySelector('#fl-sub-primary-hex');
    const highlightInput = container.querySelector('#fl-sub-highlight');
    const highlightHex = container.querySelector('#fl-sub-highlight-hex');
    const outlineCInput = container.querySelector('#fl-sub-outline-c');
    const outlineCHex = container.querySelector('#fl-sub-outline-hex');

    const submitBtn = container.querySelector('#fl-submit-btn');
    const submitLabel = container.querySelector('#fl-submit-label');

    backBtn?.addEventListener('click', () => {
        if (handlers?.onNavigate) {
            handlers.onNavigate('dashboard');
        } else {
            document.querySelector('[data-nav="dashboard"]')?.click();
        }
    });

    container.querySelector('[data-dismiss-ffmpeg]')?.addEventListener('click', () => {
        container.querySelector('#fl-ffmpeg-warning')?.remove();
    });

    container.querySelector('[data-dismiss-sandbox-ai]')?.addEventListener('click', () => {
        container.querySelector('#fl-sandbox-ai-notice')?.remove();
    });

    container.querySelector('#fl-sample-btn')?.addEventListener('click', () => {
        container.querySelector('[data-source-mode="topic"]')?.click();
        const titleInput = container.querySelector('#fl-title');
        const topicInput = container.querySelector('#fl-topic-input');
        if (titleInput) titleInput.value = '3 Thói Quen Buổi Sáng Tăng Hiệu Suất';
        if (topicInput) {
            topicInput.value = '3 thói quen buổi sáng khoa học giúp não bộ tỉnh táo, giảm trì hoãn và tăng gấp đôi sự tập trung cho dân sáng tạo nội dung.';
        }
        container.querySelector('[data-duration="15"]')?.click();
        showToast('Đã điền mẫu thử Faceless nhanh!', 'info');
    });

    function updateKaraokePreview() {
        const enabled = burnSubtitlesInput.checked;
        karaokeControls.classList.toggle('opacity-45', !enabled);
        karaokeControls.classList.toggle('pointer-events-none', !enabled);
        previewCaptionWrap.classList.toggle('opacity-20', !enabled);

        if (aspectRatio === '9:16') {
            previewStage.className = 'karaoke-preview-stage w-52 aspect-[9/16] flex flex-col p-3 transition-all duration-300 shadow-inner';
        } else if (aspectRatio === '16:9') {
            previewStage.className = 'karaoke-preview-stage w-full max-w-sm aspect-[16/9] flex flex-col p-3 transition-all duration-300 shadow-inner';
        } else {
            previewStage.className = 'karaoke-preview-stage w-64 aspect-square flex flex-col p-3 transition-all duration-300 shadow-inner';
        }
        previewRatioBadge.textContent = aspectRatio;

        const pos = posSelect.value;
        if (pos === 'top') {
            previewStage.classList.add('justify-start');
            previewCaptionWrap.className = 'w-full flex justify-center text-center mt-8';
        } else if (pos === 'middle') {
            previewStage.classList.add('justify-center');
            previewCaptionWrap.className = 'w-full flex justify-center text-center my-0';
        } else {
            previewStage.classList.add('justify-end');
            previewCaptionWrap.className = 'w-full flex justify-center text-center mb-4';
        }

        const fontSizePx = Number(sizeRange.value) || 56;
        const scaledPx = Math.max(11, Math.min(22, Math.round(fontSizePx * 0.25)));
        const outlineW = Number(outlineWRange.value) || 0;
        const outlineColor = outlineCInput.value;
        const primary = primaryInput.value;
        const highlight = highlightInput.value;

        sizeVal.textContent = `${fontSizePx}px`;
        outlineWVal.textContent = `${outlineW}px`;
        primaryHex.textContent = primary.toUpperCase();
        highlightHex.textContent = highlight.toUpperCase();
        outlineCHex.textContent = outlineColor.toUpperCase();

        previewCaptionLine.style.fontFamily = `'${fontSelect.value}', sans-serif`;
        previewCaptionLine.style.fontSize = `${scaledPx}px`;
        previewCaptionLine.style.textShadow = outlineW > 0
            ? `0 0 ${Math.max(1, outlineW)}px ${outlineColor}, 0 1px ${outlineW + 1}px ${outlineColor}`
            : 'none';

        previewCaptionLine.innerHTML = PREVIEW_WORDS.map((word, idx) => {
            const isActive = idx === activeWordIdx;
            return `<span class="karaoke-word ${isActive ? 'is-active' : ''}" style="color: ${isActive ? highlight : primary}">${escapeHtml(word)}</span>`;
        }).join('');
    }

    previewIntervalId = setInterval(() => {
        if (!container.isConnected) {
            clearInterval(previewIntervalId);
            previewIntervalId = null;
            return;
        }
        activeWordIdx = (activeWordIdx + 1) % PREVIEW_WORDS.length;
        updateKaraokePreview();
    }, 550);

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
            console.warn('[faceless_view] Failed to load voices:', err);
        } finally {
            voiceLoading.classList.add('hidden');
        }
    }

    modeTabs.forEach((btn) => {
        btn.addEventListener('click', () => {
            sourceMode = btn.dataset.sourceMode;
            modeTabs.forEach((b) => {
                const active = b.dataset.sourceMode === sourceMode;
                b.className = `fl-mode-tab px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                    active ? 'bg-violet-500 text-white' : 'text-slate-400 hover:text-white'
                }`;
            });
            panelTopic.classList.toggle('hidden', sourceMode !== 'topic');
            panelScript.classList.toggle('hidden', sourceMode !== 'script');
            panelArticle.classList.toggle('hidden', sourceMode !== 'article_url');
        });
    });

    aspectBtns.forEach((btn) => {
        btn.addEventListener('click', () => {
            aspectRatio = btn.dataset.aspect;
            aspectBtns.forEach((b) => {
                const active = b.dataset.aspect === aspectRatio;
                b.className = `fl-aspect-btn rounded-xl border p-3 text-left transition ${
                    active
                        ? 'bg-violet-500/15 border-violet-400 text-white'
                        : 'bg-[#0B0D12] border-[#262E42] text-slate-400 hover:border-slate-500'
                }`;
            });
            updateKaraokePreview();
        });
    });

    durBtns.forEach((btn) => {
        btn.addEventListener('click', () => {
            durationPreset = btn.dataset.duration;
            durBtns.forEach((b) => {
                const active = b.dataset.duration === durationPreset;
                b.className = `fl-dur-btn px-3 py-1.5 rounded-lg text-xs font-semibold border transition ${
                    active
                        ? 'bg-violet-500 text-white border-violet-400'
                        : 'bg-[#0B0D12] border-[#262E42] text-slate-300 hover:border-slate-500'
                }`;
            });
            customDurWrap.classList.toggle('hidden', durationPreset !== 'custom');
        });
    });

    [
        burnSubtitlesInput,
        fontSelect,
        posSelect,
        sizeRange,
        outlineWRange,
        primaryInput,
        highlightInput,
        outlineCInput,
    ].forEach((el) => {
        el?.addEventListener('input', updateKaraokePreview);
        el?.addEventListener('change', updateKaraokePreview);
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

        const topicVal = container.querySelector('#fl-topic-input').value.trim();
        const scriptVal = container.querySelector('#fl-script-input').value.trim();
        const articleVal = container.querySelector('#fl-article-input').value.trim();

        if (sourceMode === 'topic' && !topicVal) {
            showToast('Vui lòng nhập chủ đề video', 'warning');
            return;
        }
        if (sourceMode === 'script' && !scriptVal) {
            showToast('Vui lòng nhập kịch bản thuyết minh', 'warning');
            return;
        }
        if (sourceMode === 'article_url') {
            if (!articleVal || !isValidHttpUrl(articleVal)) {
                showToast('Vui lòng nhập URL bài báo hợp lệ (http/https)', 'warning');
                return;
            }
        }

        const sceneMin = Number(sceneMinInput.value) || 3;
        const sceneMax = Number(sceneMaxInput.value) || 5;
        if (sceneMax < sceneMin) {
            showToast('Thời lượng tối đa mỗi cảnh phải ≥ thời lượng tối thiểu', 'error');
            return;
        }

        const rawTargetDur = durationPreset === 'custom'
            ? Number(customDurInput.value)
            : Number(durationPreset);
        if (!Number.isFinite(rawTargetDur) || rawTargetDur < 5 || rawTargetDur > 600) {
            showToast('Thời lượng mục tiêu phải từ 5 đến 600 giây', 'error');
            return;
        }

        const customTitle = container.querySelector('#fl-title').value.trim();
        const fallbackSourceContent = sourceMode === 'topic' ? topicVal : sourceMode === 'script' ? scriptVal : articleVal;
        const burnSubs = Boolean(burnSubtitlesInput.checked);

        const bgmConfig = bgmMixer.getConfig();
        const payload = {
            mode: 'faceless',
            title: customTitle || deriveDefaultTitle(sourceMode, fallbackSourceContent),
            source_mode: sourceMode,
            topic: sourceMode === 'topic' ? topicVal : null,
            script: sourceMode === 'script' ? scriptVal : null,
            article_url: sourceMode === 'article_url' ? articleVal : null,
            aspect_ratio: aspectRatio,
            target_duration_seconds: Math.round(rawTargetDur),
            pipeline_config: {
                llm_provider: container.querySelector('#fl-llm-provider').value || null,
                visual_provider: container.querySelector('#fl-visual-provider').value || null,
                tts_provider: ttsProviderSelect.value || null,
                target_lang: targetLangSelect.value,
                tone: container.querySelector('#fl-tone').value,
                voice_id: voiceSelect.value || null,
                scene_duration_min: sceneMin,
                scene_duration_max: sceneMax,
                ken_burns_enabled: Boolean(container.querySelector('#fl-ken-burns').checked),
                ...bgmConfig,
                burn_subtitles: burnSubs,
                subtitle_style: {
                    font_name: fontSelect.value,
                    font_size: Number(sizeRange.value) || 56,
                    primary_color: primaryInput.value.toUpperCase(),
                    highlight_color: highlightInput.value.toUpperCase(),
                    outline_color: outlineCInput.value.toUpperCase(),
                    outline_width: Number(outlineWRange.value) ?? 3,
                    position: posSelect.value,
                    burn_subtitles: burnSubs,
                },
            },
        };

        isSubmitting = true;
        submitBtn.disabled = true;
        submitBtn.classList.add('opacity-70');
        submitLabel.textContent = 'Đang khởi tạo Storyboard...';

        try {
            const project = await api.createProject(payload);
            showToast(`Đã tạo dự án Faceless "${project.title}"`, 'success');

            try {
                const updatedList = await api.listProjects({ limit: 50 });
                store.setState({ projects: Array.isArray(updatedList) ? updatedList : [] });
            } catch {
                // Non-fatal
            }

            if (handlers?.onProjectCreated) {
                handlers.onProjectCreated(project);
            }
        } catch (err) {
            showToast(err?.message || 'Không thể tạo dự án Faceless', 'error');
        } finally {
            isSubmitting = false;
            submitBtn.disabled = false;
            submitBtn.classList.remove('opacity-70');
            submitLabel.textContent = 'Tạo Storyboard & Video Faceless';
        }
    });

    updateKaraokePreview();
    loadVoicesForSelection();
}
