/**
 * Stage 4 Interactive Component: VietQR Top-Up & Credit Wallet Modal
 * Displays balance, lifetime_purchased, lifetime_spent, credit_ledger history,
 * preset/custom VND top-up packages, SePay/Casso gateway selector, dynamic VietQR,
 * copyable transfer memo, and 3-second automatic transaction status polling.
 */

import { api } from "../core/api.js";
import { openAuthModal } from "../core/auth.js";
import { showToast, store } from "../core/state.js";

const PRESET_AMOUNTS = [50000, 100000, 200000, 500000];
const POLL_INTERVAL_MS = 3000;

let pollTimerId = null;
let keydownHandler = null;
let modalActive = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatVnd(amount) {
  return `${Number(amount || 0).toLocaleString("vi-VN")} đ`;
}

function formatDateTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("vi-VN", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "—";
  }
}

function stopPolling() {
  if (pollTimerId !== null) {
    clearInterval(pollTimerId);
    pollTimerId = null;
  }
}

export function closeVietQRModal() {
  modalActive = false;
  stopPolling();
  if (keydownHandler) {
    document.removeEventListener("keydown", keydownHandler);
    keydownHandler = null;
  }
  const root = document.getElementById("vietqr-modal-root");
  if (root) {
    root.innerHTML = "";
  }
}

const ENTRY_TYPE_STYLE = {
  signup_bonus: { label: "Thưởng đăng ký", cls: "text-cyan-300 bg-cyan-500/15 border-cyan-500/30" },
  topup: { label: "Nạp VietQR", cls: "text-emerald-300 bg-emerald-500/15 border-emerald-500/30" },
  deduction: { label: "Sử dụng", cls: "text-amber-300 bg-amber-500/15 border-amber-500/30" },
  refund: { label: "Hoàn Credit", cls: "text-violet-300 bg-violet-500/15 border-violet-500/30" },
  adjustment: { label: "Điều chỉnh", cls: "text-slate-300 bg-slate-500/15 border-slate-500/30" },
};

/**
 * Open the VietQR Credit Wallet & Top-Up Modal inside #vietqr-modal-root.
 * @param {object} [callbacks]
 * @param {(tx: object, credits: object) => void} [callbacks.onTopupCompleted]
 */
export function openVietQRModal(callbacks = {}) {
  const root = document.getElementById("vietqr-modal-root");
  if (!root) return;

  if (!store.getState().accessToken) {
    showToast("Vui lòng đăng nhập để xem ví Credit và nạp tiền", "warning");
    openAuthModal();
    return;
  }

  closeVietQRModal();
  modalActive = true;

  const vndPerCredit = Math.max(1, Number(store.getState().publicConfig?.vnd_per_credit || 1000));
  const proCost = Number(store.getState().publicConfig?.pro_upgrade_credits || 500);
  const enterpriseCost = Number(store.getState().publicConfig?.enterprise_upgrade_credits || 1500);
  let selectedAmount = 100000;
  let selectedGateway = "sepay";
  let currentIntent = null;
  let creditsData = store.getState().credits || {
    balance: 0,
    lifetime_purchased: 0,
    lifetime_spent: 0,
    credit_ledger: [],
  };
  let loadingCredits = true;
  let creatingIntent = false;
  let upgradingTier = null;
  let pollError = "";

  const resolveCurrentTier = () => {
    const st = store.getState();
    return String(
      st.profile?.subscription_tier ||
        st.user?.user_metadata?.subscription_tier ||
        st.user?.app_metadata?.subscription_tier ||
        "free"
    ).toLowerCase();
  };

  const startPolling = (transactionId) => {
    stopPolling();
    if (!transactionId || !modalActive) return;

    pollTimerId = setInterval(async () => {
      if (!modalActive || !currentIntent || String(currentIntent.transaction_id) !== String(transactionId)) {
        stopPolling();
        return;
      }

      try {
        const tx = await api.getTransactionStatus(transactionId);
        if (!modalActive) return;
        pollError = "";

        if (tx?.status === "completed") {
          stopPolling();
          let refreshedCredits = creditsData;
          try {
            refreshedCredits = await api.getBillingCredits();
            store.setState({ credits: refreshedCredits });
          } catch {
            // Keep existing credits if refresh momentarily fails
          }
          showToast(
            `Thanh toán thành công! Đã cộng +${tx.credits_amount || currentIntent.credits_amount} Credits vào ví.`,
            "success",
            5500
          );
          callbacks.onTopupCompleted?.(tx, refreshedCredits);
          closeVietQRModal();
          return;
        }

        if (tx?.status === "failed" || tx?.status === "expired") {
          stopPolling();
          currentIntent = { ...currentIntent, status: tx.status };
          showToast(`Giao dịch ${tx.reference_code || ""} đã ${tx.status}.`, "warning");
          render();
        }
      } catch (err) {
        pollError = err?.message || "Đang kiểm tra trạng thái giao dịch...";
      }
    }, POLL_INTERVAL_MS);
  };

  const renderLedgerRows = () => {
    const rows = Array.isArray(creditsData?.credit_ledger) ? creditsData.credit_ledger : [];
    if (loadingCredits && rows.length === 0) {
      return `
        <div class="py-6 text-center text-xs text-slate-400">
          Đang tải lịch sử Credit...
        </div>
      `;
    }
    if (rows.length === 0) {
      return `
        <div class="py-6 text-center text-xs text-slate-500">
          Chưa có giao dịch Credit nào.
        </div>
      `;
    }
    return rows
      .slice(0, 15)
      .map((entry) => {
        const meta = ENTRY_TYPE_STYLE[entry.entry_type] || ENTRY_TYPE_STYLE.adjustment;
        const amt = Number(entry.amount || 0);
        const amtClass = amt >= 0 ? "text-emerald-400" : "text-rose-400";
        const amtText = amt >= 0 ? `+${amt}` : `${amt}`;
        return `
          <div class="flex items-center justify-between gap-3 py-2.5 border-b border-[#262E42]/60 last:border-b-0 text-xs">
            <div class="min-w-0 space-y-0.5">
              <div class="flex items-center gap-2">
                <span class="px-2 py-0.5 rounded-full text-[10px] font-semibold border ${meta.cls}">
                  ${escapeHtml(meta.label)}
                </span>
                <span class="text-[11px] text-slate-500">${formatDateTime(entry.created_at)}</span>
              </div>
              <div class="text-slate-300 truncate">${escapeHtml(entry.description || entry.entry_type)}</div>
            </div>
            <div class="text-right shrink-0">
              <div class="font-mono font-bold ${amtClass}">${amtText} cr</div>
              <div class="text-[10px] font-mono text-slate-500">Sau GD: ${Number(entry.balance_after || 0)}</div>
            </div>
          </div>
        `;
      })
      .join("");
  };

  const render = () => {
    if (!modalActive) return;
    const estCredits = Math.floor(Number(selectedAmount || 0) / vndPerCredit);
    const currentTier = resolveCurrentTier();

    root.innerHTML = `
      <div
        id="vietqr-modal-backdrop"
        class="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-4 bg-black/80 backdrop-blur-sm overflow-y-auto"
      >
        <div
          class="w-full max-w-4xl rounded-2xl bg-[#121620] border border-[#262E42] shadow-2xl overflow-hidden my-auto"
          role="dialog"
          aria-modal="true"
          aria-labelledby="vietqr-modal-title"
        >
          <!-- Modal Header -->
          <div class="px-6 py-4 border-b border-[#262E42] bg-[#151A27] flex items-center justify-between gap-4">
            <div>
              <h2 id="vietqr-modal-title" class="text-base sm:text-lg font-bold text-white flex items-center gap-2">
                <span class="text-amber-400">💳</span>
                Ví Credit &amp; Nạp tự động VietQR
              </h2>
              <p class="text-xs text-slate-400 mt-0.5">
                Tỷ giá: <span class="font-mono text-cyan-300">${formatVnd(vndPerCredit)}</span> = 1 Credit · Cộng tự động trong vài giây qua SePay / Casso
              </p>
            </div>
            <button
              type="button"
              id="vietqr-modal-close"
              class="p-2 rounded-xl text-slate-400 hover:text-white hover:bg-[#191F2E] transition"
              aria-label="Đóng"
            >
              ✕
            </button>
          </div>

          <div class="p-5 sm:p-6 space-y-5 max-h-[84vh] overflow-y-auto studio-scroll">
            <!-- Wallet Stats -->
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div class="rounded-xl bg-gradient-to-br from-violet-600/20 to-cyan-500/15 border border-violet-500/35 p-4">
                <div class="text-[11px] uppercase tracking-wider text-slate-300 font-semibold">Số dư hiện tại</div>
                <div class="mt-1 text-2xl font-mono font-extrabold text-white">
                  ${Number(creditsData.balance || 0).toLocaleString("vi-VN")}
                  <span class="text-xs font-normal text-cyan-300">Credits</span>
                </div>
              </div>
              <div class="rounded-xl bg-[#191F2E] border border-[#262E42] p-4">
                <div class="text-[11px] uppercase tracking-wider text-slate-400 font-semibold">Tổng đã nạp</div>
                <div class="mt-1 text-xl font-mono font-bold text-emerald-400">
                  ${Number(creditsData.lifetime_purchased || 0).toLocaleString("vi-VN")}
                  <span class="text-xs font-normal text-slate-400">Credits</span>
                </div>
              </div>
              <div class="rounded-xl bg-[#191F2E] border border-[#262E42] p-4">
                <div class="text-[11px] uppercase tracking-wider text-slate-400 font-semibold">Tổng đã sử dụng</div>
                <div class="mt-1 text-xl font-mono font-bold text-amber-300">
                  ${Number(creditsData.lifetime_spent || 0).toLocaleString("vi-VN")}
                  <span class="text-xs font-normal text-slate-400">Credits</span>
                </div>
              </div>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-12 gap-5">
              <!-- Left: Top-up Packages & Gateway / Active QR -->
              <div class="lg:col-span-7 space-y-4">
                ${
                  currentIntent
                    ? `
                  <!-- Active VietQR Payment Card -->
                  <div class="rounded-2xl bg-[#0B0D12] border border-cyan-500/40 p-5 space-y-4">
                    <div class="flex items-center justify-between gap-2">
                      <div class="flex items-center gap-2">
                        <span class="w-2.5 h-2.5 rounded-full bg-cyan-400 pulse-dot"></span>
                        <span class="text-xs font-bold uppercase tracking-wider text-cyan-300">
                          Đang chờ thanh toán (${escapeHtml(currentIntent.gateway)})
                        </span>
                      </div>
                      <button
                        type="button"
                        id="vietqr-change-package-btn"
                        class="text-xs text-slate-400 hover:text-white underline"
                      >
                        Chọn gói khác
                      </button>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-12 gap-4 items-center">
                      <div class="sm:col-span-5 flex justify-center">
                        <div class="p-2.5 rounded-2xl bg-white shadow-xl">
                          <img
                            src="${escapeHtml(currentIntent.qr_image_url)}"
                            alt="VietQR Payment Code"
                            class="w-44 h-44 object-contain"
                          />
                        </div>
                      </div>

                      <div class="sm:col-span-7 space-y-2.5 text-xs">
                        <div class="rounded-xl bg-[#121620] border border-[#262E42] p-3 space-y-1.5">
                          <div class="flex justify-between gap-2">
                            <span class="text-slate-400">Ngân hàng (BIN):</span>
                            <span class="font-mono font-semibold text-white">${escapeHtml(currentIntent.bank_bin || "—")}</span>
                          </div>
                          <div class="flex justify-between gap-2">
                            <span class="text-slate-400">Số tài khoản:</span>
                            <span class="font-mono font-semibold text-white">${escapeHtml(currentIntent.account_no || "—")}</span>
                          </div>
                          <div class="flex justify-between gap-2">
                            <span class="text-slate-400">Chủ tài khoản:</span>
                            <span class="font-semibold text-white text-right">${escapeHtml(currentIntent.account_name || "—")}</span>
                          </div>
                          <div class="flex justify-between gap-2 pt-1 border-t border-[#262E42]">
                            <span class="text-slate-400">Số tiền:</span>
                            <span class="font-mono font-bold text-emerald-400">${formatVnd(currentIntent.amount_vnd)} (+${currentIntent.credits_amount} cr)</span>
                          </div>
                        </div>

                        <div class="rounded-xl bg-amber-500/10 border border-amber-500/35 p-3 space-y-1.5">
                          <div class="text-[11px] font-semibold text-amber-300">
                            Nội dung chuyển khoản (Bắt buộc chính xác):
                          </div>
                          <div class="flex items-center justify-between gap-2">
                            <code id="vietqr-memo-code" class="font-mono text-sm font-extrabold text-white tracking-wider">
                              ${escapeHtml(currentIntent.reference_code)}
                            </code>
                            <button
                              type="button"
                              id="vietqr-copy-memo-btn"
                              class="px-2.5 py-1 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 text-[11px] font-bold transition shrink-0"
                            >
                              Sao chép
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>

                    <div class="flex items-center justify-between text-[11px] text-slate-400 pt-1">
                      <span>Tự động kiểm tra trạng thái mỗi 3 giây...</span>
                      <span class="font-mono text-cyan-300">${escapeHtml(currentIntent.status)}</span>
                    </div>
                    ${
                      pollError
                        ? `<div class="text-[11px] text-amber-300">${escapeHtml(pollError)}</div>`
                        : ""
                    }
                  </div>
                `
                    : `
                  <!-- Package Selector -->
                  <div class="rounded-2xl bg-[#0B0D12] border border-[#262E42] p-5 space-y-4">
                    <div>
                      <div class="text-xs font-bold text-white mb-2.5">Chọn gói nạp nhanh (VND)</div>
                      <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
                        ${PRESET_AMOUNTS.map((amt) => {
                          const active = Number(selectedAmount) === amt;
                          const cr = Math.floor(amt / vndPerCredit);
                          return `
                            <button
                              type="button"
                              data-preset-amount="${amt}"
                              class="p-3 rounded-xl border text-left transition ${
                                active
                                  ? "bg-violet-500/20 border-violet-400 text-white shadow-lg shadow-violet-500/15"
                                  : "bg-[#121620] border-[#262E42] text-slate-300 hover:border-slate-500"
                              }"
                            >
                              <div class="font-mono text-xs font-bold">${formatVnd(amt)}</div>
                              <div class="text-[11px] text-cyan-300 mt-0.5">+${cr} Credits</div>
                            </button>
                          `;
                        }).join("")}
                      </div>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <div>
                        <label for="vietqr-custom-amount" class="block text-xs font-semibold text-slate-300 mb-1.5">
                          Hoặc nhập số tiền (VND)
                        </label>
                        <input
                          id="vietqr-custom-amount"
                          type="number"
                          min="${vndPerCredit}"
                          step="1000"
                          value="${selectedAmount}"
                          class="w-full px-3.5 py-2.5 rounded-xl bg-[#121620] border border-[#262E42] text-sm font-mono text-white focus:outline-none focus:border-cyan-400"
                        />
                      </div>

                      <div>
                        <label for="vietqr-gateway-select" class="block text-xs font-semibold text-slate-300 mb-1.5">
                          Cổng xác nhận tự động
                        </label>
                        <select
                          id="vietqr-gateway-select"
                          class="w-full px-3.5 py-2.5 rounded-xl bg-[#121620] border border-[#262E42] text-sm text-white focus:outline-none focus:border-cyan-400"
                        >
                          <option value="sepay" ${selectedGateway === "sepay" ? "selected" : ""}>SePay Webhook (Khuyên dùng)</option>
                          <option value="casso" ${selectedGateway === "casso" ? "selected" : ""}>Casso Webhook</option>
                        </select>
                      </div>
                    </div>

                    <div class="flex items-center justify-between rounded-xl bg-[#121620] border border-[#262E42] px-4 py-3 text-xs">
                      <span class="text-slate-400">Số Credit sẽ nhận:</span>
                      <span id="vietqr-est-credits" class="font-mono text-base font-bold text-cyan-300">
                        +${estCredits.toLocaleString("vi-VN")} Credits
                      </span>
                    </div>

                    <button
                      type="button"
                      id="vietqr-generate-btn"
                      ${creatingIntent ? "disabled" : ""}
                      class="w-full py-3 rounded-xl bg-gradient-to-r from-violet-600 to-cyan-500 hover:brightness-110 text-white text-sm font-bold shadow-lg shadow-violet-500/20 transition disabled:opacity-60"
                    >
                      ${creatingIntent ? "Đang tạo mã VietQR..." : "Tạo mã VietQR thanh toán"}
                    </button>
                  </div>
                `
                }
              </div>

              <!-- Right: Credit Ledger History -->
              <div class="lg:col-span-5">
                <div class="rounded-2xl bg-[#0B0D12] border border-[#262E42] p-4 flex flex-col h-full">
                  <div class="flex items-center justify-between pb-2.5 border-b border-[#262E42]">
                    <h3 class="text-xs font-bold uppercase tracking-wider text-slate-300">
                      Lịch sử biến động Credit
                    </h3>
                    <button
                      type="button"
                      id="vietqr-refresh-credits-btn"
                      class="text-[11px] text-cyan-300 hover:text-cyan-200"
                    >
                      ↻ Làm mới
                    </button>
                  </div>
                  <div class="flex-1 max-h-80 overflow-y-auto studio-scroll pr-1">
                    ${renderLedgerRows()}
                  </div>
                </div>
              </div>
            </div>

            <!-- Subscription Tier Upgrade Panel -->
            <div class="rounded-2xl bg-[#0B0D12] border border-[#262E42] p-4 sm:p-5 space-y-3">
              <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                <div>
                  <h3 class="text-sm font-bold text-white flex items-center gap-2">
                    <span>👑</span>
                    Nâng cấp Gói Tài Khoản (Tắt Watermark)
                  </h3>
                  <p class="text-[11px] text-slate-400 mt-0.5">
                    Dùng Credit có sẵn để mở khóa xuất video sạch không Watermark &amp; ưu tiên hàng đợi render.
                  </p>
                </div>
                <span class="px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider border ${
                  currentTier === "enterprise"
                    ? "bg-amber-500/15 text-amber-300 border-amber-500/40"
                    : currentTier === "pro"
                    ? "bg-cyan-500/15 text-cyan-300 border-cyan-500/40"
                    : "bg-violet-500/15 text-violet-300 border-violet-500/35"
                }">
                  Gói hiện tại: ${escapeHtml(currentTier.toUpperCase())}
                </span>
              </div>

              <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div class="rounded-xl bg-[#121620] border ${
                  currentTier === "pro" ? "border-cyan-500/50" : "border-[#262E42]"
                } p-3.5 flex items-center justify-between gap-3">
                  <div class="space-y-0.5">
                    <div class="text-xs font-bold text-cyan-300">PRO PLAN</div>
                    <div class="text-[11px] text-slate-400">Không Watermark · Ưu tiên Render</div>
                    <div class="font-mono text-xs font-bold text-white">${proCost.toLocaleString("vi-VN")} Credits</div>
                  </div>
                  <button
                    type="button"
                    data-upgrade-tier="pro"
                    ${
                      currentTier === "pro" ||
                      currentTier === "enterprise" ||
                      upgradingTier !== null
                        ? "disabled"
                        : ""
                    }
                    class="px-3.5 py-2 rounded-xl text-xs font-bold transition shrink-0 ${
                      currentTier === "pro" || currentTier === "enterprise"
                        ? "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 cursor-default"
                        : "bg-cyan-500 hover:bg-cyan-400 text-slate-950 disabled:opacity-50"
                    }"
                  >
                    ${
                      currentTier === "pro"
                        ? "✓ Đang dùng"
                        : currentTier === "enterprise"
                        ? "Đã vượt cấp"
                        : upgradingTier === "pro"
                        ? "Đang nâng cấp..."
                        : "Nâng cấp bằng Credit"
                    }
                  </button>
                </div>

                <div class="rounded-xl bg-[#121620] border ${
                  currentTier === "enterprise" ? "border-amber-500/50" : "border-[#262E42]"
                } p-3.5 flex items-center justify-between gap-3">
                  <div class="space-y-0.5">
                    <div class="text-xs font-bold text-amber-300">ENTERPRISE PLAN</div>
                    <div class="text-[11px] text-slate-400">Không Watermark · Batch tối đa</div>
                    <div class="font-mono text-xs font-bold text-white">${enterpriseCost.toLocaleString("vi-VN")} Credits</div>
                  </div>
                  <button
                    type="button"
                    data-upgrade-tier="enterprise"
                    ${currentTier === "enterprise" || upgradingTier !== null ? "disabled" : ""}
                    class="px-3.5 py-2 rounded-xl text-xs font-bold transition shrink-0 ${
                      currentTier === "enterprise"
                        ? "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 cursor-default"
                        : "bg-gradient-to-r from-amber-500 to-orange-500 hover:brightness-110 text-slate-950 disabled:opacity-50"
                    }"
                  >
                    ${
                      currentTier === "enterprise"
                        ? "✓ Đang dùng"
                        : upgradingTier === "enterprise"
                        ? "Đang nâng cấp..."
                        : "Nâng cấp bằng Credit"
                    }
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;

    // Wire events
    root.querySelector("#vietqr-modal-close")?.addEventListener("click", closeVietQRModal);
    root.querySelector("#vietqr-modal-backdrop")?.addEventListener("click", (e) => {
      if (e.target.id === "vietqr-modal-backdrop") {
        closeVietQRModal();
      }
    });

    root.querySelectorAll("[data-preset-amount]").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectedAmount = Number(btn.getAttribute("data-preset-amount")) || 100000;
        render();
      });
    });

    root.querySelector("#vietqr-custom-amount")?.addEventListener("input", (e) => {
      selectedAmount = Math.max(0, Number.parseInt(e.target.value, 10) || 0);
      const estEl = root.querySelector("#vietqr-est-credits");
      if (estEl) {
        const cr = Math.floor(selectedAmount / vndPerCredit);
        estEl.textContent = `+${cr.toLocaleString("vi-VN")} Credits`;
      }
    });

    root.querySelector("#vietqr-gateway-select")?.addEventListener("change", (e) => {
      selectedGateway = e.target.value || "sepay";
    });

    root.querySelector("#vietqr-generate-btn")?.addEventListener("click", async () => {
      if (selectedAmount < vndPerCredit) {
        showToast(`Số tiền nạp tối thiểu là ${formatVnd(vndPerCredit)}`, "warning");
        return;
      }
      creatingIntent = true;
      render();
      try {
        const intent = await api.createTopupIntent(selectedAmount, selectedGateway);
        if (!modalActive) return;
        currentIntent = intent;
        creatingIntent = false;
        render();
        startPolling(intent.transaction_id);
      } catch (err) {
        creatingIntent = false;
        showToast(err?.message || "Không thể tạo mã VietQR", "error");
        render();
      }
    });

    root.querySelector("#vietqr-change-package-btn")?.addEventListener("click", () => {
      stopPolling();
      currentIntent = null;
      pollError = "";
      render();
    });

    root.querySelector("#vietqr-copy-memo-btn")?.addEventListener("click", async () => {
      const code = currentIntent?.reference_code || "";
      if (!code) return;
      try {
        await navigator.clipboard.writeText(code);
        showToast(`Đã sao chép nội dung CK: ${code}`, "success");
      } catch {
        showToast(`Nội dung CK: ${code}`, "info");
      }
    });

    root.querySelector("#vietqr-refresh-credits-btn")?.addEventListener("click", async () => {
      await fetchCredits();
    });

    root.querySelectorAll("[data-upgrade-tier]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const targetTier = btn.getAttribute("data-upgrade-tier");
        if (!targetTier) return;
        upgradingTier = targetTier;
        render();
        try {
          const res = await api.upgradeTier(targetTier);
          const nextTier = res?.subscription_tier || targetTier;
          const prevProfile = store.getState().profile || {};
          store.setState({
            profile: { ...prevProfile, subscription_tier: nextTier },
          });
          showToast(
            `Nâng cấp gói ${String(nextTier).toUpperCase()} thành công! Watermark đã được tắt.`,
            "success",
            5000
          );
          await fetchCredits();
        } catch (err) {
          showToast(err?.message || "Không thể nâng cấp gói tài khoản", "error");
        } finally {
          upgradingTier = null;
          if (modalActive) render();
        }
      });
    });
  };

  const fetchCredits = async () => {
    loadingCredits = true;
    try {
      const data = await api.getBillingCredits({ limit: 20 });
      if (!modalActive) return;
      creditsData = data;
      store.setState({ credits: data });
    } catch (err) {
      if (modalActive) {
        showToast(err?.message || "Không thể tải thông tin ví Credit", "error");
      }
    } finally {
      loadingCredits = false;
      if (modalActive) render();
    }
  };

  keydownHandler = (e) => {
    if (e.key === "Escape") {
      closeVietQRModal();
    }
  };
  document.addEventListener("keydown", keydownHandler);

  render();
  fetchCredits();
}
