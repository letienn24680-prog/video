/**
 * Account Profile, System Diagnostics & Default Studio Preferences View
 */

import { store, showToast, savePreferences, confirmDialog } from '../core/state.js';
import { api } from '../core/api.js';
import { logout, openAuthModal, loginWithSandbox } from '../core/auth.js';

let activeUnsubscribe = null;

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function statusBadge(ok, okText = 'OK', missingText = 'Thiếu') {
    if (ok) {
        return `<span class="inline-flex items-center gap-1 whitespace-nowrap shrink-0 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">● ${escapeHtml(okText)}</span>`;
    }
    return `<span class="inline-flex items-center gap-1 whitespace-nowrap shrink-0 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-rose-500/15 text-rose-300 border border-rose-500/30">○ ${escapeHtml(missingText)}</span>`;
}

function normalizeTranslationStyle(val) {
    if (val === 'casual' || !val) return 'natural';
    return val;
}

function secretPlaceholder(isConfigured) {
    return isConfigured
        ? '•••••••• (đã cấu hình — để trống nếu giữ nguyên)'
        : 'Chưa cấu hình — dán key vào đây';
}

function renderEnvInputField({ key, label, type = 'password', placeholder = '', configured = null, hint = '' }) {
    const dot = configured === null
        ? ''
        : `<span class="inline-block w-2 h-2 rounded-full ${configured ? 'bg-emerald-400' : 'bg-slate-600'}" title="${configured ? 'Đã cấu hình' : 'Chưa cấu hình'}"></span>`;
    const isPassword = type === 'password';
    const inputId = `env-input-${key.toLowerCase()}`;
    return `
        <div class="space-y-1">
            <label for="${inputId}" class="flex items-center justify-between gap-2 text-[11px] font-medium text-slate-300">
                <span class="flex items-center gap-1.5 truncate">${dot}<span>${escapeHtml(label)}</span></span>
                <code class="text-[10px] text-slate-500 font-mono shrink-0">${escapeHtml(key)}</code>
            </label>
            <div class="relative flex items-center">
                <input id="${inputId}" type="${escapeHtml(type)}" data-env-key="${escapeHtml(key)}"
                    value="" autocomplete="off" spellcheck="false"
                    placeholder="${escapeHtml(placeholder)}"
                    class="w-full px-3 py-2 ${isPassword ? 'pr-9' : ''} rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white placeholder:text-slate-500 focus:outline-none focus:border-cyan-400" />
                ${isPassword ? `
                    <button type="button" data-toggle-secret="${inputId}" aria-label="Hiện/ẩn ${escapeHtml(key)}"
                        class="absolute right-2 text-slate-400 hover:text-slate-200 text-xs px-1 py-0.5 rounded">
                        👁️
                    </button>
                ` : ''}
            </div>
            ${hint ? `<p class="text-[10px] text-slate-500">${escapeHtml(hint)}</p>` : ''}
        </div>
    `;
}

export function renderSettingsView(container, handlers = {}) {
    if (!container) return;

    if (activeUnsubscribe) {
        activeUnsubscribe();
        activeUnsubscribe = null;
    }

    const viewState = {
        loadingDiag: false,
        loadingProfile: false,
        savingProfile: false,
        savingConfig: false,
        cleaningTemp: false,
        configNotice: '',
        diagnostics: store.getState().diagnostics || null,
        profile: store.getState().profile || null,
    };

    async function loadDiagnosticsAndProfile() {
        viewState.loadingDiag = true;
        if (store.getState().accessToken) {
            viewState.loadingProfile = true;
        }
        render();

        const diagPromise = api.getSystemDiagnostics()
            .then((diag) => {
                viewState.diagnostics = diag;
                store.setState({ diagnostics: diag });
            })
            .catch((err) => {
                console.warn('[settings_view] Failed to load diagnostics:', err);
            })
            .finally(() => {
                viewState.loadingDiag = false;
            });

        const profilePromise = store.getState().accessToken
            ? api.getAccountProfile()
                .then((prof) => {
                    viewState.profile = prof;
                    store.setState({
                        profile: {
                            ...(store.getState().profile || {}),
                            ...prof,
                            subscription_tier: prof?.tier || prof?.subscription_tier || 'free',
                        },
                    });
                })
                .catch((err) => {
                    console.warn('[settings_view] Failed to load profile:', err);
                })
                .finally(() => {
                    viewState.loadingProfile = false;
                })
            : Promise.resolve().then(() => {
                viewState.profile = null;
                viewState.loadingProfile = false;
            });

        await Promise.all([diagPromise, profilePromise]);
        render();
    }

    function render() {
        if (!container.isConnected) return;

        const state = store.getState();
        const isAuthed = Boolean(state.accessToken);
        const isSandboxEnabled = Boolean(state.publicConfig?.local_sandbox_mode);
        const prof = viewState.profile || state.profile || {};
        const user = state.user || {};
        const diag = viewState.diagnostics || state.diagnostics || {};
        const tools = diag.tools || {};
        const providers = diag.providers || {};
        const storage = diag.storage || {};
        const queue = diag.queue || {};

        const prefs = state.preferences || {};
        const prefAspect = prefs.aspectRatio || '9:16';
        const prefLang = prefs.targetLang || 'vi';
        const prefStyle = normalizeTranslationStyle(prefs.translationStyle);
        const prefBurn = prefs.burnSubtitles !== undefined ? Boolean(prefs.burnSubtitles) : true;

        const tier = String(prof.tier || prof.subscription_tier || 'free').toLowerCase();
        const authMode = prof.is_sandbox || isSandboxEnabled ? 'Local Sandbox' : 'Supabase Auth';

        const aiEnvFields = [
            { key: 'GEMINI_API_KEY', label: 'Google Gemini API Key', configured: Boolean(providers.gemini) },
            { key: 'GROQ_API_KEY', label: 'Groq API Key (Whisper STT)', configured: Boolean(providers.groq) },
            {
                key: 'PIXABAY_API_KEY',
                label: 'Pixabay Stock API Key',
                hint: 'Khuyên dùng miễn phí khi Pexels tạm dừng',
                configured: Boolean(providers.pixabay),
            },
            { key: 'PEXELS_API_KEY', label: 'Pexels Stock API Key', configured: Boolean(providers.pexels) },
            { key: 'REPLICATE_API_TOKEN', label: 'Replicate API Token', configured: Boolean(providers.replicate) },
            { key: 'ELEVENLABS_API_KEY', label: 'ElevenLabs TTS API Key', configured: Boolean(providers.elevenlabs) },
            { key: 'FPT_AI_API_KEY', label: 'FPT.AI TTS API Key', configured: Boolean(providers.fpt_ai) },
            { key: 'VIETTEL_AI_TOKEN', label: 'Viettel AI TTS Token', configured: Boolean(providers.viettel_ai) },
        ].map((item) => ({
            ...item,
            type: 'password',
            placeholder: secretPlaceholder(item.configured),
        }));

        const supabaseConfigured = Boolean(providers.supabase_configured);
        const supabaseEnvFields = [
            {
                key: 'SUPABASE_URL',
                label: 'Supabase Project URL',
                type: 'text',
                configured: supabaseConfigured,
                placeholder: supabaseConfigured
                    ? 'https://xxxx.supabase.co (đã cấu hình — nhập mới để đổi)'
                    : 'VD: https://xxxx.supabase.co',
            },
            {
                key: 'SUPABASE_ANON_KEY',
                label: 'Supabase Anon Key',
                type: 'password',
                configured: supabaseConfigured,
                placeholder: secretPlaceholder(supabaseConfigured),
            },
            {
                key: 'SUPABASE_SERVICE_ROLE_KEY',
                label: 'Supabase Service Role Key',
                type: 'password',
                configured: supabaseConfigured,
                placeholder: secretPlaceholder(supabaseConfigured),
            },
            {
                key: 'SUPABASE_JWT_SECRET',
                label: 'Supabase JWT Secret',
                type: 'password',
                configured: supabaseConfigured,
                placeholder: secretPlaceholder(supabaseConfigured),
            },
        ];

        const sepayConfigured = Boolean(providers.sepay_configured);
        const paymentEnvFields = [
            {
                key: 'VIETQR_BANK_BIN',
                label: 'Mã BIN Ngân hàng VietQR',
                type: 'text',
                placeholder: 'VD: 970422 (MB), 970436 (VCB), 970415 (VietinBank)',
            },
            {
                key: 'VIETQR_ACCOUNT_NO',
                label: 'Số tài khoản VietQR',
                type: 'text',
                placeholder: 'Nhập số tài khoản nhận chuyển khoản',
            },
            {
                key: 'VIETQR_ACCOUNT_NAME',
                label: 'Tên chủ tài khoản',
                type: 'text',
                placeholder: 'VD: NGUYEN VAN A (không dấu)',
            },
            {
                key: 'SEPAY_API_KEY',
                label: 'SePay API Key',
                type: 'password',
                configured: sepayConfigured,
                placeholder: secretPlaceholder(sepayConfigured),
            },
            {
                key: 'SEPAY_WEBHOOK_SECRET',
                label: 'SePay Webhook Secret',
                type: 'password',
                configured: sepayConfigured,
                placeholder: secretPlaceholder(sepayConfigured),
            },
        ];

        const providerItems = [
            { key: 'groq', label: 'Groq (Whisper STT)' },
            { key: 'gemini', label: 'Google Gemini (LLM)' },
            { key: 'pixabay', label: 'Pixabay Stock' },
            { key: 'pexels', label: 'Pexels Stock' },
            { key: 'replicate', label: 'Replicate (Demucs / Flux)' },
            { key: 'elevenlabs', label: 'ElevenLabs TTS' },
            { key: 'fpt_ai', label: 'FPT.AI TTS' },
            { key: 'viettel_ai', label: 'Viettel AI TTS' },
        ];

        container.innerHTML = `
            <div class="space-y-6">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                    <div>
                        <h2 class="text-xl font-bold text-white flex items-center gap-2">
                            <span>⚙️</span> Tài khoản &amp; Chẩn đoán Hệ thống
                        </h2>
                        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">
                            Quản lý hồ sơ cá nhân, kiểm tra FFmpeg / AI Providers, dọn thư mục tạm và cấu hình mặc định.
                        </p>
                    </div>
                    <button type="button" id="set-refresh-btn" ${viewState.loadingDiag ? 'disabled' : ''}
                        class="self-start sm:self-auto px-3.5 py-2 rounded-xl text-xs font-semibold bg-[#191F2E] hover:bg-[#232B40] border border-[#262E42] text-slate-200 transition">
                        ${viewState.loadingDiag ? '⏳ Đang kiểm tra...' : '↻ Kiểm tra lại hệ thống'}
                    </button>
                </div>

                <div class="grid grid-cols-1 xl:grid-cols-12 gap-6">
                    <!-- Profile & Account (5 cols) -->
                    <section class="xl:col-span-5 rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-4">
                        <h3 class="text-sm font-bold text-white flex items-center gap-2">
                            <span>👤</span> Hồ sơ &amp; Tài khoản
                        </h3>

                        ${!isAuthed ? `
                            <div class="rounded-xl border border-dashed border-[#262E42] bg-[#0B0D12]/80 p-6 text-center space-y-3">
                                <p class="text-xs text-slate-400">
                                    Bạn chưa đăng nhập. Đăng nhập để chỉnh sửa tên hiển thị và thực hiện dọn dẹp hệ thống.
                                </p>
                                <div class="flex flex-wrap justify-center gap-2">
                                    <button type="button" id="set-login-btn"
                                        class="px-4 py-2 rounded-xl text-xs font-semibold bg-violet-600 hover:bg-violet-500 text-white transition">
                                        Đăng nhập
                                    </button>
                                    ${isSandboxEnabled ? `
                                        <button type="button" id="set-sandbox-btn"
                                            class="px-4 py-2 rounded-xl text-xs font-bold bg-gradient-to-r from-cyan-500 to-violet-600 text-white transition">
                                            ⚡ Sandbox Demo
                                        </button>
                                    ` : ''}
                                </div>
                            </div>
                        ` : `
                            <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-4 space-y-2.5 text-xs">
                                <div class="flex items-center justify-between gap-2">
                                    <span class="text-slate-400">Email</span>
                                    <span class="font-semibold text-white truncate">${escapeHtml(prof.email || user.email || '—')}</span>
                                </div>
                                <div class="flex items-center justify-between gap-2">
                                    <span class="text-slate-400">UID</span>
                                    <span class="font-mono text-[11px] text-slate-300 truncate">${escapeHtml(prof.id || user.id || '—')}</span>
                                </div>
                                <div class="flex items-center justify-between gap-2">
                                    <span class="text-slate-400">Gói hiện tại</span>
                                    <span class="px-2 py-0.5 rounded-full text-[11px] font-bold uppercase bg-violet-500/20 text-violet-300 border border-violet-500/30">
                                        ${escapeHtml(tier.toUpperCase())}
                                    </span>
                                </div>
                                <div class="flex items-center justify-between gap-2">
                                    <span class="text-slate-400">Chế độ xác thực</span>
                                    <span class="px-2 py-0.5 rounded-md text-[11px] font-medium bg-cyan-500/10 text-cyan-300 border border-cyan-500/25">
                                        ${escapeHtml(authMode)}
                                    </span>
                                </div>
                            </div>

                            <form id="set-profile-form" class="space-y-3">
                                <div>
                                    <label for="set-display-name" class="block text-xs font-medium text-slate-300 mb-1.5">
                                        Tên hiển thị (Display Name)
                                    </label>
                                    <div class="flex gap-2">
                                        <input id="set-display-name" type="text" maxlength="80" required
                                            value="${escapeHtml(prof.display_name || '')}"
                                            placeholder="VD: Minh Studio"
                                            class="flex-1 px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs sm:text-sm text-white focus:outline-none focus:border-violet-400" />
                                        <button type="submit" id="set-save-profile-btn" ${viewState.savingProfile ? 'disabled' : ''}
                                            class="px-4 py-2 rounded-xl text-xs font-semibold bg-violet-600 hover:bg-violet-500 text-white transition shrink-0">
                                            ${viewState.savingProfile ? 'Đang lưu...' : 'Lưu tên'}
                                        </button>
                                    </div>
                                </div>
                            </form>

                            <div class="pt-2 border-t border-[#262E42] flex justify-end">
                                <button type="button" id="set-logout-btn"
                                    class="px-4 py-2 rounded-xl text-xs font-semibold bg-rose-500/15 hover:bg-rose-500/25 text-rose-300 border border-rose-500/30 transition">
                                    🚪 Đăng xuất
                                </button>
                            </div>
                        `}

                        <!-- Default Studio Preferences -->
                        <div class="pt-4 border-t border-[#262E42] space-y-3">
                            <h3 class="text-sm font-bold text-white flex items-center gap-2">
                                <span>🎛️</span> Cấu hình Studio mặc định
                            </h3>
                            <form id="set-prefs-form" class="space-y-3">
                                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                    <div>
                                        <label for="pref-aspect" class="block text-[11px] text-slate-400 mb-1">Tỷ lệ khung hình</label>
                                        <select id="pref-aspect"
                                            class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                            <option value="9:16" ${prefAspect === '9:16' ? 'selected' : ''}>9:16 (Dọc)</option>
                                            <option value="16:9" ${prefAspect === '16:9' ? 'selected' : ''}>16:9 (Ngang)</option>
                                            <option value="1:1" ${prefAspect === '1:1' ? 'selected' : ''}>1:1 (Vuông)</option>
                                        </select>
                                    </div>
                                    <div>
                                        <label for="pref-lang" class="block text-[11px] text-slate-400 mb-1">Ngôn ngữ đích</label>
                                        <select id="pref-lang"
                                            class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                            <option value="vi" ${prefLang === 'vi' ? 'selected' : ''}>Tiếng Việt (vi)</option>
                                            <option value="en" ${prefLang === 'en' ? 'selected' : ''}>English (en)</option>
                                            <option value="ja" ${prefLang === 'ja' ? 'selected' : ''}>日本語 (ja)</option>
                                            <option value="ko" ${prefLang === 'ko' ? 'selected' : ''}>한국어 (ko)</option>
                                            <option value="zh" ${prefLang === 'zh' ? 'selected' : ''}>中文 (zh)</option>
                                            <option value="fr" ${prefLang === 'fr' ? 'selected' : ''}>Français (fr)</option>
                                            <option value="es" ${prefLang === 'es' ? 'selected' : ''}>Español (es)</option>
                                        </select>
                                    </div>
                                    <div class="sm:col-span-2">
                                        <label for="pref-style" class="block text-[11px] text-slate-400 mb-1">Văn phong dịch mặc định</label>
                                        <select id="pref-style"
                                            class="w-full px-2.5 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                            <option value="natural" ${prefStyle === 'natural' ? 'selected' : ''}>Đời thường (Tự nhiên)</option>
                                            <option value="movie_review" ${prefStyle === 'movie_review' ? 'selected' : ''}>Review phim</option>
                                            <option value="news" ${prefStyle === 'news' ? 'selected' : ''}>Tin tức / Bản tin</option>
                                            <option value="humorous" ${prefStyle === 'humorous' ? 'selected' : ''}>Hài hước / Bắt trend</option>
                                            <option value="technical" ${prefStyle === 'technical' ? 'selected' : ''}>Chuyên ngành</option>
                                        </select>
                                    </div>
                                </div>

                                <label class="flex items-center gap-2.5 cursor-pointer pt-1">
                                    <input id="pref-burn-subs" type="checkbox" ${prefBurn ? 'checked' : ''}
                                        class="w-4 h-4 rounded border-[#262E42] bg-[#0B0D12] text-cyan-500 focus:ring-cyan-500" />
                                    <span class="text-xs text-slate-200">Mặc định ghi cứng phụ đề Karaoke (.ass)</span>
                                </label>

                                <button type="submit"
                                    class="w-full py-2.5 px-4 rounded-xl text-xs font-bold bg-[#191F2E] hover:bg-[#232B40] border border-cyan-500/40 text-cyan-300 transition">
                                    💾 Lưu cấu hình mặc định
                                </button>
                            </form>
                        </div>
                    </section>

                    <!-- System Diagnostics & Temp Cleanup (7 cols) -->
                    <section class="xl:col-span-7 rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-5">
                        <h3 class="text-sm font-bold text-white flex items-center gap-2">
                            <span>🩺</span> Chẩn đoán Hệ thống &amp; Công cụ Media
                        </h3>

                        <!-- Media Tools -->
                        <div class="space-y-2.5">
                            <div class="text-xs font-semibold uppercase tracking-wider text-slate-400">Công cụ xử lý Video / Audio</div>
                            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3.5 space-y-1.5">
                                    <div class="flex items-center justify-between">
                                        <span class="text-xs font-bold text-white">FFmpeg</span>
                                        ${statusBadge(Boolean(tools.ffmpeg?.available))}
                                    </div>
                                    <p class="text-[11px] font-mono text-slate-400 truncate" title="${escapeHtml(tools.ffmpeg?.version || tools.ffmpeg?.path || '')}">
                                        ${escapeHtml(tools.ffmpeg?.version || tools.ffmpeg?.path || 'Chưa tìm thấy trong PATH')}
                                    </p>
                                </div>

                                <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3.5 space-y-1.5">
                                    <div class="flex items-center justify-between">
                                        <span class="text-xs font-bold text-white">FFprobe</span>
                                        ${statusBadge(Boolean(tools.ffprobe?.available))}
                                    </div>
                                    <p class="text-[11px] font-mono text-slate-400 truncate">
                                        ${escapeHtml(tools.ffprobe?.path || 'Chưa tìm thấy trong PATH')}
                                    </p>
                                </div>

                                <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3.5 flex items-center justify-between">
                                    <div>
                                        <div class="text-xs font-bold text-white">yt-dlp</div>
                                        <div class="text-[11px] text-slate-400">Tải video TikTok / Shorts</div>
                                    </div>
                                    ${statusBadge(Boolean(tools.yt_dlp?.available))}
                                </div>

                                <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3.5 flex items-center justify-between">
                                    <div>
                                        <div class="text-xs font-bold text-white">Edge-TTS</div>
                                        <div class="text-[11px] text-slate-400">Giọng đọc miễn phí mặc định</div>
                                    </div>
                                    ${statusBadge(Boolean(tools.edge_tts?.available))}
                                </div>
                            </div>
                        </div>

                        <!-- Platform & AI Providers -->
                        <div class="space-y-2.5">
                            <div class="text-xs font-semibold uppercase tracking-wider text-slate-400">Backend &amp; AI Providers</div>
                            <div class="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
                                <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] px-3 py-2.5 flex items-center justify-between gap-2">
                                    <span class="text-xs text-slate-200 truncate">Local Sandbox</span>
                                    ${statusBadge(Boolean(providers.local_sandbox_mode), 'Bật', 'Tắt')}
                                </div>
                                <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] px-3 py-2.5 flex items-center justify-between gap-2">
                                    <span class="text-xs text-slate-200 truncate">Supabase</span>
                                    ${statusBadge(Boolean(providers.supabase_configured))}
                                </div>
                                <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] px-3 py-2.5 flex items-center justify-between gap-2">
                                    <span class="text-xs text-slate-200 truncate">SePay VietQR</span>
                                    ${statusBadge(Boolean(providers.sepay_configured))}
                                </div>
                                ${providerItems.map((p) => `
                                    <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] px-3 py-2.5 flex items-center justify-between gap-2">
                                        <span class="text-xs text-slate-200 truncate">${escapeHtml(p.label)}</span>
                                        ${statusBadge(Boolean(providers[p.key]))}
                                    </div>
                                `).join('')}
                            </div>
                        </div>

                        <!-- Storage & Temp Cleanup -->
                        <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                            <div class="space-y-1 text-xs">
                                <div class="font-bold text-white">Thư mục tạm &amp; Hàng đợi Worker</div>
                                <div class="text-slate-400 font-mono text-[11px] break-all">
                                    Temp: ${escapeHtml(storage.temp_dir || './temp')} ·
                                    ${Number(storage.temp_job_dirs || 0)} thư mục job ·
                                    <strong class="text-cyan-300">${Number(storage.temp_size_mb || 0).toFixed(2)} MB</strong>
                                </div>
                                <div class="text-slate-400 text-[11px]">
                                    Storage: <strong class="text-slate-200">${escapeHtml(storage.provider || 'local')}</strong> ·
                                    Active Jobs: <strong class="text-slate-200">${Number(queue.active_jobs || 0)}/${Number(queue.max_concurrent_jobs || 3)}</strong> ·
                                    Auto-Cleanup: <strong class="text-slate-200">${queue.auto_cleanup_running ? 'Đang chạy' : 'Tạm dừng'}</strong>
                                </div>
                            </div>

                            <button type="button" id="set-cleanup-btn" ${viewState.cleaningTemp ? 'disabled' : ''}
                                class="px-4 py-2.5 rounded-xl text-xs font-semibold bg-amber-500/15 hover:bg-amber-500/25 text-amber-300 border border-amber-500/35 transition shrink-0">
                                ${viewState.cleaningTemp ? '⏳ Đang dọn...' : '🧹 Dọn thư mục tạm'}
                            </button>
                        </div>
                    </section>
                </div>

                <!-- Interactive .env Configuration Panel -->
                <section id="set-env-config-section" class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-5">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-[#262E42] pb-4">
                        <div>
                            <h3 class="text-sm sm:text-base font-bold text-white flex items-center gap-2">
                                <span>🔑</span> Cấu hình API Keys, Supabase &amp; VietQR (.env)
                            </h3>
                            <p class="text-xs text-slate-400 mt-1">
                                Nhập trực tiếp khóa dịch vụ hoặc đường dẫn công cụ để ghi vào <code class="text-cyan-300">.env</code> và áp dụng ngay. Các ô bỏ trống sẽ giữ nguyên giá trị hiện tại.
                            </p>
                        </div>
                        <div class="flex flex-wrap items-center gap-2 shrink-0">
                            <span class="text-[11px] text-slate-400">Trạng thái:</span>
                            ${statusBadge(Boolean(providers.gemini) && Boolean(providers.groq), 'Core AI OK', 'Thiếu AI Key')}
                            ${statusBadge(supabaseConfigured, 'Supabase OK', 'Chưa bật Supabase')}
                            ${statusBadge(sepayConfigured, 'SePay OK', 'Chưa bật SePay')}
                        </div>
                    </div>

                    ${!isAuthed ? `
                        <div class="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <p class="text-xs text-amber-200">
                                Bạn cần đăng nhập (hoặc kích hoạt Sandbox Demo) để lưu thay đổi vào cấu hình <code class="font-mono">.env</code>.
                            </p>
                            <div class="flex items-center gap-2 shrink-0">
                                <button type="button" id="set-env-login-btn"
                                    class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-violet-600 hover:bg-violet-500 text-white transition">
                                    Đăng nhập
                                </button>
                                ${isSandboxEnabled ? `
                                    <button type="button" id="set-env-sandbox-btn"
                                        class="px-3 py-1.5 rounded-lg text-xs font-bold bg-gradient-to-r from-cyan-500 to-violet-600 text-white transition">
                                        ⚡ Sandbox Demo
                                    </button>
                                ` : ''}
                            </div>
                        </div>
                    ` : ''}

                    ${viewState.configNotice ? `
                        <div class="rounded-xl border border-cyan-500/35 bg-cyan-500/10 px-4 py-3 text-xs text-cyan-200 flex items-start justify-between gap-3">
                            <span>${escapeHtml(viewState.configNotice)}</span>
                            <button type="button" id="set-dismiss-config-notice" class="text-cyan-300 hover:text-white shrink-0">✕</button>
                        </div>
                    ` : ''}

                    <form id="set-env-config-form" class="space-y-6" autocomplete="off">
                        <!-- Group 1: AI & Media APIs -->
                        <div class="space-y-3">
                            <div class="flex items-center justify-between">
                                <h4 class="text-xs font-bold uppercase tracking-wider text-cyan-300">AI &amp; Media APIs</h4>
                                <span class="text-[11px] text-slate-500">STT, LLM, Stock Video &amp; TTS</span>
                            </div>
                            <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3.5">
                                ${aiEnvFields.map(renderEnvInputField).join('')}
                            </div>
                        </div>

                        <!-- Group 2 & 3: Supabase Cloud & Thanh toán VietQR / SePay -->
                        <div class="grid grid-cols-1 xl:grid-cols-2 gap-5">
                            <div class="rounded-xl bg-[#0B0D12]/70 border border-[#262E42] p-4 space-y-3">
                                <div class="flex items-center justify-between gap-2">
                                    <h4 class="text-xs font-bold uppercase tracking-wider text-violet-300">Supabase Cloud</h4>
                                    ${statusBadge(supabaseConfigured, 'Đã cấu hình', 'Chưa cấu hình')}
                                </div>
                                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                    ${supabaseEnvFields.map(renderEnvInputField).join('')}
                                </div>
                            </div>

                            <div class="rounded-xl bg-[#0B0D12]/70 border border-[#262E42] p-4 space-y-3">
                                <div class="flex items-center justify-between gap-2">
                                    <h4 class="text-xs font-bold uppercase tracking-wider text-emerald-300">Thanh toán VietQR / SePay</h4>
                                    ${statusBadge(sepayConfigured, 'SePay OK', 'Chưa cấu hình SePay')}
                                </div>
                                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                    ${paymentEnvFields.map(renderEnvInputField).join('')}
                                </div>
                            </div>
                        </div>

                        <!-- Group 4: Đường dẫn FFmpeg cục bộ & Storage -->
                        <div class="rounded-xl bg-[#0B0D12]/70 border border-[#262E42] p-4 space-y-3">
                            <div class="flex items-center justify-between gap-2">
                                <h4 class="text-xs font-bold uppercase tracking-wider text-amber-300">Đường dẫn FFmpeg cục bộ &amp; Lưu trữ</h4>
                                ${statusBadge(Boolean(tools.ffmpeg?.available) && Boolean(tools.ffprobe?.available), 'FFmpeg OK', 'Kiểm tra FFmpeg')}
                            </div>
                            <div class="grid grid-cols-1 md:grid-cols-3 gap-3.5">
                                ${renderEnvInputField({
                                    key: 'FFMPEG_BIN',
                                    label: 'Đường dẫn FFmpeg',
                                    type: 'text',
                                    configured: Boolean(tools.ffmpeg?.available),
                                    placeholder: tools.ffmpeg?.path ? `Hiện tại: ${tools.ffmpeg.path}` : 'VD: ffmpeg hoặc C:\\ffmpeg\\bin\\ffmpeg.exe',
                                })}
                                ${renderEnvInputField({
                                    key: 'FFPROBE_BIN',
                                    label: 'Đường dẫn FFprobe',
                                    type: 'text',
                                    configured: Boolean(tools.ffprobe?.available),
                                    placeholder: tools.ffprobe?.path ? `Hiện tại: ${tools.ffprobe.path}` : 'VD: ffprobe hoặc C:\\ffmpeg\\bin\\ffprobe.exe',
                                })}
                                <div class="space-y-1">
                                    <label for="env-input-storage_provider" class="flex items-center justify-between gap-2 text-[11px] font-medium text-slate-300">
                                        <span>Storage Provider</span>
                                        <code class="text-[10px] text-slate-500 font-mono">STORAGE_PROVIDER</code>
                                    </label>
                                    <select id="env-input-storage_provider" data-env-key="STORAGE_PROVIDER"
                                        class="w-full px-3 py-2 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400">
                                        <option value="">-- Giữ nguyên (${escapeHtml(storage.provider || 'local')}) --</option>
                                        <option value="local">local (Lưu trữ cục bộ)</option>
                                        <option value="supabase">supabase (Supabase Storage)</option>
                                    </select>
                                </div>
                            </div>
                        </div>

                        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pt-1">
                            <p class="text-[11px] text-slate-400">
                                Chỉ gửi các trường có giá trị mới. Khóa bí mật không bao giờ hiển thị lại sau khi lưu.
                            </p>
                            <button type="submit" id="set-save-env-btn" ${viewState.savingConfig ? 'disabled' : ''}
                                class="px-5 py-2.5 rounded-xl text-xs font-bold bg-gradient-to-r from-cyan-500 to-violet-600 hover:opacity-95 disabled:opacity-60 text-white shadow-lg shadow-violet-900/20 transition shrink-0">
                                ${viewState.savingConfig ? '⏳ Đang lưu & áp dụng...' : '💾 Lưu cấu hình & Áp dụng ngay'}
                            </button>
                        </div>
                    </form>
                </section>
            </div>
        `;

        async function activateSandboxDemo(btn) {
            if (btn) btn.disabled = true;
            try {
                await loginWithSandbox();
                await loadDiagnosticsAndProfile();
            } catch (err) {
                showToast(err?.message || 'Không thể kích hoạt Sandbox', 'error');
                if (btn) btn.disabled = false;
            }
        }

        container.querySelector('#set-refresh-btn')?.addEventListener('click', loadDiagnosticsAndProfile);

        container.querySelector('#set-login-btn')?.addEventListener('click', () => {
            openAuthModal({ onSuccess: () => loadDiagnosticsAndProfile() });
        });
        container.querySelector('#set-env-login-btn')?.addEventListener('click', () => {
            openAuthModal({ onSuccess: () => loadDiagnosticsAndProfile() });
        });

        const sbBtn = container.querySelector('#set-sandbox-btn');
        sbBtn?.addEventListener('click', () => activateSandboxDemo(sbBtn));
        const envSbBtn = container.querySelector('#set-env-sandbox-btn');
        envSbBtn?.addEventListener('click', () => activateSandboxDemo(envSbBtn));

        container.querySelector('#set-dismiss-config-notice')?.addEventListener('click', () => {
            viewState.configNotice = '';
            render();
        });

        container.querySelectorAll('[data-toggle-secret]').forEach((toggleBtn) => {
            toggleBtn.addEventListener('click', () => {
                const targetId = toggleBtn.getAttribute('data-toggle-secret');
                const inputEl = targetId ? container.querySelector(`#${targetId}`) : null;
                if (!inputEl) return;
                const showing = inputEl.type === 'text';
                inputEl.type = showing ? 'password' : 'text';
                toggleBtn.textContent = showing ? '👁️' : '🙈';
            });
        });

        container.querySelector('#set-env-config-form')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!store.getState().accessToken) {
                showToast('Vui lòng đăng nhập để lưu cấu hình .env', 'warning');
                openAuthModal({ onSuccess: () => loadDiagnosticsAndProfile() });
                return;
            }

            const updates = {};
            container.querySelectorAll('#set-env-config-form [data-env-key]').forEach((field) => {
                const envKey = field.getAttribute('data-env-key');
                const rawVal = typeof field.value === 'string' ? field.value.trim() : '';
                if (envKey && rawVal !== '') {
                    updates[envKey] = rawVal;
                }
            });

            const keysToSave = Object.keys(updates);
            if (keysToSave.length === 0) {
                showToast('Hãy nhập ít nhất một giá trị cần cập nhật', 'info');
                return;
            }

            viewState.savingConfig = true;
            viewState.configNotice = '';
            render();
            try {
                const result = await api.updateSystemConfig(updates);
                const updatedKeys = Array.isArray(result?.updated_keys) ? result.updated_keys : keysToSave;

                const [diagRes, pubRes, provRes] = await Promise.allSettled([
                    api.getSystemDiagnostics(),
                    api.getPublicConfig(),
                    api.getProviders(),
                ]);

                const storePatch = {};
                if (diagRes.status === 'fulfilled' && diagRes.value) {
                    viewState.diagnostics = diagRes.value;
                    storePatch.diagnostics = diagRes.value;
                }
                if (pubRes.status === 'fulfilled' && pubRes.value) {
                    storePatch.publicConfig = pubRes.value;
                }
                if (provRes.status === 'fulfilled' && provRes.value) {
                    storePatch.providers = provRes.value;
                }
                if (Object.keys(storePatch).length > 0) {
                    store.setState(storePatch);
                }

                const needsServerReload = updatedKeys.some(
                    (k) => k === 'STORAGE_PROVIDER' || k.startsWith('SUPABASE_')
                );

                if (needsServerReload) {
                    const notice =
                        `Đã cập nhật ${updatedKeys.length} khóa (${updatedKeys.join(', ')}). ` +
                        'Lưu ý: Nếu bạn vừa đổi STORAGE_PROVIDER hoặc SUPABASE_*, hãy khởi động lại server (restart uvicorn) để pool kết nối mới có hiệu lực hoàn toàn.';
                    viewState.configNotice = notice;
                    showToast('Đã lưu cấu hình! Hãy restart server nếu vừa đổi DB/Storage pool.', 'info', 6000);
                } else {
                    viewState.configNotice = `Đã lưu và áp dụng ngay: ${updatedKeys.join(', ')}.`;
                    showToast(`Đã lưu ${updatedKeys.length} cấu hình & áp dụng ngay`, 'success');
                }
            } catch (err) {
                showToast(err?.message || 'Không thể lưu cấu hình .env', 'error');
            } finally {
                viewState.savingConfig = false;
                render();
            }
        });

        container.querySelector('#set-logout-btn')?.addEventListener('click', () => {
            logout();
            loadDiagnosticsAndProfile();
        });

        container.querySelector('#set-profile-form')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const input = container.querySelector('#set-display-name');
            const displayName = input?.value?.trim() || '';
            if (!displayName) {
                showToast('Vui lòng nhập tên hiển thị', 'warning');
                return;
            }
            viewState.savingProfile = true;
            render();
            try {
                const updated = await api.updateAccountProfile({ display_name: displayName });
                viewState.profile = updated;
                store.setState({
                    profile: {
                        ...(store.getState().profile || {}),
                        ...updated,
                        subscription_tier: updated?.tier || updated?.subscription_tier || tier,
                    },
                });
                const authLabel = document.querySelector('#auth-action-btn [data-auth-label]');
                if (authLabel) {
                    authLabel.textContent = `👤 ${updated.display_name || updated.email || displayName}`;
                }
                showToast('Đã cập nhật tên hiển thị', 'success');
                if (handlers?.onProfileUpdated) {
                    handlers.onProfileUpdated(updated);
                }
            } catch (err) {
                showToast(err?.message || 'Không thể cập nhật hồ sơ', 'error');
            } finally {
                viewState.savingProfile = false;
                render();
            }
        });

        container.querySelector('#set-prefs-form')?.addEventListener('submit', (e) => {
            e.preventDefault();
            const nextPrefs = {
                aspectRatio: container.querySelector('#pref-aspect').value,
                targetLang: container.querySelector('#pref-lang').value,
                translationStyle: container.querySelector('#pref-style').value,
                burnSubtitles: Boolean(container.querySelector('#pref-burn-subs').checked),
            };
            savePreferences(nextPrefs);
            showToast('Đã lưu cấu hình Studio mặc định', 'success');
        });

        container.querySelector('#set-cleanup-btn')?.addEventListener('click', async () => {
            if (!store.getState().accessToken) {
                showToast('Vui lòng đăng nhập để dọn thư mục tạm', 'warning');
                openAuthModal();
                return;
            }
            const ok = await confirmDialog({
                title: 'Dọn thư mục tạm',
                message: 'Xóa các thư mục tạm của job đã hoàn tất/cũ? (Dữ liệu Sandbox và các job đang chạy sẽ được giữ nguyên).',
                confirmText: 'Dọn ngay',
            });
            if (!ok) return;

            viewState.cleaningTemp = true;
            render();
            try {
                const res = await api.runSystemCleanup();
                showToast(`Đã dọn ${Number(res?.cleaned_count || 0)} thư mục tạm`, 'success');
                await loadDiagnosticsAndProfile();
            } catch (err) {
                showToast(err?.message || 'Dọn thư mục tạm thất bại', 'error');
            } finally {
                viewState.cleaningTemp = false;
                render();
            }
        });
    }

    let prevToken = store.getState().accessToken;
    activeUnsubscribe = store.subscribe((next) => {
        if (!container.isConnected) {
            if (activeUnsubscribe) {
                activeUnsubscribe();
                activeUnsubscribe = null;
            }
            return;
        }
        if (next.accessToken !== prevToken) {
            prevToken = next.accessToken;
            loadDiagnosticsAndProfile();
        }
    });

    loadDiagnosticsAndProfile();
}
