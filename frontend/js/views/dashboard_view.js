/**
 * Dashboard View — Hero Mode Selector & Project History / Render Queue
 */

import { store, showToast, confirmDialog, promptDialog } from '../core/state.js';
import { api } from '../core/api.js';
import { loginWithSandbox } from '../core/auth.js';

const STATUS_META = {
    PENDING: {
        label: 'Chờ xử lý',
        badgeClass: 'bg-slate-500/15 text-slate-300 border-slate-500/30',
        dotClass: 'bg-slate-400',
        cancellable: true,
        active: true,
    },
    PROCESSING: {
        label: 'Đang xử lý AI',
        badgeClass: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30',
        dotClass: 'bg-cyan-400 animate-pulse',
        cancellable: true,
        active: true,
    },
    AWAITING_REVIEW: {
        label: 'Chờ duyệt HITL',
        badgeClass: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
        dotClass: 'bg-amber-400 animate-pulse',
        cancellable: true,
        active: false,
    },
    RENDERING: {
        label: 'Đang Render',
        badgeClass: 'bg-violet-500/15 text-violet-300 border-violet-500/30',
        dotClass: 'bg-violet-400 animate-pulse',
        cancellable: true,
        active: true,
    },
    COMPLETED: {
        label: 'Hoàn tất',
        badgeClass: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
        dotClass: 'bg-emerald-400',
        cancellable: false,
        active: false,
    },
    FAILED: {
        label: 'Thất bại',
        badgeClass: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
        dotClass: 'bg-rose-400',
        cancellable: false,
        active: false,
    },
    CANCELLED: {
        label: 'Đã hủy',
        badgeClass: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30',
        dotClass: 'bg-zinc-500',
        cancellable: false,
        active: false,
    },
};

const MODE_META = {
    translator: {
        label: 'Translator & Dub',
        pillClass: 'bg-cyan-500/10 text-cyan-300 border-cyan-500/25',
    },
    faceless: {
        label: 'Faceless AI',
        pillClass: 'bg-violet-500/10 text-violet-300 border-violet-500/25',
    },
};

let activeUnsubscribe = null;
const filterState = {
    mode: 'all',
    status: 'all',
    sort: 'newest',
    search: '',
    refreshing: false,
};

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function triggerBlobDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || 'download';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) {
        return '—';
    }
    const sec = Math.max(0, Math.round(Number(seconds)));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function formatRelativeDate(isoString) {
    if (!isoString) return '—';
    try {
        const date = new Date(isoString);
        return date.toLocaleString('vi-VN', {
            day: '2-digit',
            month: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
        });
    } catch {
        return '—';
    }
}

function getFilteredProjects(projects) {
    const list = Array.isArray(projects) ? projects : [];
    const filtered = list.filter((p) => {
        if (filterState.mode !== 'all' && p.mode !== filterState.mode) return false;
        if (filterState.status !== 'all' && p.status !== filterState.status) return false;
        if (filterState.search.trim()) {
            const q = filterState.search.trim().toLowerCase();
            const title = (p.title || '').toLowerCase();
            const stage = (p.current_stage || '').toLowerCase();
            const id = String(p.id || '').toLowerCase();
            if (!title.includes(q) && !stage.includes(q) && !id.includes(q)) return false;
        }
        return true;
    });

    return [...filtered].sort((a, b) => {
        if (filterState.sort === 'title_asc') {
            return String(a.title || '').localeCompare(String(b.title || ''), 'vi');
        }
        const ta = new Date(a.created_at || 0).getTime();
        const tb = new Date(b.created_at || 0).getTime();
        return filterState.sort === 'oldest' ? ta - tb : tb - ta;
    });
}

async function openProjectDetailModal(project, handlers = {}) {
    const root = document.getElementById('project-detail-modal-root');
    if (!root) return;

    const renderModal = (detail, loading = false) => {
        const st = STATUS_META[detail.status] || STATUS_META.PENDING;
        const scenes = Array.isArray(detail.scenes) ? detail.scenes : [];
        const inputs = detail.source_inputs || {};
        const cfg = detail.pipeline_config || {};

        root.innerHTML = `
            <div id="pd-modal-backdrop" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm">
                <div class="w-full max-w-3xl max-h-[88vh] flex flex-col rounded-2xl bg-[#121620] border border-[#262E42] shadow-2xl overflow-hidden" role="dialog" aria-modal="true">
                    <div class="px-5 py-4 border-b border-[#262E42] flex items-center justify-between gap-3 bg-[#151A27]">
                        <div class="min-w-0">
                            <div class="flex items-center gap-2">
                                <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold border ${st.badgeClass}">
                                    <span class="w-1.5 h-1.5 rounded-full ${st.dotClass}"></span>
                                    ${escapeHtml(detail.status)}
                                </span>
                                <span class="text-xs font-mono text-slate-400">#${escapeHtml(String(detail.id || '').slice(0, 8))}</span>
                            </div>
                            <h3 class="text-base font-bold text-white truncate mt-1">${escapeHtml(detail.title || 'Project Detail')}</h3>
                        </div>
                        <button type="button" id="pd-close-btn" class="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-[#191F2E]">✕</button>
                    </div>

                    <div class="flex-1 overflow-y-auto studio-scroll p-5 space-y-4 text-xs">
                        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
                            <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3">
                                <div class="text-slate-400">Mode</div>
                                <div class="font-semibold text-white mt-0.5 uppercase">${escapeHtml(detail.mode)}</div>
                            </div>
                            <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3">
                                <div class="text-slate-400">Tỷ lệ</div>
                                <div class="font-mono font-semibold text-white mt-0.5">${escapeHtml(detail.aspect_ratio || '9:16')}</div>
                            </div>
                            <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3">
                                <div class="text-slate-400">Thời lượng / Tiến độ</div>
                                <div class="font-mono font-semibold text-cyan-300 mt-0.5">${formatDuration(detail.duration_seconds)} · ${Number(detail.progress_pct || 0)}%</div>
                            </div>
                            <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3">
                                <div class="text-slate-400">Credits</div>
                                <div class="font-mono font-semibold text-amber-300 mt-0.5">${detail.credits_charged > 0 ? `${detail.credits_charged} đã trừ` : `${detail.credits_estimated || 0} dự kiến`}</div>
                            </div>
                        </div>

                        ${detail.current_stage || detail.error_message ? `
                            <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3.5 space-y-1.5">
                                ${detail.current_stage ? `<div><span class="text-slate-400">Stage hiện tại:</span> <code class="text-cyan-300">${escapeHtml(detail.current_stage)}</code></div>` : ''}
                                ${detail.error_message ? `<div class="text-rose-300">⚠️ Lỗi: ${escapeHtml(detail.error_message)}</div>` : ''}
                            </div>
                        ` : ''}

                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3.5 space-y-2">
                                <div class="font-bold text-slate-200">Nguồn đầu vào (source_inputs)</div>
                                <pre class="text-[11px] font-mono text-slate-300 whitespace-pre-wrap break-all max-h-40 overflow-y-auto studio-scroll">${escapeHtml(JSON.stringify(inputs, null, 2))}</pre>
                            </div>
                            <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3.5 space-y-2">
                                <div class="font-bold text-slate-200">Cấu hình Pipeline (pipeline_config)</div>
                                <pre class="text-[11px] font-mono text-slate-300 whitespace-pre-wrap break-all max-h-40 overflow-y-auto studio-scroll">${escapeHtml(JSON.stringify(cfg, null, 2))}</pre>
                            </div>
                        </div>

                        <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-3.5 space-y-2">
                            <div class="flex items-center justify-between">
                                <span class="font-bold text-slate-200">Tóm tắt Phân cảnh / Phụ đề (${scenes.length} scenes)</span>
                                ${loading ? '<span class="text-cyan-400">⏳ Đang tải chi tiết...</span>' : ''}
                            </div>
                            ${scenes.length === 0 ? `
                                <p class="text-slate-500 py-2">${loading ? 'Đang tải danh sách scene...' : 'Chưa có scene nào được tạo.'}</p>
                            ` : `
                                <div class="space-y-1.5 max-h-48 overflow-y-auto studio-scroll pr-1">
                                    ${scenes.map((sc, idx) => `
                                        <div class="flex items-start gap-2.5 rounded-lg bg-[#121620] border border-[#262E42] px-3 py-2">
                                            <span class="font-mono text-[11px] text-cyan-300 shrink-0">#${idx + 1} (${(Number(sc.start_ms || 0) / 1000).toFixed(1)}s–${(Number(sc.end_ms || 0) / 1000).toFixed(1)}s)</span>
                                            <span class="text-slate-200 flex-1 line-clamp-2">${escapeHtml(sc.edited_text || sc.narration_text || sc.original_text || '—')}</span>
                                        </div>
                                    `).join('')}
                                </div>
                            `}
                        </div>
                    </div>

                    <div class="px-5 py-3.5 border-t border-[#262E42] bg-[#151A27] flex flex-wrap items-center justify-between gap-2">
                        <span class="text-[11px] text-slate-400">Tạo lúc: ${formatRelativeDate(detail.created_at)}</span>
                        <div class="flex flex-wrap gap-2">
                            ${detail.status === 'AWAITING_REVIEW' ? `
                                <button type="button" data-pd-action="review" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-amber-500 hover:bg-amber-400 text-slate-950">✏️ Mở Review Editor</button>
                            ` : ''}
                            ${detail.status === 'COMPLETED' ? `
                                <button type="button" data-pd-action="play" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 hover:bg-emerald-400 text-slate-950">▶️ Xem Video</button>
                            ` : ''}
                            ${['PENDING', 'PROCESSING', 'RENDERING'].includes(detail.status) ? `
                                <button type="button" data-pd-action="watch" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-cyan-500/20 text-cyan-300 border border-cyan-500/30">📡 Theo dõi SSE</button>
                            ` : ''}
                            <button type="button" id="pd-done-btn" class="px-3.5 py-1.5 rounded-lg text-xs font-medium bg-[#191F2E] hover:bg-[#232B40] border border-[#262E42] text-slate-200">Đóng</button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        const close = () => {
            root.innerHTML = '';
        };
        root.querySelector('#pd-close-btn')?.addEventListener('click', close);
        root.querySelector('#pd-done-btn')?.addEventListener('click', close);
        root.querySelector('#pd-modal-backdrop')?.addEventListener('click', (ev) => {
            if (ev.target.id === 'pd-modal-backdrop') close();
        });
        root.querySelector('[data-pd-action="review"]')?.addEventListener('click', () => {
            close();
            store.setState({ activeProject: detail });
            (handlers?.onReviewProject || handlers?.onOpenReview)?.(detail);
        });
        root.querySelector('[data-pd-action="play"]')?.addEventListener('click', () => {
            close();
            (handlers?.onPlayVideo || handlers?.onPreviewVideo)?.(detail);
        });
        root.querySelector('[data-pd-action="watch"]')?.addEventListener('click', () => {
            close();
            (handlers?.onWatchProject || handlers?.onOpenProgress)?.(detail);
        });
    };

    renderModal(project, true);
    try {
        const detail = await api.getProject(project.id);
        if (root.innerHTML) renderModal(detail, false);
    } catch {
        if (root.innerHTML) renderModal(project, false);
    }
}

function navigateTo(viewName, handlers) {
    if (handlers?.onNavigate) {
        handlers.onNavigate(viewName);
        return;
    }
    const navBtn = document.querySelector(`[data-nav="${viewName}"]`);
    if (navBtn) {
        navBtn.click();
    } else {
        store.setState({ activeView: viewName });
    }
}

async function refreshProjectsList(showSuccessToast = false) {
    if (!store.getState().accessToken) {
        showToast('Vui lòng đăng nhập để tải danh sách dự án', 'warning');
        return;
    }
    filterState.refreshing = true;
    try {
        const projects = await api.listProjects({ limit: 50 });
        store.setState({ projects: Array.isArray(projects) ? projects : [] });
        if (showSuccessToast) {
            showToast('Đã làm mới danh sách dự án', 'success');
        }
    } catch (err) {
        showToast(err?.message || 'Không thể tải danh sách dự án', 'error');
    } finally {
        filterState.refreshing = false;
    }
}

function renderProjectCard(project, completedBatchCounts = {}) {
    const status = STATUS_META[project.status] || STATUS_META.PENDING;
    const modeInfo = MODE_META[project.mode] || {
        label: project.mode || 'unknown',
        pillClass: 'bg-slate-500/10 text-slate-300 border-slate-500/25',
    };
    const progress = Math.min(100, Math.max(0, Number(project.progress_pct || 0)));
    const creditsDisplay = project.credits_charged > 0
        ? `${project.credits_charged} cr đã trừ`
        : `${project.credits_estimated || 0} cr dự kiến`;
    const thumbnailUrl = project.pipeline_config?.thumbnail_url || null;
    const batchCompletedCount = project.batch_id ? (completedBatchCounts[String(project.batch_id)] || 0) : 0;

    const actionButtons = [];

    if (status.active) {
        actionButtons.push(`
            <button type="button" data-action="watch" data-project-id="${escapeHtml(project.id)}"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 border border-cyan-500/30 transition">
                <span>📡</span> Theo dõi
            </button>
        `);
    }

    if (project.status === 'AWAITING_REVIEW') {
        actionButtons.push(`
            <button type="button" data-action="review" data-project-id="${escapeHtml(project.id)}"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-amber-500 hover:bg-amber-400 text-slate-950 transition shadow-sm shadow-amber-500/20">
                <span>✏️</span> Chỉnh sửa & Duyệt
            </button>
        `);
    }

    if (project.status === 'COMPLETED') {
        actionButtons.push(`
            <button type="button" data-action="play" data-project-id="${escapeHtml(project.id)}"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 hover:bg-emerald-400 text-slate-950 transition shadow-sm shadow-emerald-500/20">
                <span>▶️</span> Xem & Tải Video
            </button>
        `);
        actionButtons.push(`
            <button type="button" data-action="export-srt" data-project-id="${escapeHtml(project.id)}"
                title="Tải phụ đề .SRT"
                class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-mono font-medium bg-[#121620] hover:bg-[#222A3E] text-slate-200 border border-[#262E42] transition">
                .SRT
            </button>
        `);
        actionButtons.push(`
            <button type="button" data-action="export-ass" data-project-id="${escapeHtml(project.id)}"
                title="Tải phụ đề Karaoke .ASS"
                class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-mono font-medium bg-[#121620] hover:bg-[#222A3E] text-cyan-300 border border-[#262E42] transition">
                .ASS
            </button>
        `);
        if (project.batch_id && batchCompletedCount > 1) {
            actionButtons.push(`
                <button type="button" data-action="download-batch" data-project-id="${escapeHtml(project.id)}" data-batch-id="${escapeHtml(project.batch_id)}"
                    class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-semibold bg-indigo-500/15 hover:bg-indigo-500/25 text-indigo-300 border border-indigo-500/30 transition">
                    📦 Tải Batch ZIP
                </button>
            `);
        }
    }

    if (status.cancellable) {
        actionButtons.push(`
            <button type="button" data-action="cancel" data-project-id="${escapeHtml(project.id)}"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 border border-rose-500/25 transition">
                <span>✕</span> Hủy
            </button>
        `);
    }

    if (project.status === 'FAILED' || project.status === 'CANCELLED') {
        actionButtons.push(`
            <button type="button" data-action="retry" data-project-id="${escapeHtml(project.id)}"
                class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 border border-cyan-500/30 transition">
                🔄 Chạy lại
            </button>
        `);
    }

    actionButtons.push(`
        <button type="button" data-action="detail" data-project-id="${escapeHtml(project.id)}"
            title="Xem chi tiết dự án"
            class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-[#121620] hover:bg-[#222A3E] text-slate-300 border border-[#262E42] transition">
            🔍 Chi tiết
        </button>
    `);

    actionButtons.push(`
        <button type="button" data-action="rename" data-project-id="${escapeHtml(project.id)}"
            title="Đổi tên dự án"
            class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-[#121620] hover:bg-[#222A3E] text-slate-300 border border-[#262E42] transition">
            ✏️ Đổi tên
        </button>
    `);

    actionButtons.push(`
        <button type="button" data-action="duplicate" data-project-id="${escapeHtml(project.id)}"
            title="Nhân bản dự án"
            class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-[#121620] hover:bg-[#222A3E] text-slate-300 border border-[#262E42] transition">
            📋 Nhân bản
        </button>
    `);

    if (project.status !== 'PROCESSING' && project.status !== 'RENDERING') {
        actionButtons.push(`
            <button type="button" data-action="delete" data-project-id="${escapeHtml(project.id)}"
                title="Xóa dự án"
                class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 border border-rose-500/25 transition">
                🗑️ Xóa
            </button>
        `);
    }

    return `
        <div class="group rounded-xl bg-[#191F2E]/80 hover:bg-[#191F2E] border border-[#262E42] hover:border-violet-500/40 p-4 transition-all">
            <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                <div class="min-w-0 flex-1 flex items-start gap-3.5">
                    ${project.status === 'COMPLETED' && thumbnailUrl ? `
                        <div class="w-16 h-20 rounded-lg overflow-hidden bg-[#0B0D12] border border-[#262E42] shrink-0 hidden sm:block">
                            <img src="${escapeHtml(thumbnailUrl)}" alt="Thumbnail"
                                class="w-full h-full object-cover"
                                onerror="this.parentElement.style.display='none'" />
                        </div>
                    ` : ''}
                    <div class="min-w-0 flex-1 space-y-2">
                        <div class="flex flex-wrap items-center gap-2">
                            <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold border ${status.badgeClass}">
                                <span class="w-1.5 h-1.5 rounded-full ${status.dotClass}"></span>
                                ${escapeHtml(project.status)} · ${escapeHtml(status.label)}
                            </span>
                            <span class="px-2 py-0.5 rounded-md text-[11px] font-medium border ${modeInfo.pillClass}">
                                ${escapeHtml(modeInfo.label)}
                            </span>
                            <span class="px-2 py-0.5 rounded-md text-[11px] font-mono bg-[#121620] text-slate-300 border border-[#262E42]">
                                ${escapeHtml(project.aspect_ratio || '9:16')}
                            </span>
                            ${project.batch_id ? `<span class="px-2 py-0.5 rounded-md text-[10px] font-mono bg-indigo-500/10 text-indigo-300 border border-indigo-500/20">Batch</span>` : ''}
                        </div>

                        <div class="flex items-baseline gap-3">
                            <h4 class="text-sm sm:text-base font-semibold text-white truncate">
                                ${escapeHtml(project.title || 'Untitled Project')}
                            </h4>
                            <span class="text-[11px] font-mono text-slate-500 hidden sm:inline">
                                #${escapeHtml(String(project.id || '').slice(0, 8))}
                            </span>
                        </div>

                        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-400">
                            <span>⏱️ Thời lượng: <strong class="text-slate-200">${formatDuration(project.duration_seconds)}</strong></span>
                            <span>💎 Credits: <strong class="text-slate-200">${escapeHtml(creditsDisplay)}</strong></span>
                            <span>🕒 Tạo lúc: <span class="text-slate-300">${formatRelativeDate(project.created_at)}</span></span>
                            ${project.current_stage ? `<span>⚙️ Stage: <code class="text-cyan-300">${escapeHtml(project.current_stage)}</code></span>` : ''}
                        </div>

                        ${project.error_message ? `
                            <p class="text-xs text-rose-300 bg-rose-500/10 border border-rose-500/20 rounded-lg px-2.5 py-1.5 line-clamp-2">
                                ⚠️ ${escapeHtml(project.error_message)}
                            </p>
                        ` : ''}
                    </div>
                </div>

                <div class="flex flex-col sm:flex-row lg:flex-col items-stretch sm:items-center lg:items-end justify-between gap-3 shrink-0 lg:w-72">
                    <div class="w-full">
                        <div class="flex items-center justify-between text-[11px] mb-1">
                            <span class="text-slate-400">Tiến độ</span>
                            <span class="font-mono font-semibold text-slate-200">${progress}%</span>
                        </div>
                        <div class="w-full h-1.5 rounded-full bg-[#0B0D12] overflow-hidden border border-[#262E42]">
                            <div class="h-full rounded-full transition-all duration-500 ${
                                project.status === 'FAILED'
                                    ? 'bg-rose-500'
                                    : project.status === 'COMPLETED'
                                    ? 'bg-emerald-500'
                                    : 'bg-gradient-to-r from-violet-500 to-cyan-400'
                            }" style="width: ${progress}%"></div>
                        </div>
                    </div>

                    <div class="flex flex-wrap items-center justify-end gap-1.5">
                        ${actionButtons.join('')}
                    </div>
                </div>
            </div>
        </div>
    `;
}

function renderProjectListSection(listRoot) {
    if (!listRoot) return;
    const state = store.getState();
    const allProjects = Array.isArray(state.projects) ? state.projects : [];
    const filtered = getFilteredProjects(allProjects);

    if (!state.accessToken) {
        const isSandboxEnabled = Boolean(state.publicConfig?.local_sandbox_mode);
        listRoot.innerHTML = `
            <div class="rounded-xl border border-dashed border-[#262E42] bg-[#121620]/60 p-8 text-center space-y-3">
                <div class="text-3xl">🔐</div>
                <h4 class="text-sm font-semibold text-slate-200">Đăng nhập để xem lịch sử & hàng đợi render</h4>
                <p class="text-xs text-slate-400 max-w-md mx-auto">
                    Sử dụng tài khoản Supabase hoặc dán JWT token ở nút Đăng nhập trên thanh công cụ để đồng bộ dự án và credit.
                </p>
                <div class="flex flex-wrap items-center justify-center gap-2.5 pt-1">
                    <button type="button" data-action="open-auth"
                        class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold bg-violet-600 hover:bg-violet-500 text-white transition">
                        Đăng nhập ngay
                    </button>
                    ${isSandboxEnabled ? `
                        <button type="button" data-action="sandbox-login"
                            class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-bold bg-gradient-to-r from-cyan-500 to-violet-600 hover:brightness-110 text-white shadow-lg shadow-cyan-500/20 transition">
                            ⚡ Dùng thử Local Sandbox (500 Credits)
                        </button>
                    ` : ''}
                </div>
            </div>
        `;
        return;
    }

    if (allProjects.length === 0) {
        listRoot.innerHTML = `
            <div class="rounded-xl border border-dashed border-[#262E42] bg-[#121620]/60 p-8 text-center space-y-2">
                <div class="text-3xl">🎬</div>
                <h4 class="text-sm font-semibold text-slate-200">Chưa có dự án nào</h4>
                <p class="text-xs text-slate-400">
                    Chọn một trong hai Studio ở trên để bắt đầu dịch video hàng loạt hoặc tạo video Faceless AI.
                </p>
            </div>
        `;
        return;
    }

    if (filtered.length === 0) {
        listRoot.innerHTML = `
            <div class="rounded-xl border border-[#262E42] bg-[#121620]/60 p-6 text-center text-xs text-slate-400">
                Không có dự án nào khớp với bộ lọc hiện tại.
            </div>
        `;
        return;
    }

    const completedBatchCounts = {};
    for (const p of allProjects) {
        if (p.status === 'COMPLETED' && p.batch_id) {
            const bid = String(p.batch_id);
            completedBatchCounts[bid] = (completedBatchCounts[bid] || 0) + 1;
        }
    }

    listRoot.innerHTML = `<div class="space-y-3">${filtered.map((p) => renderProjectCard(p, completedBatchCounts)).join('')}</div>`;
}

export function renderDashboardView(container, handlers = {}) {
    if (!container) return;

    if (activeUnsubscribe) {
        activeUnsubscribe();
        activeUnsubscribe = null;
    }

    const state = store.getState();
    const projects = Array.isArray(state.projects) ? state.projects : [];
    const activeCount = projects.filter((p) => ['PENDING', 'PROCESSING', 'RENDERING'].includes(p.status)).length;
    const reviewCount = projects.filter((p) => p.status === 'AWAITING_REVIEW').length;
    const completedCount = projects.filter((p) => p.status === 'COMPLETED').length;

    container.innerHTML = `
        <div class="space-y-6">
            <!-- Hero Mode Selector -->
            <section class="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <div data-launch="translator" role="button" tabindex="0"
                    class="group relative overflow-hidden rounded-2xl bg-gradient-to-br from-[#161C2C] via-[#121620] to-[#0F172A] border border-[#262E42] hover:border-cyan-400/60 p-6 cursor-pointer transition-all shadow-lg">
                    <div class="absolute -right-8 -top-8 w-40 h-40 rounded-full bg-cyan-500/10 blur-2xl group-hover:bg-cyan-500/20 transition"></div>
                    <div class="relative flex flex-col justify-between h-full space-y-4">
                        <div class="flex items-start justify-between gap-4">
                            <div class="space-y-1.5">
                                <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">
                                    ⚡ Pipeline 1 · Batch Ready
                                </span>
                                <h2 class="text-lg sm:text-xl font-bold text-white group-hover:text-cyan-300 transition">
                                    AI Video Translator & Auto-Dubber
                                </h2>
                                <p class="text-xs sm:text-sm text-slate-400 leading-relaxed">
                                    Dán danh sách link TikTok / Douyin / Shorts / Reels hoặc upload nhiều file cùng lúc. Tách nhạc nền, nhận diện từng từ (Word-level STT), dịch khớp nhịp & lồng tiếng thích ứng tốc độ.
                                </p>
                            </div>
                            <div class="w-11 h-11 rounded-xl bg-cyan-500/15 border border-cyan-500/30 flex items-center justify-center text-xl shrink-0">
                                🌐
                            </div>
                        </div>

                        <div class="flex flex-wrap items-center justify-between gap-3 pt-2 border-t border-[#262E42]/80">
                            <div class="flex flex-wrap gap-1.5 text-[11px] text-slate-300">
                                <span class="px-2 py-0.5 rounded bg-[#191F2E] border border-[#262E42]">Multi-URL Batch</span>
                                <span class="px-2 py-0.5 rounded bg-[#191F2E] border border-[#262E42]">Word-level STT</span>
                                <span class="px-2 py-0.5 rounded bg-[#191F2E] border border-[#262E42]">Adaptive Dubbing</span>
                            </div>
                            <span class="text-xs font-semibold text-cyan-300 group-hover:translate-x-1 transition-transform">
                                Mở Studio →
                            </span>
                        </div>
                    </div>
                </div>

                <div data-launch="faceless" role="button" tabindex="0"
                    class="group relative overflow-hidden rounded-2xl bg-gradient-to-br from-[#1A162C] via-[#121620] to-[#131126] border border-[#262E42] hover:border-violet-400/60 p-6 cursor-pointer transition-all shadow-lg">
                    <div class="absolute -right-8 -top-8 w-40 h-40 rounded-full bg-violet-500/10 blur-2xl group-hover:bg-violet-500/20 transition"></div>
                    <div class="relative flex flex-col justify-between h-full space-y-4">
                        <div class="flex items-start justify-between gap-4">
                            <div class="space-y-1.5">
                                <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-violet-500/15 text-violet-300 border border-violet-500/30">
                                    ✨ Pipeline 2 · Auto Storyboard
                                </span>
                                <h2 class="text-lg sm:text-xl font-bold text-white group-hover:text-violet-300 transition">
                                    AI Faceless Video Generator
                                </h2>
                                <p class="text-xs sm:text-sm text-slate-400 leading-relaxed">
                                    Biến Chủ đề, Kịch bản hoặc Link bài báo thành video ngắn 3–5 giây mỗi cảnh. Tự động tìm stock Pexels/Pixabay hoặc tạo ảnh AI, hiệu ứng Ken Burns, ducking & phụ đề .ass Karaoke.
                                </p>
                            </div>
                            <div class="w-11 h-11 rounded-xl bg-violet-500/15 border border-violet-500/30 flex items-center justify-center text-xl shrink-0">
                                🎞️
                            </div>
                        </div>

                        <div class="flex flex-wrap items-center justify-between gap-3 pt-2 border-t border-[#262E42]/80">
                            <div class="flex flex-wrap gap-1.5 text-[11px] text-slate-300">
                                <span class="px-2 py-0.5 rounded bg-[#191F2E] border border-[#262E42]">Topic / Script / URL</span>
                                <span class="px-2 py-0.5 rounded bg-[#191F2E] border border-[#262E42]">Storyboard 3–5s</span>
                                <span class="px-2 py-0.5 rounded bg-[#191F2E] border border-[#262E42]">.ass Karaoke</span>
                            </div>
                            <span class="text-xs font-semibold text-violet-300 group-hover:translate-x-1 transition-transform">
                                Mở Studio →
                            </span>
                        </div>
                    </div>
                </div>
            </section>

            <!-- Summary Counters & Queue Controls -->
            <section class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-4">
                <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
                    <div>
                        <h3 class="text-base font-bold text-white flex items-center gap-2">
                            <span>📂</span> Lịch sử dự án & Hàng đợi Render
                        </h3>
                        <p id="dashboard-stats-line" class="text-xs text-slate-400 mt-0.5">
                            Đang chạy: <strong class="text-cyan-300">${activeCount}</strong> ·
                            Chờ duyệt HITL: <strong class="text-amber-300">${reviewCount}</strong> ·
                            Hoàn tất: <strong class="text-emerald-300">${completedCount}</strong>
                        </p>
                    </div>

                    <div class="flex flex-wrap items-center gap-2">
                        <input id="dash-search-input" type="search" value="${escapeHtml(filterState.search)}"
                            placeholder="Tìm tên dự án..."
                            class="px-3 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-violet-500 w-44" />

                        <select id="dash-mode-filter"
                            class="px-2.5 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-slate-200 focus:outline-none focus:border-violet-500">
                            <option value="all" ${filterState.mode === 'all' ? 'selected' : ''}>Tất cả Mode</option>
                            <option value="translator" ${filterState.mode === 'translator' ? 'selected' : ''}>Translator & Dub</option>
                            <option value="faceless" ${filterState.mode === 'faceless' ? 'selected' : ''}>Faceless AI</option>
                        </select>

                        <select id="dash-status-filter"
                            class="px-2.5 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-slate-200 focus:outline-none focus:border-violet-500">
                            <option value="all" ${filterState.status === 'all' ? 'selected' : ''}>Tất cả Trạng thái</option>
                            <option value="PENDING" ${filterState.status === 'PENDING' ? 'selected' : ''}>PENDING</option>
                            <option value="PROCESSING" ${filterState.status === 'PROCESSING' ? 'selected' : ''}>PROCESSING</option>
                            <option value="AWAITING_REVIEW" ${filterState.status === 'AWAITING_REVIEW' ? 'selected' : ''}>AWAITING_REVIEW</option>
                            <option value="RENDERING" ${filterState.status === 'RENDERING' ? 'selected' : ''}>RENDERING</option>
                            <option value="COMPLETED" ${filterState.status === 'COMPLETED' ? 'selected' : ''}>COMPLETED</option>
                            <option value="FAILED" ${filterState.status === 'FAILED' ? 'selected' : ''}>FAILED</option>
                            <option value="CANCELLED" ${filterState.status === 'CANCELLED' ? 'selected' : ''}>CANCELLED</option>
                        </select>

                        <select id="dash-sort-filter"
                            class="px-2.5 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-slate-200 focus:outline-none focus:border-violet-500">
                            <option value="newest" ${filterState.sort === 'newest' ? 'selected' : ''}>Mới nhất</option>
                            <option value="oldest" ${filterState.sort === 'oldest' ? 'selected' : ''}>Cũ nhất</option>
                            <option value="title_asc" ${filterState.sort === 'title_asc' ? 'selected' : ''}>Tên A–Z</option>
                        </select>

                        <button id="dash-refresh-btn" type="button"
                            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[#191F2E] hover:bg-[#222A3E] border border-[#262E42] text-xs font-medium text-slate-200 transition">
                            <span>↻</span> Làm mới
                        </button>
                    </div>
                </div>

                <div id="dashboard-project-list"></div>
            </section>
        </div>
    `;

    const listRoot = container.querySelector('#dashboard-project-list');
    const statsLine = container.querySelector('#dashboard-stats-line');
    renderProjectListSection(listRoot);

    container.querySelectorAll('[data-launch]').forEach((card) => {
        const launch = () => navigateTo(card.dataset.launch, handlers);
        card.addEventListener('click', launch);
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                launch();
            }
        });
    });

    const searchInput = container.querySelector('#dash-search-input');
    const modeFilter = container.querySelector('#dash-mode-filter');
    const statusFilter = container.querySelector('#dash-status-filter');
    const sortFilter = container.querySelector('#dash-sort-filter');
    const refreshBtn = container.querySelector('#dash-refresh-btn');

    searchInput?.addEventListener('input', (e) => {
        filterState.search = e.target.value;
        renderProjectListSection(listRoot);
    });

    modeFilter?.addEventListener('change', (e) => {
        filterState.mode = e.target.value;
        renderProjectListSection(listRoot);
    });

    statusFilter?.addEventListener('change', (e) => {
        filterState.status = e.target.value;
        renderProjectListSection(listRoot);
    });

    sortFilter?.addEventListener('change', (e) => {
        filterState.sort = e.target.value;
        renderProjectListSection(listRoot);
    });

    refreshBtn?.addEventListener('click', async () => {
        refreshBtn.disabled = true;
        refreshBtn.classList.add('opacity-60');
        try {
            if (handlers?.onRefreshProjects) {
                await handlers.onRefreshProjects();
            } else {
                await refreshProjectsList(true);
            }
        } finally {
            refreshBtn.disabled = false;
            refreshBtn.classList.remove('opacity-60');
        }
    });

    listRoot?.addEventListener('click', async (e) => {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const action = btn.dataset.action;

        if (action === 'open-auth') {
            if (handlers?.onOpenAuth) {
                handlers.onOpenAuth();
            } else {
                document.getElementById('auth-action-btn')?.click();
            }
            return;
        }

        if (action === 'sandbox-login') {
            btn.disabled = true;
            btn.textContent = 'Đang kích hoạt Sandbox...';
            try {
                await loginWithSandbox();
            } catch (err) {
                showToast(err?.message || 'Không thể kích hoạt Sandbox', 'error');
                btn.disabled = false;
                btn.textContent = '⚡ Dùng thử Local Sandbox (500 Credits)';
            }
            return;
        }

        const projectId = btn.dataset.projectId;
        const currentProjects = store.getState().projects || [];
        const project = currentProjects.find((p) => String(p.id) === String(projectId));
        if (!project) return;

        if (action === 'watch') {
            if (handlers?.onWatchProject) {
                handlers.onWatchProject(project);
            } else if (handlers?.onOpenProgress) {
                handlers.onOpenProgress(project);
            } else {
                store.setState({ activeProject: project });
                showToast('Đang mở Progress Console...', 'info');
            }
            return;
        }

        if (action === 'review') {
            btn.disabled = true;
            try {
                const detail = await api.getProject(project.id);
                store.setState({ activeProject: detail });
                if (handlers?.onReviewProject) {
                    handlers.onReviewProject(detail);
                } else if (handlers?.onOpenReview) {
                    handlers.onOpenReview(detail);
                }
            } catch (err) {
                showToast(err?.message || 'Không thể tải chi tiết scene để duyệt', 'error');
            } finally {
                btn.disabled = false;
            }
            return;
        }

        if (action === 'play') {
            if (handlers?.onPlayVideo) {
                handlers.onPlayVideo(project);
            } else if (handlers?.onPreviewVideo) {
                handlers.onPreviewVideo(project);
            } else if (project.output_video_url) {
                window.open(project.output_video_url, '_blank', 'noopener,noreferrer');
            } else {
                showToast('Video chưa có đường dẫn đầu ra', 'warning');
            }
            return;
        }

        if (action === 'export-srt' || action === 'export-ass') {
            const fmt = action === 'export-srt' ? 'srt' : 'ass';
            if (typeof api.exportSubtitles !== 'function') {
                showToast('Tính năng xuất phụ đề chưa sẵn sàng', 'error');
                return;
            }
            btn.disabled = true;
            btn.classList.add('opacity-50');
            try {
                const { blob, filename } = await api.exportSubtitles(project.id, fmt);
                triggerBlobDownload(blob, filename || `subtitles_${project.id}.${fmt}`);
                showToast(`Đã tải phụ đề .${fmt.toUpperCase()}`, 'success');
            } catch (err) {
                showToast(err?.message || `Không thể xuất phụ đề .${fmt.toUpperCase()}`, 'error');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-50');
            }
            return;
        }

        if (action === 'download-batch') {
            const batchId = btn.dataset.batchId || project.batch_id;
            if (!batchId || typeof api.downloadBatchZip !== 'function') {
                showToast('Tính năng tải Batch ZIP chưa sẵn sàng', 'error');
                return;
            }
            btn.disabled = true;
            btn.classList.add('opacity-50');
            try {
                const { blob, filename } = await api.downloadBatchZip(batchId);
                triggerBlobDownload(blob, filename || `batch_${batchId}.zip`);
                showToast('Đã tải xuống gói Batch ZIP', 'success');
            } catch (err) {
                showToast(err?.message || 'Không thể tải Batch ZIP', 'error');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-50');
            }
            return;
        }

        if (action === 'cancel') {
            btn.disabled = true;
            btn.classList.add('opacity-50');
            try {
                const updated = await api.cancelProject(project.id);
                const nextList = (store.getState().projects || []).map((item) =>
                    String(item.id) === String(project.id) ? updated : item
                );
                store.setState({ projects: nextList });
                showToast(`Đã hủy dự án "${project.title}"`, 'success');
                if (handlers?.onProjectCancelled) {
                    handlers.onProjectCancelled(updated);
                }
            } catch (err) {
                showToast(err?.message || 'Không thể hủy dự án', 'error');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-50');
            }
            return;
        }

        if (action === 'detail') {
            openProjectDetailModal(project, handlers);
            return;
        }

        if (action === 'rename') {
            const nextTitle = await promptDialog({
                title: 'Đổi tên dự án',
                label: 'Tên dự án mới',
                value: project.title || '',
                placeholder: 'Nhập tên dự án...',
                confirmText: 'Lưu tên mới',
            });
            if (!nextTitle || !nextTitle.trim() || nextTitle.trim() === project.title) return;

            btn.disabled = true;
            try {
                const updated = await api.updateProject(project.id, { title: nextTitle.trim() });
                const nextList = (store.getState().projects || []).map((item) =>
                    String(item.id) === String(project.id) ? updated : item
                );
                store.setState({ projects: nextList });
                showToast('Đã đổi tên dự án', 'success');
            } catch (err) {
                showToast(err?.message || 'Không thể đổi tên dự án', 'error');
            } finally {
                btn.disabled = false;
            }
            return;
        }

        if (action === 'duplicate') {
            btn.disabled = true;
            btn.classList.add('opacity-50');
            try {
                const cloned = await api.duplicateProject(project.id);
                const nextList = [cloned, ...(store.getState().projects || [])];
                store.setState({ projects: nextList });
                showToast(`Đã nhân bản "${cloned.title}"`, 'success');
                if (handlers?.onDuplicateProject) {
                    handlers.onDuplicateProject(cloned);
                }
            } catch (err) {
                showToast(err?.message || 'Không thể nhân bản dự án', 'error');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-50');
            }
            return;
        }

        if (action === 'retry') {
            btn.disabled = true;
            btn.classList.add('opacity-50');
            try {
                const retried = await api.retryProject(project.id);
                const nextList = (store.getState().projects || []).map((item) =>
                    String(item.id) === String(project.id) ? retried : item
                );
                store.setState({ projects: nextList, activeProject: retried });
                showToast(`Đã khởi chạy lại "${retried.title}"`, 'success');
                if (handlers?.onWatchProject) {
                    handlers.onWatchProject(retried);
                } else if (handlers?.onRetryProject) {
                    handlers.onRetryProject(retried);
                }
            } catch (err) {
                showToast(err?.message || 'Không thể chạy lại dự án', 'error');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-50');
            }
            return;
        }

        if (action === 'delete') {
            const confirmed = await confirmDialog({
                title: 'Xóa dự án',
                message: `Bạn có chắc chắn muốn xóa vĩnh viễn dự án "${project.title}"? Thao tác này không thể hoàn tác.`,
                confirmText: 'Xóa dự án',
                danger: true,
            });
            if (!confirmed) return;

            btn.disabled = true;
            btn.classList.add('opacity-50');
            try {
                await api.deleteProject(project.id);
                const nextList = (store.getState().projects || []).filter(
                    (item) => String(item.id) !== String(project.id)
                );
                store.setState({ projects: nextList });
                showToast(`Đã xóa dự án "${project.title}"`, 'success');
                if (handlers?.onDeleteProject) {
                    handlers.onDeleteProject(project.id);
                }
            } catch (err) {
                showToast(err?.message || 'Không thể xóa dự án', 'error');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-50');
            }
        }
    });

    let prevProjectsRef = state.projects;
    let prevToken = state.accessToken;
    activeUnsubscribe = store.subscribe((nextState) => {
        if (!container.isConnected) {
            if (activeUnsubscribe) {
                activeUnsubscribe();
                activeUnsubscribe = null;
            }
            return;
        }
        if (nextState.projects !== prevProjectsRef || nextState.accessToken !== prevToken) {
            prevProjectsRef = nextState.projects;
            prevToken = nextState.accessToken;
            const list = Array.isArray(nextState.projects) ? nextState.projects : [];
            if (statsLine) {
                const act = list.filter((p) => ['PENDING', 'PROCESSING', 'RENDERING'].includes(p.status)).length;
                const rev = list.filter((p) => p.status === 'AWAITING_REVIEW').length;
                const cmp = list.filter((p) => p.status === 'COMPLETED').length;
                statsLine.innerHTML = `
                    Đang chạy: <strong class="text-cyan-300">${act}</strong> ·
                    Chờ duyệt HITL: <strong class="text-amber-300">${rev}</strong> ·
                    Hoàn tất: <strong class="text-emerald-300">${cmp}</strong>
                `;
            }
            renderProjectListSection(listRoot);
        }
    });
}
