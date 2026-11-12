/**
 * BGM Mood Library, Custom BGM Upload & Audio Mixing Control Component
 */

import { showToast, store } from '../core/state.js';
import { api } from '../core/api.js';

const FALLBACK_BGM_PRESETS = [
    {
        id: 'lofi_chill',
        name: 'Lo-Fi Chill',
        mood: 'Chill / Storytelling',
        bpm: 78,
        duration_seconds: 15,
        description: 'Nhẹ nhàng, ấm áp, hợp review & kể chuyện',
        tags: ['lofi', 'chill'],
    },
    {
        id: 'cinematic_epic',
        name: 'Cinematic Epic',
        mood: 'Dramatic / Trailer',
        bpm: 100,
        duration_seconds: 15,
        description: 'Hùng tráng, cao trào cho phim & tài liệu',
        tags: ['epic', 'cinematic'],
    },
    {
        id: 'upbeat_tech',
        name: 'Upbeat Tech',
        mood: 'Energetic / Modern',
        bpm: 120,
        duration_seconds: 15,
        description: 'Sôi động, hiện đại, hợp video công nghệ & viral',
        tags: ['tech', 'viral'],
    },
    {
        id: 'corporate_clean',
        name: 'Corporate Clean',
        mood: 'Professional',
        bpm: 108,
        duration_seconds: 15,
        description: 'Trong trẻo, chuyên nghiệp cho tin tức & giáo dục',
        tags: ['clean', 'news'],
    },
    {
        id: 'horror_suspense',
        name: 'Horror Suspense',
        mood: 'Dark / Mystery',
        bpm: 70,
        duration_seconds: 15,
        description: 'Kịch tính, rùng rợn cho trinh thám & bí ẩn',
        tags: ['mystery', 'dark'],
    },
    {
        id: 'emotional_piano',
        name: 'Emotional Piano',
        mood: 'Emotional / Inspiring',
        bpm: 72,
        duration_seconds: 15,
        description: 'Piano sâu lắng, truyền cảm hứng',
        tags: ['piano', 'emotional'],
    },
];

const ALLOWED_BGM_EXTENSIONS = ['.mp3', '.wav', '.m4a', '.aac', '.ogg'];

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function resolvePreviewUrl(presetId) {
    if (typeof api.getBgmPreviewUrl === 'function') {
        return api.getBgmPreviewUrl(presetId);
    }
    return `/api/v1/bgm/presets/${encodeURIComponent(presetId)}/preview`;
}

export function renderBgmMixer(container, initialConfig = {}) {
    if (!container) {
        return {
            getConfig: () => ({
                bgm_preset_id: null,
                bgm_file_key: null,
                bgm_volume: 0.25,
                auto_ducking: true,
            }),
        };
    }

    const mixerState = {
        presets: [...FALLBACK_BGM_PRESETS],
        bgm_preset_id: initialConfig.bgm_preset_id ?? null,
        bgm_file_key: initialConfig.bgm_file_key ?? null,
        uploadedFilename: initialConfig.bgm_file_name ?? null,
        bgm_volume: Number.isFinite(Number(initialConfig.bgm_volume))
            ? Math.min(1, Math.max(0, Number(initialConfig.bgm_volume)))
            : 0.25,
        auto_ducking: initialConfig.auto_ducking !== undefined ? Boolean(initialConfig.auto_ducking) : true,
        playingPresetId: null,
        uploading: false,
    };

    let activeAudio = null;

    function stopPreviewAudio() {
        if (activeAudio) {
            try {
                activeAudio.pause();
                activeAudio.currentTime = 0;
            } catch {
                // Ignore audio stop errors
            }
            activeAudio = null;
        }
        mixerState.playingPresetId = null;
    }

    function render() {
        if (!container.isConnected) {
            stopPreviewAudio();
            return;
        }

        const volPct = Math.round(mixerState.bgm_volume * 100);
        const isNoneSelected = !mixerState.bgm_preset_id && !mixerState.bgm_file_key;

        container.innerHTML = `
            <div class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-4">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                    <div>
                        <h3 class="text-sm font-bold text-white flex items-center gap-2">
                            <span>🎵</span> Nhạc nền &amp; Auto-Ducking (BGM Mixer)
                        </h3>
                        <p class="text-[11px] text-slate-400">
                            Chọn nhạc nền theo mood, nghe thử trực tiếp hoặc tải file nhạc riêng của bạn.
                        </p>
                    </div>
                    <label class="inline-flex items-center gap-2 cursor-pointer rounded-xl bg-[#0B0D12] border border-[#262E42] px-3 py-1.5 hover:border-cyan-500/40 transition">
                        <input type="checkbox" data-bgm-ducking ${mixerState.auto_ducking ? 'checked' : ''}
                            class="w-4 h-4 rounded border-[#262E42] bg-[#121620] text-cyan-500 focus:ring-cyan-500" />
                        <span class="text-xs font-medium text-slate-200">Auto-Ducking <code class="text-[10px] text-cyan-300">sidechaincompress</code></span>
                    </label>
                </div>

                <!-- Preset Grid -->
                <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
                    <button type="button" data-select-preset="none"
                        class="bgm-preset-card rounded-xl border p-3 text-left transition ${
                            isNoneSelected
                                ? 'is-selected bg-violet-500/15 border-violet-400 text-white'
                                : 'bg-[#0B0D12] border-[#262E42] text-slate-300 hover:border-slate-500'
                        }">
                        <div class="flex items-center justify-between gap-2">
                            <span class="text-xs font-bold">🚫 Không thêm BGM</span>
                            ${isNoneSelected ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-violet-500/30 text-violet-200">Đang chọn</span>' : ''}
                        </div>
                        <p class="text-[11px] text-slate-400 mt-1">Chỉ dùng giọng đọc hoặc nhạc nền gốc (nếu có).</p>
                    </button>

                    ${mixerState.presets.map((preset) => {
                        const selected = mixerState.bgm_preset_id === preset.id && !mixerState.bgm_file_key;
                        const playing = mixerState.playingPresetId === preset.id;
                        return `
                            <div data-preset-card="${escapeHtml(preset.id)}"
                                class="bgm-preset-card rounded-xl border p-3 flex flex-col justify-between gap-2 transition cursor-pointer ${
                                    selected
                                        ? 'is-selected bg-cyan-500/15 border-cyan-400 text-white'
                                        : 'bg-[#0B0D12] border-[#262E42] text-slate-300 hover:border-slate-500'
                                }">
                                <div class="space-y-1">
                                    <div class="flex items-center justify-between gap-1.5">
                                        <span class="text-xs font-bold truncate">${escapeHtml(preset.name)}</span>
                                        <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[#191F2E] text-slate-400 border border-[#262E42] shrink-0">
                                            ${escapeHtml(preset.bpm)} BPM
                                        </span>
                                    </div>
                                    <p class="text-[11px] text-slate-400 line-clamp-2">${escapeHtml(preset.description || preset.mood)}</p>
                                </div>
                                <div class="flex items-center justify-between gap-2 pt-1">
                                    <span class="text-[10px] text-cyan-300/90 truncate">${escapeHtml(preset.mood)}</span>
                                    <button type="button" data-preview-preset="${escapeHtml(preset.id)}"
                                        class="px-2 py-1 rounded-lg text-[11px] font-semibold transition shrink-0 ${
                                            playing
                                                ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40'
                                                : 'bg-[#191F2E] hover:bg-[#232B40] text-cyan-300 border border-[#262E42]'
                                        }">
                                        ${playing ? '⏹ Dừng' : '▶ Nghe thử'}
                                    </button>
                                </div>
                            </div>
                        `;
                    }).join('')}
                </div>

                <!-- Custom BGM Upload + Volume Slider -->
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 border-t border-[#262E42]">
                    <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3 flex flex-col justify-between gap-2">
                        <div class="flex items-center justify-between gap-2">
                            <span class="text-xs font-medium text-slate-300">Nhạc nền riêng (.mp3 / .wav)</span>
                            ${mixerState.bgm_file_key ? `
                                <button type="button" data-clear-custom-bgm
                                    class="text-[11px] text-rose-300 hover:text-rose-200">
                                    ✕ Xóa file
                                </button>
                            ` : ''}
                        </div>
                        <div class="flex items-center gap-2">
                            <input type="file" accept=".mp3,.wav,.m4a,.aac,.ogg" data-bgm-file-input class="hidden" />
                            <button type="button" data-bgm-upload-btn
                                ${mixerState.uploading ? 'disabled' : ''}
                                class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-[#191F2E] hover:bg-[#232B40] border border-[#262E42] text-slate-200 transition shrink-0">
                                ${mixerState.uploading ? '⏳ Đang tải...' : '📁 Upload nhạc nền'}
                            </button>
                            <span class="text-[11px] font-mono text-slate-400 truncate">
                                ${mixerState.bgm_file_key
                                    ? `✓ ${escapeHtml(mixerState.uploadedFilename || mixerState.bgm_file_key)}`
                                    : 'Chưa chọn file riêng'}
                            </span>
                        </div>
                    </div>

                    <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3 flex flex-col justify-center gap-2">
                        <div class="flex items-center justify-between text-xs">
                            <span class="font-medium text-slate-300">Âm lượng nhạc nền (BGM Volume)</span>
                            <strong data-bgm-vol-label class="font-mono text-cyan-300">${volPct}%</strong>
                        </div>
                        <input type="range" min="0" max="100" value="${volPct}" data-bgm-vol-slider
                            class="w-full studio-range accent-cyan-400 cursor-pointer" />
                    </div>
                </div>
            </div>
        `;

        wireEvents();
    }

    function wireEvents() {
        const duckToggle = container.querySelector('[data-bgm-ducking]');
        const volSlider = container.querySelector('[data-bgm-vol-slider]');
        const volLabel = container.querySelector('[data-bgm-vol-label]');
        const noneBtn = container.querySelector('[data-select-preset="none"]');
        const fileInput = container.querySelector('[data-bgm-file-input]');
        const uploadBtn = container.querySelector('[data-bgm-upload-btn]');
        const clearCustomBtn = container.querySelector('[data-clear-custom-bgm]');

        duckToggle?.addEventListener('change', (e) => {
            mixerState.auto_ducking = Boolean(e.target.checked);
        });

        volSlider?.addEventListener('input', (e) => {
            const pct = Number(e.target.value);
            mixerState.bgm_volume = Math.min(1, Math.max(0, pct / 100));
            if (volLabel) volLabel.textContent = `${pct}%`;
        });

        noneBtn?.addEventListener('click', () => {
            mixerState.bgm_preset_id = null;
            mixerState.bgm_file_key = null;
            mixerState.uploadedFilename = null;
            render();
        });

        container.querySelectorAll('[data-preset-card]').forEach((card) => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('[data-preview-preset]')) return;
                mixerState.bgm_preset_id = card.dataset.presetCard;
                mixerState.bgm_file_key = null;
                mixerState.uploadedFilename = null;
                render();
            });
        });

        container.querySelectorAll('[data-preview-preset]').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const presetId = btn.dataset.previewPreset;

                if (mixerState.playingPresetId === presetId) {
                    stopPreviewAudio();
                    render();
                    return;
                }

                stopPreviewAudio();
                const audio = new Audio(resolvePreviewUrl(presetId));
                audio.volume = Math.max(0.15, mixerState.bgm_volume || 0.4);
                activeAudio = audio;
                mixerState.playingPresetId = presetId;
                render();

                audio.addEventListener('ended', () => {
                    if (activeAudio === audio) {
                        stopPreviewAudio();
                        render();
                    }
                });
                audio.addEventListener('error', () => {
                    if (activeAudio === audio) {
                        stopPreviewAudio();
                        render();
                    }
                    showToast('Không thể phát âm thanh preview BGM', 'warning');
                });

                try {
                    await audio.play();
                } catch {
                    stopPreviewAudio();
                    render();
                    showToast('Trình duyệt chặn tự phát hoặc chưa có audio preview', 'warning');
                }
            });
        });

        uploadBtn?.addEventListener('click', () => fileInput?.click());

        fileInput?.addEventListener('change', async (e) => {
            const file = e.target.files?.[0];
            fileInput.value = '';
            if (!file) return;

            const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
            if (!ALLOWED_BGM_EXTENSIONS.includes(ext)) {
                showToast(`Định dạng BGM không hỗ trợ (${ext})`, 'error');
                return;
            }
            if (!store.getState().accessToken) {
                showToast('Vui lòng đăng nhập để upload nhạc nền riêng', 'warning');
                document.getElementById('auth-action-btn')?.click();
                return;
            }
            if (typeof api.uploadBgmFile !== 'function') {
                showToast('API upload BGM chưa sẵn sàng', 'error');
                return;
            }

            mixerState.uploading = true;
            render();
            try {
                const res = await api.uploadBgmFile(file);
                mixerState.bgm_file_key = res.bgm_file_key;
                mixerState.uploadedFilename = res.filename || file.name;
                mixerState.bgm_preset_id = null;
                showToast(`Đã tải lên nhạc nền "${mixerState.uploadedFilename}"`, 'success');
            } catch (err) {
                showToast(err?.message || 'Upload nhạc nền thất bại', 'error');
            } finally {
                mixerState.uploading = false;
                render();
            }
        });

        clearCustomBtn?.addEventListener('click', () => {
            mixerState.bgm_file_key = null;
            mixerState.uploadedFilename = null;
            render();
        });
    }

    render();

    if (typeof api.getBgmPresets === 'function') {
        api.getBgmPresets()
            .then((res) => {
                const list = Array.isArray(res) ? res : (Array.isArray(res?.presets) ? res.presets : null);
                if (list && list.length > 0 && container.isConnected) {
                    mixerState.presets = list;
                    render();
                }
            })
            .catch(() => {
                // Keep fallback presets silently
            });
    }

    return {
        getConfig() {
            return {
                bgm_preset_id: mixerState.bgm_file_key ? null : (mixerState.bgm_preset_id || null),
                bgm_file_key: mixerState.bgm_file_key || null,
                bgm_volume: Number(mixerState.bgm_volume.toFixed(2)),
                auto_ducking: Boolean(mixerState.auto_ducking),
            };
        },
        destroy() {
            stopPreviewAudio();
        },
    };
}
