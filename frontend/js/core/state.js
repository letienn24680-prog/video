/**
 * Reactive Studio Store & Toast Notification System
 * Owns application state for AI Video Studio SPA.
 */

export const STORAGE_KEYS = {
  AUTH_SESSION: "ai_video_studio_auth_session",
  ACCESS_TOKEN: "ai_video_studio_access_token",
  PREFERENCES: "avs_studio_prefs",
};

const DEFAULT_PREFERENCES = {
  aspectRatio: "9:16",
  targetLang: "vi",
  translationStyle: "casual",
  burnSubtitles: true,
};

function loadSavedPreferences() {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.PREFERENCES);
    if (!raw) return { ...DEFAULT_PREFERENCES };
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return { ...DEFAULT_PREFERENCES };
    return {
      ...DEFAULT_PREFERENCES,
      ...parsed,
    };
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

const initialState = {
  publicConfig: null,
  diagnostics: null,
  user: null,
  accessToken: null,
  profile: null,
  preferences: loadSavedPreferences(),
  credits: {
    balance: 0,
    lifetime_purchased: 0,
    lifetime_spent: 0,
    credit_ledger: [],
  },
  providers: {
    providers: {},
    defaults: {},
  },
  ttsVoices: {},
  projects: [],
  activeProject: null,
  activeView: "dashboard",
  sseConnected: false,
  sseLogs: [],
  sseStage: null,
};

let state = structuredClone(initialState);
const listeners = new Set();

export const store = {
  /**
   * Return the current immutable-by-convention state snapshot.
   */
  getState() {
    return state;
  },

  /**
   * Merge partial state (or functional updater) and notify subscribers.
   * @param {object|function} partial
   */
  setState(partial) {
    const prevState = state;
    const patch = typeof partial === "function" ? partial(prevState) : partial;
    if (!patch || typeof patch !== "object") {
      return state;
    }

    state = {
      ...prevState,
      ...patch,
    };

    for (const listener of listeners) {
      try {
        listener(state, prevState);
      } catch (err) {
        console.error("[store] Subscriber error:", err);
      }
    }
    return state;
  },

  /**
   * Subscribe to store updates. Returns an unsubscribe callback.
   * @param {(nextState: object, prevState: object) => void} listener
   */
  subscribe(listener) {
    if (typeof listener !== "function") {
      throw new TypeError("store.subscribe expects a function listener");
    }
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },

  /**
   * Reset user/session/project state cleanly on logout or 401.
   */
  clearSession() {
    try {
      localStorage.removeItem(STORAGE_KEYS.AUTH_SESSION);
      localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN);
    } catch {
      // Ignore storage failures in restricted contexts
    }
    this.setState({
      user: null,
      accessToken: null,
      profile: null,
      credits: {
        balance: 0,
        lifetime_purchased: 0,
        lifetime_spent: 0,
        credit_ledger: [],
      },
      projects: [],
      activeProject: null,
      sseConnected: false,
      sseLogs: [],
      sseStage: null,
    });
  },
};

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const TOAST_STYLES = {
  info: {
    border: "border-cyan-500/40",
    bg: "bg-[#121620]/95",
    iconColor: "text-cyan-400",
    dot: "bg-cyan-400",
  },
  success: {
    border: "border-emerald-500/45",
    bg: "bg-[#101C1A]/95",
    iconColor: "text-emerald-400",
    dot: "bg-emerald-400",
  },
  warning: {
    border: "border-amber-500/45",
    bg: "bg-[#1C1810]/95",
    iconColor: "text-amber-400",
    dot: "bg-amber-400",
  },
  error: {
    border: "border-rose-500/50",
    bg: "bg-[#1F1218]/95",
    iconColor: "text-rose-400",
    dot: "bg-rose-400",
  },
};

/**
 * Display a dismissible toast notification in #toast-container.
 * @param {string} message
 * @param {"info"|"success"|"error"|"warning"} [type="info"]
 * @param {number} [durationMs=4200]
 */
export function showToast(message, type = "info", durationMs = 4200) {
  const container = document.getElementById("toast-container");
  if (!container) {
    console.log(`[toast:${type}]`, message);
    return;
  }

  const palette = TOAST_STYLES[type] || TOAST_STYLES.info;
  const toast = document.createElement("div");
  toast.className = `studio-toast pointer-events-auto flex items-start gap-3 rounded-xl border ${palette.border} ${palette.bg} backdrop-blur-xl px-4 py-3 shadow-2xl text-sm text-slate-100`;
  toast.setAttribute("role", "status");

  toast.innerHTML = `
    <span class="mt-1.5 w-2 h-2 rounded-full ${palette.dot} shrink-0"></span>
    <div class="flex-1 min-w-0 leading-snug break-words">${escapeHtml(message)}</div>
    <button
      type="button"
      class="shrink-0 text-slate-400 hover:text-white transition text-xs px-1"
      aria-label="Đóng thông báo"
    >✕</button>
  `;

  const removeToast = () => {
    if (!toast.parentNode) return;
    toast.style.opacity = "0";
    toast.style.transform = "translateY(6px)";
    toast.style.transition = "opacity 150ms ease, transform 150ms ease";
    setTimeout(() => toast.remove(), 160);
  };

  const closeBtn = toast.querySelector("button");
  if (closeBtn) {
    closeBtn.addEventListener("click", removeToast);
  }

  container.appendChild(toast);

  if (durationMs > 0) {
    setTimeout(removeToast, durationMs);
  }
}

/**
 * Merge and persist user studio preferences into localStorage ("avs_studio_prefs") and store.
 * @param {object} partial
 */
export function savePreferences(partial = {}) {
  const prev = store.getState().preferences || DEFAULT_PREFERENCES;
  const next = {
    ...DEFAULT_PREFERENCES,
    ...prev,
    ...partial,
  };
  try {
    localStorage.setItem(STORAGE_KEYS.PREFERENCES, JSON.stringify(next));
  } catch {
    // Ignore storage quota errors
  }
  store.setState({ preferences: next });
  return next;
}

/**
 * Render an accessible dark-mode confirmation modal inside #studio-dialog-modal-root.
 * @returns {Promise<boolean>}
 */
export function confirmDialog({
  title = "Xác nhận",
  message = "",
  confirmText = "Xác nhận",
  cancelText = "Hủy",
  danger = false,
} = {}) {
  const root = document.getElementById("studio-dialog-modal-root");
  if (!root) {
    return Promise.resolve(window.confirm(message || title));
  }

  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKeyDown);
      root.innerHTML = "";
      resolve(Boolean(result));
    };

    const onKeyDown = (e) => {
      if (e.key === "Escape") finish(false);
    };
    document.addEventListener("keydown", onKeyDown);

    const confirmBtnClass = danger
      ? "bg-rose-600 hover:bg-rose-500 text-white shadow-lg shadow-rose-500/20"
      : "bg-gradient-to-r from-violet-600 to-cyan-500 hover:brightness-110 text-white shadow-lg shadow-violet-500/20";

    root.innerHTML = `
      <div
        id="sd-backdrop"
        class="fixed inset-0 z-[80] flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm"
      >
        <div
          class="w-full max-w-md rounded-2xl bg-[#121620] border border-[#262E42] shadow-2xl overflow-hidden"
          role="dialog"
          aria-modal="true"
          aria-labelledby="sd-title"
        >
          <div class="px-5 py-4 border-b border-[#262E42] bg-[#151A27] flex items-center justify-between gap-3">
            <h3 id="sd-title" class="text-sm font-bold text-white">${escapeHtml(title)}</h3>
            <button
              type="button"
              id="sd-close-btn"
              class="p-1 rounded-lg text-slate-400 hover:text-white hover:bg-[#191F2E] transition"
              aria-label="Đóng"
            >✕</button>
          </div>
          <div class="p-5 space-y-5">
            <p class="text-sm text-slate-300 leading-relaxed whitespace-pre-line">${escapeHtml(message)}</p>
            <div class="flex items-center justify-end gap-2.5">
              <button
                type="button"
                id="sd-cancel-btn"
                class="px-4 py-2 rounded-xl bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-xs sm:text-sm font-semibold text-slate-200 transition"
              >${escapeHtml(cancelText)}</button>
              <button
                type="button"
                id="sd-confirm-btn"
                class="px-4 py-2 rounded-xl text-xs sm:text-sm font-semibold transition ${confirmBtnClass}"
              >${escapeHtml(confirmText)}</button>
            </div>
          </div>
        </div>
      </div>
    `;

    root.querySelector("#sd-backdrop")?.addEventListener("click", (e) => {
      if (e.target.id === "sd-backdrop") finish(false);
    });
    root.querySelector("#sd-close-btn")?.addEventListener("click", () => finish(false));
    root.querySelector("#sd-cancel-btn")?.addEventListener("click", () => finish(false));
    const confirmBtn = root.querySelector("#sd-confirm-btn");
    confirmBtn?.addEventListener("click", () => finish(true));
    confirmBtn?.focus();
  });
}

/**
 * Render an accessible dark-mode input prompt modal inside #studio-dialog-modal-root.
 * @returns {Promise<string | null>}
 */
export function promptDialog({
  title = "Nhập thông tin",
  label = "",
  value = "",
  placeholder = "",
  confirmText = "Lưu",
} = {}) {
  const root = document.getElementById("studio-dialog-modal-root");
  if (!root) {
    return Promise.resolve(window.prompt(label || title, value));
  }

  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKeyDown);
      root.innerHTML = "";
      resolve(result);
    };

    const onKeyDown = (e) => {
      if (e.key === "Escape") finish(null);
    };
    document.addEventListener("keydown", onKeyDown);

    root.innerHTML = `
      <div
        id="sd-backdrop"
        class="fixed inset-0 z-[80] flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm"
      >
        <div
          class="w-full max-w-md rounded-2xl bg-[#121620] border border-[#262E42] shadow-2xl overflow-hidden"
          role="dialog"
          aria-modal="true"
          aria-labelledby="sd-prompt-title"
        >
          <div class="px-5 py-4 border-b border-[#262E42] bg-[#151A27] flex items-center justify-between gap-3">
            <h3 id="sd-prompt-title" class="text-sm font-bold text-white">${escapeHtml(title)}</h3>
            <button
              type="button"
              id="sd-close-btn"
              class="p-1 rounded-lg text-slate-400 hover:text-white hover:bg-[#191F2E] transition"
              aria-label="Đóng"
            >✕</button>
          </div>
          <form id="sd-prompt-form" class="p-5 space-y-4">
            <div>
              ${
                label
                  ? `<label for="sd-prompt-input" class="block text-xs font-semibold text-slate-300 mb-1.5">${escapeHtml(
                      label
                    )}</label>`
                  : ""
              }
              <input
                id="sd-prompt-input"
                type="text"
                value="${escapeHtml(value)}"
                placeholder="${escapeHtml(placeholder)}"
                class="w-full rounded-xl bg-[#0B0D12] border border-[#262E42] focus:border-violet-500 focus:outline-none px-3.5 py-2.5 text-sm text-slate-100 placeholder-slate-600"
              />
            </div>
            <div class="flex items-center justify-end gap-2.5">
              <button
                type="button"
                id="sd-cancel-btn"
                class="px-4 py-2 rounded-xl bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-xs sm:text-sm font-semibold text-slate-200 transition"
              >Hủy</button>
              <button
                type="submit"
                id="sd-confirm-btn"
                class="px-4 py-2 rounded-xl bg-gradient-to-r from-violet-600 to-cyan-500 hover:brightness-110 text-white text-xs sm:text-sm font-semibold shadow-lg shadow-violet-500/20 transition"
              >${escapeHtml(confirmText)}</button>
            </div>
          </form>
        </div>
      </div>
    `;

    root.querySelector("#sd-backdrop")?.addEventListener("click", (e) => {
      if (e.target.id === "sd-backdrop") finish(null);
    });
    root.querySelector("#sd-close-btn")?.addEventListener("click", () => finish(null));
    root.querySelector("#sd-cancel-btn")?.addEventListener("click", () => finish(null));

    const input = root.querySelector("#sd-prompt-input");
    const form = root.querySelector("#sd-prompt-form");
    form?.addEventListener("submit", (e) => {
      e.preventDefault();
      finish(input ? input.value : "");
    });
    if (input) {
      input.focus();
      input.select();
    }
  });
}
