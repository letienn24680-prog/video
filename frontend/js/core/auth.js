/**
 * AI Video Studio Authentication Module
 * Supports Supabase GoTrue REST (Password Sign-In & Sign-Up) + Direct JWT Token mode for local testing.
 */

import { api } from "./api.js";
import { STORAGE_KEYS, showToast, store } from "./state.js";

function decodeJwtPayload(token) {
  try {
    const parts = String(token || "").split(".");
    if (parts.length !== 3) return null;
    const base64Url = parts[1];
    const base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "=");
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    const json = new TextDecoder("utf-8").decode(bytes);
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function isTokenExpired(token, skewSeconds = 30) {
  const payload = decodeJwtPayload(token);
  if (!payload || typeof payload.exp !== "number") {
    return false;
  }
  const nowSeconds = Math.floor(Date.now() / 1000);
  return payload.exp <= nowSeconds + skewSeconds;
}

function buildUserFromSession(accessToken, rawUser = null) {
  const claims = decodeJwtPayload(accessToken) || {};
  const id = rawUser?.id || claims.sub || null;
  if (!id) return null;
  return {
    id: String(id),
    email: rawUser?.email || claims.email || "user@studio.local",
    role: rawUser?.role || claims.role || "authenticated",
    app_metadata: rawUser?.app_metadata || claims.app_metadata || {},
    user_metadata: rawUser?.user_metadata || claims.user_metadata || {},
  };
}

function persistSession(session) {
  const accessToken = session?.access_token ? String(session.access_token).trim() : null;
  if (!accessToken) return null;

  const user = buildUserFromSession(accessToken, session.user || null);
  const normalized = {
    access_token: accessToken,
    refresh_token: session.refresh_token || null,
    expires_at: session.expires_at || null,
    user,
    mode: session.mode || "supabase",
  };

  try {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, accessToken);
    localStorage.setItem(STORAGE_KEYS.AUTH_SESSION, JSON.stringify(normalized));
  } catch {
    // Ignore storage quota errors
  }

  store.setState({
    user,
    accessToken,
  });

  window.dispatchEvent(
    new CustomEvent("studio:auth-changed", {
      detail: { user, accessToken },
    })
  );

  return normalized;
}

function readStoredSession() {
  try {
    const rawSession = localStorage.getItem(STORAGE_KEYS.AUTH_SESSION);
    if (rawSession) {
      const parsed = JSON.parse(rawSession);
      if (parsed?.access_token) {
        return parsed;
      }
    }
    const rawToken = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN);
    if (rawToken) {
      return {
        access_token: rawToken,
        user: buildUserFromSession(rawToken),
        mode: "jwt",
      };
    }
  } catch {
    // Ignore corrupt localStorage
  }
  return null;
}

function getSupabaseConfig() {
  const cfg = store.getState().publicConfig || {};
  const url = String(cfg.supabase_url || "").trim().replace(/\/+$/, "");
  const anonKey = String(cfg.supabase_anon_key || "").trim();
  const isConfigured =
    Boolean(url) &&
    Boolean(anonKey) &&
    !url.includes("your-project") &&
    !url.includes("your_") &&
    !url.includes("_here") &&
    !anonKey.includes("your_supabase") &&
    !anonKey.includes("your_") &&
    !anonKey.includes("_here");
  return { url, anonKey, isConfigured };
}

async function supabaseAuthRequest(endpoint, body) {
  const { url, anonKey, isConfigured } = getSupabaseConfig();
  if (!isConfigured) {
    throw new Error(
      "Supabase Auth chưa được cấu hình ở public-config. Hãy dùng tab 'JWT Token' để đăng nhập test cục bộ."
    );
  }

  const res = await fetch(`${url}${endpoint}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      apikey: anonKey,
      Authorization: `Bearer ${anonKey}`,
    },
    body: JSON.stringify(body),
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg =
      data?.error_description ||
      data?.msg ||
      data?.message ||
      data?.error ||
      `Supabase Auth error (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

async function refreshSessionIfNeeded(session) {
  if (!session?.access_token) return null;
  if (!isTokenExpired(session.access_token)) {
    return session;
  }

  if (session.refresh_token) {
    const { isConfigured } = getSupabaseConfig();
    if (isConfigured) {
      try {
        const refreshed = await supabaseAuthRequest("/auth/v1/token?grant_type=refresh_token", {
          refresh_token: session.refresh_token,
        });
        if (refreshed?.access_token) {
          return persistSession({ ...refreshed, mode: "supabase" });
        }
      } catch {
        // Refresh failed; fall through and clear expired session
      }
    }
  }

  store.clearSession();
  return null;
}

/**
 * Initialize authentication state from localStorage / refresh token if needed.
 */
export async function initAuth() {
  const stored = readStoredSession();
  if (!stored || !stored.access_token) {
    store.setState({ user: null, accessToken: null });
    return null;
  }

  const active = await refreshSessionIfNeeded(stored);
  if (!active || !active.access_token) {
    return null;
  }

  const user = buildUserFromSession(active.access_token, active.user);
  if (!user) {
    store.clearSession();
    return null;
  }

  store.setState({
    user,
    accessToken: active.access_token,
  });
  return user;
}

/**
 * Return the active Bearer access token (if any).
 */
export function getAccessToken() {
  const inStore = store.getState().accessToken;
  if (inStore) return inStore;
  const stored = readStoredSession();
  return stored?.access_token || null;
}

/**
 * Log out current user and clear persisted credentials.
 */
export function logout() {
  store.clearSession();
  window.dispatchEvent(
    new CustomEvent("studio:auth-changed", {
      detail: { user: null, accessToken: null },
    })
  );
  showToast("Đã đăng xuất khỏi AI Video Studio.", "info");
}

/**
 * Close and remove the Auth modal from #auth-modal-root.
 */
export function closeAuthModal() {
  const root = document.getElementById("auth-modal-root");
  if (root) {
    root.innerHTML = "";
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Authenticate using the backend Local Sandbox Demo endpoint (when LOCAL_SANDBOX_MODE=true).
 */
export async function loginWithSandbox() {
  const data = await api.getSandboxToken();
  if (!data?.access_token) {
    throw new Error("Không nhận được sandbox token từ máy chủ.");
  }
  const saved = persistSession({
    access_token: data.access_token,
    user: data.user || {
      id: "00000000-0000-0000-0000-000000000001",
      email: "demo@local.studio",
      role: "authenticated",
    },
    mode: "sandbox",
  });
  showToast("Đã kích hoạt phiên Local Sandbox Demo!", "success");
  return saved;
}

/**
 * Render and open the interactive Auth Modal inside #auth-modal-root.
 * Supports Email/Password Sign-In, Sign-Up, direct JWT paste, and Local Sandbox Demo.
 * @param {{ defaultTab?: "login"|"signup"|"token", onSuccess?: (session: object) => void }} [options]
 */
export function openAuthModal(options = {}) {
  const root = document.getElementById("auth-modal-root");
  if (!root) return;

  const { isConfigured } = getSupabaseConfig();
  const isSandboxInitial = Boolean(store.getState().publicConfig?.local_sandbox_mode);
  let activeTab = options.defaultTab || (isConfigured || isSandboxInitial ? "login" : "token");

  const render = (errorMessage = "", infoMessage = "") => {
    const currentUser = store.getState().user;
    const isSandboxEnabled = Boolean(store.getState().publicConfig?.local_sandbox_mode);

    root.innerHTML = `
      <div
        id="auth-modal-backdrop"
        class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm"
      >
        <div
          class="w-full max-w-md rounded-2xl bg-[#121620] border border-[#262E42] shadow-2xl overflow-hidden"
          role="dialog"
          aria-modal="true"
          aria-labelledby="auth-modal-title"
        >
          <!-- Header -->
          <div class="px-6 py-4 border-b border-[#262E42] flex items-center justify-between bg-[#151A27]">
            <div>
              <h2 id="auth-modal-title" class="text-base font-bold text-white">
                ${currentUser ? "Tài khoản Studio" : "Đăng nhập AI Video Studio"}
              </h2>
              <p class="text-xs text-slate-400 mt-0.5">
                Xác thực Supabase Auth hoặc JWT Token để tạo &amp; render video
              </p>
            </div>
            <button
              type="button"
              id="auth-modal-close"
              class="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-[#191F2E] transition"
              aria-label="Đóng"
            >
              ✕
            </button>
          </div>

          <div class="p-6 space-y-5">
            ${
              currentUser
                ? `
              <div class="rounded-xl bg-[#191F2E] border border-emerald-500/30 p-4 space-y-2">
                <div class="flex items-center justify-between">
                  <span class="text-xs font-semibold uppercase tracking-wider text-emerald-400">Đã đăng nhập</span>
                  <span class="px-2 py-0.5 rounded-full text-[11px] bg-violet-500/20 text-violet-300 border border-violet-500/30">
                    ${escapeHtml(currentUser.role || "authenticated")}
                  </span>
                </div>
                <div class="text-sm font-semibold text-white break-all">${escapeHtml(currentUser.email)}</div>
                <div class="text-[11px] font-mono text-slate-400 break-all">UID: ${escapeHtml(currentUser.id)}</div>
              </div>
              <div class="flex items-center justify-end gap-3">
                <button
                  type="button"
                  id="auth-logout-btn"
                  class="px-4 py-2 rounded-xl bg-rose-500/15 hover:bg-rose-500/25 border border-rose-500/40 text-rose-300 text-sm font-semibold transition"
                >
                  Đăng xuất
                </button>
                <button
                  type="button"
                  id="auth-done-btn"
                  class="px-4 py-2 rounded-xl bg-violet-600 hover:bg-violet-500 text-white text-sm font-semibold transition"
                >
                  Đóng
                </button>
              </div>
            `
                : `
              ${
                isSandboxEnabled
                  ? `
                <div class="rounded-xl bg-gradient-to-r from-cyan-500/15 via-violet-500/15 to-fuchsia-500/15 border border-cyan-500/40 p-3.5 space-y-2.5">
                  <div class="flex items-center justify-between gap-2">
                    <span class="text-[11px] font-bold uppercase tracking-wider text-cyan-300">Local Sandbox Mode</span>
                    <span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-cyan-500/20 text-cyan-200 border border-cyan-400/30">Không cần Supabase</span>
                  </div>
                  <button
                    type="button"
                    id="auth-sandbox-btn"
                    class="w-full py-2.5 px-3 rounded-xl bg-gradient-to-r from-cyan-500 to-violet-600 hover:brightness-110 text-white text-xs sm:text-sm font-bold shadow-lg shadow-cyan-500/20 transition"
                  >
                    Dùng thử ngay với Local Sandbox (500 Credits Demo)
                  </button>
                </div>
              `
                  : ""
              }

              <!-- Mode Tabs -->
              <div class="grid grid-cols-3 gap-1.5 p-1 rounded-xl bg-[#0B0D12] border border-[#262E42]">
                <button
                  type="button"
                  data-auth-tab="login"
                  class="py-2 px-2 rounded-lg text-xs font-semibold transition ${
                    activeTab === "login"
                      ? "bg-[#191F2E] text-white border border-violet-500/40 shadow"
                      : "text-slate-400 hover:text-slate-200"
                  }"
                >
                  Đăng nhập
                </button>
                <button
                  type="button"
                  data-auth-tab="signup"
                  class="py-2 px-2 rounded-lg text-xs font-semibold transition ${
                    activeTab === "signup"
                      ? "bg-[#191F2E] text-white border border-violet-500/40 shadow"
                      : "text-slate-400 hover:text-slate-200"
                  }"
                >
                  Đăng ký
                </button>
                <button
                  type="button"
                  data-auth-tab="token"
                  class="py-2 px-2 rounded-lg text-xs font-semibold transition ${
                    activeTab === "token"
                      ? "bg-[#191F2E] text-cyan-300 border border-cyan-500/40 shadow"
                      : "text-slate-400 hover:text-slate-200"
                  }"
                >
                  JWT Token
                </button>
              </div>

              ${
                !isConfigured && activeTab !== "token"
                  ? isSandboxEnabled
                    ? `
                <div class="rounded-xl bg-cyan-500/10 border border-cyan-500/35 p-3 text-xs text-cyan-200 leading-relaxed">
                  Đang ở <strong>Local Sandbox Mode</strong>: Bạn có thể Đăng ký / Đăng nhập tài khoản cục bộ bằng Email &amp; Mật khẩu ngay bên dưới (tặng sẵn 500 Credits), hoặc dùng nút kích hoạt nhanh phía trên.
                </div>
              `
                    : `
                <div class="rounded-xl bg-amber-500/10 border border-amber-500/30 p-3 text-xs text-amber-200 leading-relaxed">
                  Supabase URL / Anon Key chưa được cấu hình. Bạn có thể chuyển sang tab
                  <strong>JWT Token</strong> để dán token test cục bộ.
                </div>
              `
                  : ""
              }

              ${
                errorMessage
                  ? `
                <div class="rounded-xl bg-rose-500/10 border border-rose-500/40 px-3.5 py-2.5 text-xs text-rose-200">
                  ${escapeHtml(errorMessage)}
                </div>
              `
                  : ""
              }

              ${
                infoMessage
                  ? `
                <div class="rounded-xl bg-emerald-500/10 border border-emerald-500/40 px-3.5 py-2.5 text-xs text-emerald-200">
                  ${escapeHtml(infoMessage)}
                </div>
              `
                  : ""
              }

              ${
                activeTab === "token"
                  ? `
                <form id="auth-token-form" class="space-y-4">
                  <div>
                    <label for="auth-jwt-input" class="block text-xs font-semibold text-slate-300 mb-1.5">
                      Bearer JWT Access Token
                    </label>
                    <textarea
                      id="auth-jwt-input"
                      rows="4"
                      required
                      placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
                      class="w-full rounded-xl bg-[#0B0D12] border border-[#262E42] focus:border-cyan-500 focus:outline-none px-3.5 py-2.5 text-xs font-mono text-slate-100 placeholder-slate-600"
                    ></textarea>
                    <p class="mt-1.5 text-[11px] text-slate-400">
                      Dùng để test nhanh với JWT hợp lệ (có claim <code class="text-cyan-300">sub</code>).
                    </p>
                  </div>
                  <button
                    type="submit"
                    class="w-full py-2.5 rounded-xl bg-gradient-to-r from-violet-600 to-cyan-500 hover:brightness-110 text-white text-sm font-semibold shadow-lg shadow-violet-500/20 transition"
                  >
                    Xác thực bằng JWT Token
                  </button>
                </form>
              `
                  : `
                <form id="auth-password-form" class="space-y-4">
                  <div>
                    <label for="auth-email-input" class="block text-xs font-semibold text-slate-300 mb-1.5">
                      Email
                    </label>
                    <input
                      id="auth-email-input"
                      type="email"
                      required
                      autocomplete="email"
                      placeholder="creator@example.com"
                      class="w-full rounded-xl bg-[#0B0D12] border border-[#262E42] focus:border-violet-500 focus:outline-none px-3.5 py-2.5 text-sm text-slate-100 placeholder-slate-600"
                    />
                  </div>
                  <div>
                    <label for="auth-password-input" class="block text-xs font-semibold text-slate-300 mb-1.5">
                      Mật khẩu
                    </label>
                    <input
                      id="auth-password-input"
                      type="password"
                      required
                      minlength="6"
                      autocomplete="${activeTab === "signup" ? "new-password" : "current-password"}"
                      placeholder="••••••••"
                      class="w-full rounded-xl bg-[#0B0D12] border border-[#262E42] focus:border-violet-500 focus:outline-none px-3.5 py-2.5 text-sm text-slate-100 placeholder-slate-600"
                    />
                  </div>
                  <button
                    type="submit"
                    id="auth-submit-btn"
                    class="w-full py-2.5 rounded-xl bg-gradient-to-r from-violet-600 to-cyan-500 hover:brightness-110 text-white text-sm font-semibold shadow-lg shadow-violet-500/20 transition"
                  >
                    ${activeTab === "signup" ? "Tạo tài khoản" : "Đăng nhập"}
                  </button>
                </form>
              `
              }
            `
            }
          </div>
        </div>
      </div>
    `;

    // Wire events
    const backdrop = root.querySelector("#auth-modal-backdrop");
    const closeBtn = root.querySelector("#auth-modal-close");
    if (closeBtn) closeBtn.addEventListener("click", closeAuthModal);
    if (backdrop) {
      backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) closeAuthModal();
      });
    }

    const logoutBtn = root.querySelector("#auth-logout-btn");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", () => {
        logout();
        closeAuthModal();
      });
    }

    const doneBtn = root.querySelector("#auth-done-btn");
    if (doneBtn) {
      doneBtn.addEventListener("click", closeAuthModal);
    }

    const sandboxBtn = root.querySelector("#auth-sandbox-btn");
    if (sandboxBtn) {
      sandboxBtn.addEventListener("click", async () => {
        sandboxBtn.disabled = true;
        sandboxBtn.textContent = "Đang kích hoạt Sandbox...";
        try {
          const saved = await loginWithSandbox();
          closeAuthModal();
          options.onSuccess?.(saved);
        } catch (err) {
          render(err?.message || "Không thể kích hoạt Local Sandbox.");
        }
      });
    }

    root.querySelectorAll("[data-auth-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        activeTab = btn.getAttribute("data-auth-tab");
        render();
      });
    });

    const tokenForm = root.querySelector("#auth-token-form");
    if (tokenForm) {
      tokenForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const raw = root.querySelector("#auth-jwt-input")?.value?.trim() || "";
        const claims = decodeJwtPayload(raw);
        if (!claims || !claims.sub) {
          render("JWT không hợp lệ hoặc thiếu claim 'sub'.");
          return;
        }
        if (isTokenExpired(raw, 0)) {
          render("JWT Token này đã hết hạn (exp).");
          return;
        }
        const saved = persistSession({ access_token: raw, mode: "jwt" });
        showToast(`Đã xác thực JWT (${saved.user.email})`, "success");
        closeAuthModal();
        options.onSuccess?.(saved);
      });
    }

    const pwdForm = root.querySelector("#auth-password-form");
    if (pwdForm) {
      pwdForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const email = root.querySelector("#auth-email-input")?.value?.trim() || "";
        const password = root.querySelector("#auth-password-input")?.value || "";
        const submitBtn = root.querySelector("#auth-submit-btn");

        if (submitBtn) {
          submitBtn.disabled = true;
          submitBtn.textContent = "Đang xử lý...";
        }

        try {
          if (!isConfigured && isSandboxEnabled) {
            if (activeTab === "signup") {
              const data = await api.sandboxRegister(email, password);
              const saved = persistSession({ ...data, mode: "sandbox" });
              showToast("Đăng ký tài khoản Sandbox thành công!", "success");
              closeAuthModal();
              options.onSuccess?.(saved);
            } else {
              const data = await api.sandboxLogin(email, password);
              const saved = persistSession({ ...data, mode: "sandbox" });
              showToast(`Đăng nhập thành công (${saved?.user?.email || email})!`, "success");
              closeAuthModal();
              options.onSuccess?.(saved);
            }
          } else if (activeTab === "signup") {
            const data = await supabaseAuthRequest("/auth/v1/signup", { email, password });
            if (data?.access_token) {
              const saved = persistSession({ ...data, mode: "supabase" });
              showToast("Đăng ký và đăng nhập thành công!", "success");
              closeAuthModal();
              options.onSuccess?.(saved);
            } else {
              render("", "Đăng ký thành công! Vui lòng kiểm tra email để xác nhận rồi đăng nhập.");
            }
          } else {
            const data = await supabaseAuthRequest("/auth/v1/token?grant_type=password", {
              email,
              password,
            });
            const saved = persistSession({ ...data, mode: "supabase" });
            showToast(`Chào mừng ${saved?.user?.email || email}!`, "success");
            closeAuthModal();
            options.onSuccess?.(saved);
          }
        } catch (err) {
          render(err?.message || "Xác thực thất bại.");
        }
      });
    }
  };

  render();
}
