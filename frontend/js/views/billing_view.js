/**
 * Billing, Credit Wallet, Tier Upgrade & Transaction History View
 */

import { store, showToast, confirmDialog } from '../core/state.js';
import { api } from '../core/api.js';
import { openAuthModal, loginWithSandbox } from '../core/auth.js';
import { openVietQRModal } from '../components/vietqr_modal.js';

let activeUnsubscribe = null;

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatVnd(amount) {
    return `${Number(amount || 0).toLocaleString('vi-VN')} đ`;
}

function formatDateTime(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString('vi-VN', {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
        });
    } catch {
        return '—';
    }
}

const TX_STATUS_BADGE = {
    completed: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
    pending: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
    failed: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
    expired: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30',
};

const LEDGER_TYPE_META = {
    signup_bonus: { label: 'Thưởng đăng ký', cls: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30' },
    topup: { label: 'Nạp VietQR', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
    deduction: { label: 'Khấu trừ Render', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
    refund: { label: 'Hoàn Credit', cls: 'bg-violet-500/15 text-violet-300 border-violet-500/30' },
    adjustment: { label: 'Điều chỉnh / Nâng cấp', cls: 'bg-indigo-500/15 text-indigo-300 border-indigo-500/30' },
};

function syncTierBadge(tier) {
    const normalized = String(tier || 'free').toLowerCase();
    const badge = document.getElementById('user-tier-badge');
    if (badge) {
        badge.textContent = normalized.toUpperCase();
        badge.setAttribute('data-tier', normalized);
    }
}

export function renderBillingView(container, handlers = {}) {
    if (!container) return;

    if (activeUnsubscribe) {
        activeUnsubscribe();
        activeUnsubscribe = null;
    }

    const viewState = {
        loading: false,
        upgradingTier: null,
        error: null,
        history: null,
    };

    async function loadHistory() {
        if (!store.getState().accessToken) {
            viewState.history = null;
            viewState.loading = false;
            render();
            return;
        }
        viewState.loading = true;
        viewState.error = null;
        render();

        try {
            const data = await api.getBillingHistory(50);
            viewState.history = data;
            const prevCredits = store.getState().credits || {};
            store.setState({
                credits: {
                    ...prevCredits,
                    balance: Number(data?.balance ?? prevCredits.balance ?? 0),
                    credit_ledger: Array.isArray(data?.credit_ledger)
                        ? data.credit_ledger
                        : (prevCredits.credit_ledger || []),
                },
                profile: store.getState().profile
                    ? { ...store.getState().profile, subscription_tier: data?.tier || store.getState().profile.subscription_tier }
                    : store.getState().profile,
            });
            if (data?.tier) syncTierBadge(data.tier);
        } catch (err) {
            viewState.error = err?.message || 'Không thể tải lịch sử giao dịch & credit.';
        } finally {
            viewState.loading = false;
            render();
        }
    }

    function renderUnauthenticated() {
        const isSandbox = Boolean(store.getState().publicConfig?.local_sandbox_mode);
        container.innerHTML = `
            <div class="rounded-2xl bg-[#121620] border border-[#262E42] p-8 sm:p-10 text-center space-y-4 max-w-2xl mx-auto">
                <div class="text-4xl">💳</div>
                <h2 class="text-lg font-bold text-white">Ví Credit &amp; Lịch sử giao dịch</h2>
                <p class="text-xs sm:text-sm text-slate-400 leading-relaxed">
                    Đăng nhập để xem số dư Credit, lịch sử thanh toán VietQR (SePay / Casso), nhật ký khấu trừ và nâng cấp gói tài khoản.
                </p>
                <div class="flex flex-wrap items-center justify-center gap-3 pt-2">
                    <button type="button" id="bill-login-btn"
                        class="px-5 py-2.5 rounded-xl text-xs sm:text-sm font-semibold bg-violet-600 hover:bg-violet-500 text-white transition">
                        Đăng nhập / JWT Token
                    </button>
                    ${isSandbox ? `
                        <button type="button" id="bill-sandbox-btn"
                            class="px-5 py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-gradient-to-r from-cyan-500 to-violet-600 hover:brightness-110 text-white shadow-lg shadow-cyan-500/20 transition">
                            ⚡ Dùng thử Local Sandbox (500 Credits)
                        </button>
                    ` : ''}
                </div>
            </div>
        `;

        container.querySelector('#bill-login-btn')?.addEventListener('click', () => {
            openAuthModal({ onSuccess: () => loadHistory() });
        });

        const sbBtn = container.querySelector('#bill-sandbox-btn');
        sbBtn?.addEventListener('click', async () => {
            sbBtn.disabled = true;
            sbBtn.textContent = 'Đang kích hoạt Sandbox...';
            try {
                await loginWithSandbox();
                await loadHistory();
            } catch (err) {
                showToast(err?.message || 'Không thể kích hoạt Sandbox', 'error');
                sbBtn.disabled = false;
                sbBtn.textContent = '⚡ Dùng thử Local Sandbox (500 Credits)';
            }
        });
    }

    function render() {
        if (!container.isConnected) return;
        const state = store.getState();
        if (!state.accessToken) {
            renderUnauthenticated();
            return;
        }

        const publicCfg = state.publicConfig || {};
        const vndPerCredit = Number(publicCfg.vnd_per_credit || 1000);
        const proCost = Number(publicCfg.pro_upgrade_credits || 500);
        const enterpriseCost = Number(publicCfg.enterprise_upgrade_credits || 1500);

        const balance = Number(viewState.history?.balance ?? state.credits?.balance ?? 0);
        const currentTier = String(
            viewState.history?.tier || state.profile?.subscription_tier || state.profile?.tier || 'free'
        ).toLowerCase();
        const transactions = Array.isArray(viewState.history?.transactions) ? viewState.history.transactions : [];
        const ledger = Array.isArray(viewState.history?.credit_ledger)
            ? viewState.history.credit_ledger
            : (Array.isArray(state.credits?.credit_ledger) ? state.credits.credit_ledger : []);

        const tiers = [
            {
                id: 'free',
                name: 'Free Starter',
                priceLabel: '0 Credits',
                features: ['Pipeline Translator & Faceless cơ bản', 'Có Watermark góc video', 'Tối đa batch theo cấu hình'],
                accent: 'border-[#262E42] bg-[#0B0D12]/80',
                cost: 0,
            },
            {
                id: 'pro',
                name: 'Pro Creator',
                priceLabel: `${proCost.toLocaleString('vi-VN')} Credits`,
                features: ['Tắt hoàn toàn Watermark', 'Ưu tiên hàng đợi render', 'Full AI Marketing Kit & Thumbnail'],
                accent: 'border-cyan-500/40 bg-gradient-to-b from-cyan-500/10 to-[#0B0D12]',
                cost: proCost,
            },
            {
                id: 'enterprise',
                name: 'Enterprise Studio',
                priceLabel: `${enterpriseCost.toLocaleString('vi-VN')} Credits`,
                features: ['Không Watermark + Batch tối đa', 'Tối ưu xử lý đa luồng', 'Hỗ trợ đầy đủ TTS & Visual AI'],
                accent: 'border-violet-500/45 bg-gradient-to-b from-violet-500/12 to-[#0B0D12]',
                cost: enterpriseCost,
            },
        ];

        container.innerHTML = `
            <div class="space-y-6">
                <!-- Header & Balance Card -->
                <div class="grid grid-cols-1 lg:grid-cols-12 gap-5">
                    <div class="lg:col-span-5 rounded-2xl bg-gradient-to-br from-[#161E31] via-[#121620] to-[#0E131D] border border-[#262E42] p-6 flex flex-col justify-between gap-4 shadow-xl">
                        <div class="flex items-start justify-between gap-3">
                            <div>
                                <div class="text-xs font-semibold uppercase tracking-wider text-cyan-400">Ví Credit Studio</div>
                                <div class="mt-2 flex items-baseline gap-2">
                                    <span class="text-3xl sm:text-4xl font-extrabold font-mono text-white">${balance.toLocaleString('vi-VN')}</span>
                                    <span class="text-sm text-slate-400 font-medium">Credits</span>
                                </div>
                                <p class="text-xs text-slate-400 mt-1">
                                    Quy đổi chuẩn: <strong class="text-slate-200">${formatVnd(vndPerCredit)}</strong> / 1 Credit
                                </p>
                            </div>
                            <span class="px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider bg-violet-500/20 text-violet-300 border border-violet-500/40">
                                Gói ${escapeHtml(currentTier.toUpperCase())}
                            </span>
                        </div>

                        <div class="flex flex-wrap items-center gap-2.5 pt-2">
                            <button type="button" id="bill-topup-btn"
                                class="flex-1 min-w-[160px] py-2.5 px-4 rounded-xl text-xs sm:text-sm font-bold bg-gradient-to-r from-cyan-500 to-violet-500 hover:brightness-110 text-slate-950 shadow-lg shadow-cyan-500/20 transition">
                                💳 Nạp Credit qua VietQR
                            </button>
                            <button type="button" id="bill-refresh-btn" ${viewState.loading ? 'disabled' : ''}
                                class="py-2.5 px-3.5 rounded-xl text-xs font-semibold bg-[#191F2E] hover:bg-[#232B40] border border-[#262E42] text-slate-200 transition">
                                ${viewState.loading ? '⏳ Đang tải...' : '↻ Làm mới'}
                            </button>
                        </div>
                    </div>

                    <!-- Subscription Tiers -->
                    <div class="lg:col-span-7 rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-3">
                        <div class="flex items-center justify-between">
                            <div>
                                <h3 class="text-sm font-bold text-white">Gói Tài Khoản &amp; Tắt Watermark</h3>
                                <p class="text-xs text-slate-400">Nâng cấp trực tiếp bằng số dư Credit hiện có</p>
                            </div>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
                            ${tiers.map((t) => {
                                const isCurrent = currentTier === t.id;
                                const isDowngrade = (currentTier === 'enterprise' && t.id !== 'enterprise') || (currentTier === 'pro' && t.id === 'free');
                                const isBusy = viewState.upgradingTier === t.id;
                                return `
                                    <div class="rounded-xl border ${t.accent} p-4 flex flex-col justify-between gap-3">
                                        <div class="space-y-2">
                                            <div class="flex items-center justify-between gap-1">
                                                <span class="text-xs font-bold text-white">${escapeHtml(t.name)}</span>
                                                ${isCurrent ? '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">Đang dùng</span>' : ''}
                                            </div>
                                            <div class="text-base font-extrabold font-mono text-cyan-300">${escapeHtml(t.priceLabel)}</div>
                                            <ul class="space-y-1 text-[11px] text-slate-400">
                                                ${t.features.map((f) => `<li>• ${escapeHtml(f)}</li>`).join('')}
                                            </ul>
                                        </div>

                                        ${t.id !== 'free' ? `
                                            <button type="button" data-upgrade-tier="${t.id}" data-upgrade-cost="${t.cost}"
                                                ${isCurrent || isDowngrade || Boolean(viewState.upgradingTier) ? 'disabled' : ''}
                                                class="w-full py-2 px-3 rounded-lg text-xs font-semibold transition ${
                                                    isCurrent
                                                        ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 cursor-default'
                                                        : isDowngrade
                                                        ? 'bg-[#191F2E] text-slate-500 border border-[#262E42] cursor-not-allowed'
                                                        : 'bg-violet-600 hover:bg-violet-500 text-white shadow-sm'
                                                }">
                                                ${isCurrent ? 'Đã kích hoạt' : isBusy ? '⏳ Đang nâng cấp...' : isDowngrade ? 'Gói thấp hơn' : 'Nâng cấp bằng Credit'}
                                            </button>
                                        ` : `
                                            <div class="py-2 px-3 rounded-lg text-center text-[11px] font-medium bg-[#191F2E]/60 text-slate-400 border border-[#262E42]">
                                                Mặc định khi tạo tài khoản
                                            </div>
                                        `}
                                    </div>
                                `;
                            }).join('')}
                        </div>
                    </div>
                </div>

                ${viewState.error ? `
                    <div class="rounded-xl bg-rose-500/10 border border-rose-500/30 px-4 py-3 text-xs text-rose-300">
                        ⚠️ ${escapeHtml(viewState.error)}
                    </div>
                ` : ''}

                <!-- Tables Grid -->
                <div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
                    <!-- VietQR Transactions Table -->
                    <section class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-3">
                        <div class="flex items-center justify-between">
                            <h3 class="text-sm font-bold text-white flex items-center gap-2">
                                <span>🏦</span> Lịch sử giao dịch VietQR
                            </h3>
                            <span class="text-xs font-mono text-slate-400">${transactions.length} giao dịch</span>
                        </div>

                        <div class="overflow-x-auto studio-scroll rounded-xl border border-[#262E42]">
                            <table class="w-full text-left text-xs">
                                <thead class="bg-[#0B0D12] text-slate-400 uppercase text-[10px] tracking-wider">
                                    <tr>
                                        <th class="px-3 py-2.5">Mã GD</th>
                                        <th class="px-3 py-2.5">Cổng</th>
                                        <th class="px-3 py-2.5 text-right">Số tiền</th>
                                        <th class="px-3 py-2.5 text-right">Credits</th>
                                        <th class="px-3 py-2.5">Trạng thái</th>
                                        <th class="px-3 py-2.5">Thời gian</th>
                                    </tr>
                                </thead>
                                <tbody class="divide-y divide-[#262E42] bg-[#121620]">
                                    ${transactions.length === 0 ? `
                                        <tr>
                                            <td colspan="6" class="px-4 py-8 text-center text-slate-500">
                                                Chưa có giao dịch nạp VietQR nào.
                                            </td>
                                        </tr>
                                    ` : transactions.map((tx) => {
                                        const st = String(tx.status || 'pending').toLowerCase();
                                        const cls = TX_STATUS_BADGE[st] || TX_STATUS_BADGE.pending;
                                        return `
                                            <tr class="hover:bg-[#191F2E]/60 transition">
                                                <td class="px-3 py-2.5 font-mono text-cyan-300">${escapeHtml(tx.reference_code || String(tx.id || '').slice(0, 8))}</td>
                                                <td class="px-3 py-2.5 uppercase text-slate-300">${escapeHtml(tx.gateway || 'sepay')}</td>
                                                <td class="px-3 py-2.5 text-right font-mono text-white">${formatVnd(tx.amount_vnd)}</td>
                                                <td class="px-3 py-2.5 text-right font-mono text-emerald-300">+${Number(tx.credits_granted || 0).toLocaleString('vi-VN')}</td>
                                                <td class="px-3 py-2.5">
                                                    <span class="px-2 py-0.5 rounded-full text-[10px] font-semibold border ${cls}">
                                                        ${escapeHtml(st)}
                                                    </span>
                                                </td>
                                                <td class="px-3 py-2.5 text-slate-400 whitespace-nowrap">${formatDateTime(tx.created_at)}</td>
                                            </tr>
                                        `;
                                    }).join('')}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    <!-- Credit Ledger Table -->
                    <section class="rounded-2xl bg-[#121620] border border-[#262E42] p-5 space-y-3">
                        <div class="flex items-center justify-between">
                            <h3 class="text-sm font-bold text-white flex items-center gap-2">
                                <span>📒</span> Nhật ký biến động Credit
                            </h3>
                            <span class="text-xs font-mono text-slate-400">${ledger.length} mục</span>
                        </div>

                        <div class="overflow-x-auto studio-scroll rounded-xl border border-[#262E42]">
                            <table class="w-full text-left text-xs">
                                <thead class="bg-[#0B0D12] text-slate-400 uppercase text-[10px] tracking-wider">
                                    <tr>
                                        <th class="px-3 py-2.5">Loại</th>
                                        <th class="px-3 py-2.5">Mô tả</th>
                                        <th class="px-3 py-2.5 text-right">Biến động</th>
                                        <th class="px-3 py-2.5 text-right">Số dư sau</th>
                                        <th class="px-3 py-2.5">Thời gian</th>
                                    </tr>
                                </thead>
                                <tbody class="divide-y divide-[#262E42] bg-[#121620]">
                                    ${ledger.length === 0 ? `
                                        <tr>
                                            <td colspan="5" class="px-4 py-8 text-center text-slate-500">
                                                Chưa có biến động Credit nào.
                                            </td>
                                        </tr>
                                    ` : ledger.map((entry) => {
                                        const typeKey = String(entry.entry_type || 'adjustment').toLowerCase();
                                        const meta = LEDGER_TYPE_META[typeKey] || LEDGER_TYPE_META.adjustment;
                                        const amt = Number(entry.amount || 0);
                                        const amtCls = amt >= 0 ? 'text-emerald-400' : 'text-rose-400';
                                        const amtPrefix = amt > 0 ? '+' : '';
                                        return `
                                            <tr class="hover:bg-[#191F2E]/60 transition">
                                                <td class="px-3 py-2.5 whitespace-nowrap">
                                                    <span class="px-2 py-0.5 rounded-full text-[10px] font-semibold border ${meta.cls}">
                                                        ${escapeHtml(meta.label)}
                                                    </span>
                                                </td>
                                                <td class="px-3 py-2.5 text-slate-300 max-w-[200px] truncate" title="${escapeHtml(entry.description || '')}">
                                                    ${escapeHtml(entry.description || '—')}
                                                </td>
                                                <td class="px-3 py-2.5 text-right font-mono font-bold ${amtCls}">
                                                    ${amtPrefix}${amt.toLocaleString('vi-VN')}
                                                </td>
                                                <td class="px-3 py-2.5 text-right font-mono text-slate-200">
                                                    ${entry.balance_after !== undefined && entry.balance_after !== null ? Number(entry.balance_after).toLocaleString('vi-VN') : '—'}
                                                </td>
                                                <td class="px-3 py-2.5 text-slate-400 whitespace-nowrap">${formatDateTime(entry.created_at)}</td>
                                            </tr>
                                        `;
                                    }).join('')}
                                </tbody>
                            </table>
                        </div>
                    </section>
                </div>
            </div>
        `;

        container.querySelector('#bill-topup-btn')?.addEventListener('click', () => {
            openVietQRModal({
                onTopupCompleted: () => loadHistory(),
            });
        });

        container.querySelector('#bill-refresh-btn')?.addEventListener('click', loadHistory);

        container.querySelectorAll('[data-upgrade-tier]').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const targetTier = btn.dataset.upgradeTier;
                const cost = Number(btn.dataset.upgradeCost || 0);
                const ok = await confirmDialog({
                    title: `Nâng cấp gói ${targetTier.toUpperCase()}`,
                    message: `Xác nhận khấu trừ ${cost.toLocaleString('vi-VN')} Credits để nâng cấp lên gói ${targetTier.toUpperCase()} (tắt Watermark)?`,
                    confirmText: 'Nâng cấp ngay',
                });
                if (!ok) return;

                viewState.upgradingTier = targetTier;
                render();
                try {
                    const res = await api.upgradeTier(targetTier);
                    const nextTier = res?.subscription_tier || targetTier;
                    syncTierBadge(nextTier);
                    store.setState({
                        profile: { ...(store.getState().profile || {}), subscription_tier: nextTier },
                        credits: { ...(store.getState().credits || {}), balance: Number(res?.new_balance ?? balance) },
                    });
                    showToast(`Đã nâng cấp thành công lên gói ${nextTier.toUpperCase()}!`, 'success');
                    if (handlers?.onProfileUpdated) {
                        handlers.onProfileUpdated(store.getState().profile);
                    }
                    await loadHistory();
                } catch (err) {
                    showToast(err?.message || 'Nâng cấp gói thất bại', 'error');
                } finally {
                    viewState.upgradingTier = null;
                    render();
                }
            });
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
            loadHistory();
        }
    });

    loadHistory();
}
