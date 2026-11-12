/**
 * AI Video Studio — SPA Bootstrapper & Workflow Orchestrator
 * Coordinates authentication, public config, provider/voice preloading,
 * sidebar navigation, SSE Progress Console, HITL Review Editor, VietQR Modal,
 * and Video Player Modal.
 */

import { api } from "./core/api.js";
import { initAuth, loginWithSandbox, openAuthModal } from "./core/auth.js";
import { showToast, store } from "./core/state.js";
import { renderDashboardView } from "./views/dashboard_view.js";
import { renderTranslatorView } from "./views/translator_view.js";
import { renderFacelessView } from "./views/faceless_view.js";
import { renderBillingView } from "./views/billing_view.js";
import { renderSettingsView } from "./views/settings_view.js";
import { destroyProgressConsole, renderProgressConsole } from "./components/progress_console.js";
import { renderReviewEditor } from "./components/review_editor.js";
import { openVietQRModal } from "./components/vietqr_modal.js";
import { renderMarketingKitPanel } from "./components/marketing_kit_panel.js";

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const VIEW_META = {
  dashboard: {
    title: "Studio Dashboard",
    subtitle: "Dịch video hàng loạt, HITL Review & Tạo video Faceless tự động",
  },
  translator: {
    title: "Video Translator & Auto-Dubber",
    subtitle: "Nhập danh sách URL hoặc upload nhiều file để dịch & lồng tiếng song song",
  },
  faceless: {
    title: "Faceless Video Generator",
    subtitle: "Tạo storyboard 3–5s, tìm video/ảnh AI, đọc TTS & phụ đề .ass Karaoke",
  },
  billing: {
    title: "Credit & Giao dịch",
    subtitle: "Quản lý số dư Credit, gói tài khoản, lịch sử nạp VietQR và biến động chi tiêu",
  },
  settings: {
    title: "Tài khoản & Hệ thống",
    subtitle: "Hồ sơ cá nhân, chẩn đoán FFmpeg/yt-dlp/AI Providers và cấu hình Studio mặc định",
  },
};

function parseViewFromHash() {
  if (typeof window === "undefined") return null;
  const raw = String(window.location.hash || "").replace(/^#\/?/, "").trim().toLowerCase();
  return VIEW_META[raw] ? raw : null;
}

let activeReviewHandle = null;
let videoModalKeyHandler = null;
let bootstrapped = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isBrowserPlayableUrl(url) {
  if (!url) return false;
  const s = String(url).trim();
  return (
    s.startsWith("http://") ||
    s.startsWith("https://") ||
    s.startsWith("/") ||
    s.startsWith("blob:")
  );
}

export function closeVideoPlayerModal() {
  if (videoModalKeyHandler) {
    document.removeEventListener("keydown", videoModalKeyHandler);
    videoModalKeyHandler = null;
  }
  const root = document.getElementById("video-player-modal-root");
  if (root) {
    root.innerHTML = "";
  }
}

export function openVideoPlayerModal(project) {
  const root = document.getElementById("video-player-modal-root");
  if (!root || !project) return;

  closeVideoPlayerModal();

  const videoUrl = project.output_video_url || "";
  const canPlay = isBrowserPlayableUrl(videoUrl);
  const durationText =
    project.duration_seconds !== null && project.duration_seconds !== undefined
      ? `${Number(project.duration_seconds).toFixed(1)}s`
      : "—";

  root.innerHTML = `
    <div
      id="vp-modal-backdrop"
      class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/85 backdrop-blur-sm overflow-y-auto"
    >
      <div
        class="w-full max-w-4xl rounded-2xl bg-[#121620] border border-[#262E42] shadow-2xl overflow-hidden my-auto max-h-[92vh] flex flex-col"
        role="dialog"
        aria-modal="true"
        aria-labelledby="vp-modal-title"
      >
        <div class="px-5 py-4 border-b border-[#262E42] bg-[#151A27] flex items-center justify-between gap-4 shrink-0">
          <div class="min-w-0">
            <div class="flex items-center gap-2 mb-0.5">
              <span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/15 text-emerald-300 border border-emerald-500/35">
                COMPLETED
              </span>
              <span class="text-[11px] font-mono text-slate-400">
                ${escapeHtml(project.mode === "faceless" ? "Faceless AI" : "Translator")} · ${escapeHtml(project.aspect_ratio || "9:16")} · ${escapeHtml(durationText)}
              </span>
            </div>
            <h3 id="vp-modal-title" class="text-base font-bold text-white truncate">
              ${escapeHtml(project.title || "Video Output")}
            </h3>
          </div>
          <button
            type="button"
            id="vp-close-btn"
            class="p-2 rounded-xl text-slate-400 hover:text-white hover:bg-[#191F2E] transition"
            aria-label="Đóng"
          >
            ✕
          </button>
        </div>

        <div class="p-5 space-y-5 overflow-y-auto studio-scroll">
          ${
            canPlay
              ? `
            <div class="rounded-2xl overflow-hidden bg-black border border-[#262E42] flex items-center justify-center max-h-[55vh]">
              <video
                src="${escapeHtml(videoUrl)}"
                controls
                autoplay
                playsinline
                class="w-full max-h-[55vh] object-contain"
              ></video>
            </div>
          `
              : `
            <div class="rounded-2xl bg-[#0B0D12] border border-[#262E42] p-8 text-center space-y-2">
              <div class="text-3xl">🎬</div>
              <div class="text-sm font-semibold text-white">Video đã render xong</div>
              <div class="text-xs font-mono text-slate-400 break-all">
                ${escapeHtml(videoUrl || "Chưa có URL công khai (tệp lưu cục bộ trên server)")}
              </div>
            </div>
          `
          }

          <div class="flex flex-wrap items-center justify-between gap-3 pt-1">
            <div class="text-xs text-slate-400">
              Credit đã dùng: <strong class="font-mono text-amber-300">${Number(project.credits_charged || 0)} cr</strong>
            </div>

            <div class="flex flex-wrap items-center gap-2">
              <button
                type="button"
                id="vp-export-srt-btn"
                class="px-3 py-2 rounded-xl bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-xs font-semibold text-cyan-300 transition"
              >
                ⬇ .SRT
              </button>
              <button
                type="button"
                id="vp-export-ass-btn"
                class="px-3 py-2 rounded-xl bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-xs font-semibold text-violet-300 transition"
              >
                ⬇ .ASS
              </button>
              ${
                project.batch_id
                  ? `
                <button
                  type="button"
                  id="vp-download-batch-zip-btn"
                  class="px-3.5 py-2 rounded-xl bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/40 text-xs font-semibold text-amber-200 transition"
                >
                  📦 Tải Batch ZIP
                </button>
              `
                  : ""
              }
              ${
                canPlay
                  ? `
                <button
                  type="button"
                  id="vp-copy-url-btn"
                  class="px-3.5 py-2 rounded-xl bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-xs font-semibold text-slate-200 transition"
                >
                  🔗 Sao chép link
                </button>
                <a
                  href="${escapeHtml(videoUrl)}"
                  download
                  target="_blank"
                  rel="noopener noreferrer"
                  class="px-4 py-2 rounded-xl bg-gradient-to-r from-emerald-500 to-cyan-500 hover:brightness-110 text-slate-950 text-xs font-bold shadow-lg shadow-emerald-500/20 transition"
                >
                  ⬇ Tải Video MP4
                </a>
              `
                  : ""
              }
              <button
                type="button"
                id="vp-done-btn"
                class="px-3.5 py-2 rounded-xl bg-violet-600 hover:bg-violet-500 text-white text-xs font-semibold transition"
              >
                Đóng
              </button>
            </div>
          </div>

          <!-- AI Marketing Kit & Thumbnail Panel -->
          <div id="vp-marketing-kit-root"></div>
        </div>
      </div>
    </div>
  `;

  const mkRoot = root.querySelector("#vp-marketing-kit-root");
  if (mkRoot) {
    renderMarketingKitPanel(mkRoot, project);
  }

  root.querySelector("#vp-close-btn")?.addEventListener("click", closeVideoPlayerModal);
  root.querySelector("#vp-done-btn")?.addEventListener("click", closeVideoPlayerModal);
  root.querySelector("#vp-modal-backdrop")?.addEventListener("click", (e) => {
    if (e.target.id === "vp-modal-backdrop") closeVideoPlayerModal();
  });
  root.querySelector("#vp-copy-url-btn")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(videoUrl);
      showToast("Đã sao chép đường dẫn video!", "success");
    } catch {
      showToast(videoUrl, "info");
    }
  });

  const wireSubtitleBtn = (btnId, format) => {
    const btn = root.querySelector(btnId);
    btn?.addEventListener("click", async () => {
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = "Đang tải...";
      try {
        const { blob, filename } = await api.exportSubtitles(project.id, format);
        triggerBlobDownload(blob, filename);
        showToast(`Đã tải xuống ${filename}`, "success");
      } catch (err) {
        showToast(err?.message || `Không thể xuất .${format.toUpperCase()}`, "error");
      } finally {
        btn.disabled = false;
        btn.textContent = orig;
      }
    });
  };
  wireSubtitleBtn("#vp-export-srt-btn", "srt");
  wireSubtitleBtn("#vp-export-ass-btn", "ass");

  const batchBtn = root.querySelector("#vp-download-batch-zip-btn");
  batchBtn?.addEventListener("click", async () => {
    if (!project.batch_id) return;
    batchBtn.disabled = true;
    const orig = batchBtn.textContent;
    batchBtn.textContent = "Đang nén ZIP...";
    try {
      const { blob, filename } = await api.downloadBatchZip(project.batch_id);
      triggerBlobDownload(blob, filename);
      showToast(`Đã tải xuống ${filename}`, "success");
    } catch (err) {
      showToast(err?.message || "Không thể tải Batch ZIP", "error");
    } finally {
      batchBtn.disabled = false;
      batchBtn.textContent = orig;
    }
  });

  videoModalKeyHandler = (e) => {
    if (e.key === "Escape") closeVideoPlayerModal();
  };
  document.addEventListener("keydown", videoModalKeyHandler);
}

function clearReviewPanel() {
  if (activeReviewHandle) {
    activeReviewHandle.destroy?.();
    activeReviewHandle = null;
  }
  const reviewRoot = document.getElementById("studio-review-root");
  if (reviewRoot) {
    reviewRoot.innerHTML = "";
    reviewRoot.classList.add("hidden");
  }
}

function clearProgressPanel() {
  destroyProgressConsole();
  const progressRoot = document.getElementById("studio-progress-root");
  if (progressRoot) {
    progressRoot.innerHTML = "";
    progressRoot.classList.add("hidden");
  }
}

async function refreshProjectsAndCredits() {
  if (!store.getState().accessToken) return;
  const [projectsRes, creditsRes, profileRes] = await Promise.allSettled([
    api.listProjects({ limit: 50 }),
    api.getBillingCredits({ limit: 20 }),
    api.getAccountProfile(),
  ]);

  const patch = {};
  if (projectsRes.status === "fulfilled" && Array.isArray(projectsRes.value)) {
    patch.projects = projectsRes.value;
  }
  let mergedProfile = { ...(store.getState().profile || {}) };
  let hasProfilePatch = false;

  if (profileRes.status === "fulfilled" && profileRes.value) {
    mergedProfile = {
      ...mergedProfile,
      ...profileRes.value,
      subscription_tier:
        profileRes.value.subscription_tier ||
        profileRes.value.tier ||
        mergedProfile.subscription_tier ||
        "free",
    };
    hasProfilePatch = true;
  }

  if (creditsRes.status === "fulfilled" && creditsRes.value) {
    patch.credits = creditsRes.value;
    if (creditsRes.value.subscription_tier) {
      mergedProfile = {
        ...mergedProfile,
        subscription_tier: creditsRes.value.subscription_tier,
      };
      hasProfilePatch = true;
    }
  }

  if (hasProfilePatch) {
    patch.profile = mergedProfile;
  }
  if (Object.keys(patch).length > 0) {
    store.setState(patch);
  }
}

function openProgressForProject(project) {
  const progressRoot = document.getElementById("studio-progress-root");
  if (!progressRoot || !project?.id) return;

  clearReviewPanel();
  store.setState({ activeProject: project });

  renderProgressConsole(progressRoot, project, {
    onAwaitingReview(projectDetail) {
      showToast(`"${projectDetail.title}" đã sẵn sàng để Review HITL!`, "info");
      openReviewForProject(projectDetail);
    },
    onCompleted(projectDetail) {
      showToast(`Render hoàn tất: "${projectDetail.title}"`, "success");
      refreshProjectsAndCredits();
      openVideoPlayerModal(projectDetail);
    },
    onCancelled() {
      refreshProjectsAndCredits();
    },
    onError() {
      refreshProjectsAndCredits();
    },
  });

  progressRoot.scrollIntoView({ behavior: "smooth", block: "start" });
}

function openReviewForProject(projectDetail) {
  const reviewRoot = document.getElementById("studio-review-root");
  if (!reviewRoot || !projectDetail?.id) return;

  clearProgressPanel();
  store.setState({ activeProject: projectDetail });

  activeReviewHandle = renderReviewEditor(reviewRoot, projectDetail, {
    onRenderStarted(updatedProject) {
      activeReviewHandle = null;
      openProgressForProject(updatedProject);
    },
    onClose() {
      activeReviewHandle = null;
    },
  });

  reviewRoot.scrollIntoView({ behavior: "smooth", block: "start" });
}

function buildViewHandlers() {
  return {
    onNavigate(viewName) {
      navigateToView(viewName);
    },
    async onRefreshProjects() {
      await refreshProjectsAndCredits();
      showToast("Đã làm mới dữ liệu Studio", "success");
    },
    onOpenAuth() {
      openAuthModal({ onSuccess: () => refreshProjectsAndCredits() });
    },
    onWatchProject(project) {
      openProgressForProject(project);
    },
    onOpenProgress(project) {
      openProgressForProject(project);
    },
    onReviewProject(projectDetail) {
      openReviewForProject(projectDetail);
    },
    onOpenReview(projectDetail) {
      openReviewForProject(projectDetail);
    },
    onPlayVideo(project) {
      openVideoPlayerModal(project);
    },
    onPreviewVideo(project) {
      openVideoPlayerModal(project);
    },
    onProjectCancelled() {
      refreshProjectsAndCredits();
    },
    onOpenTopup() {
      openVietQRModal({
        onTopupCompleted() {
          refreshProjectsAndCredits();
        },
      });
    },
    onProfileUpdated(profile) {
      if (profile && typeof profile === "object") {
        store.setState({
          profile: {
            ...(store.getState().profile || {}),
            ...profile,
            subscription_tier:
              profile.subscription_tier ||
              profile.tier ||
              store.getState().profile?.subscription_tier ||
              "free",
          },
        });
      } else {
        refreshProjectsAndCredits();
      }
    },
    onDeleteProject(projectId) {
      if (store.getState().activeProject?.id === projectId) {
        clearProgressPanel();
        clearReviewPanel();
        store.setState({ activeProject: null });
      }
      refreshProjectsAndCredits();
    },
    onDuplicateProject() {
      refreshProjectsAndCredits();
    },
    onRetryProject(project) {
      if (project?.id) {
        openProgressForProject(project);
      }
      refreshProjectsAndCredits();
    },
    onRefreshDiagnostics(diag) {
      if (diag) {
        store.setState({ diagnostics: diag });
      }
    },
    onProjectCreated(created) {
      const target = Array.isArray(created?.projects) ? created.projects[0] : created;
      navigateToView("dashboard");
      if (target?.id) {
        openProgressForProject(target);
      }
      refreshProjectsAndCredits();
    },
  };
}

function closeMobileSidebar() {
  const sidebar = document.getElementById("studio-sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  sidebar?.classList.add("-translate-x-full");
  backdrop?.classList.add("hidden");
}

function openMobileSidebar() {
  const sidebar = document.getElementById("studio-sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  sidebar?.classList.remove("-translate-x-full");
  backdrop?.classList.remove("hidden");
}

function renderActiveView(resolved) {
  const viewContainer = document.getElementById("view-container");
  if (!viewContainer) return;

  const handlers = buildViewHandlers();
  if (resolved === "translator") {
    renderTranslatorView(viewContainer, handlers);
  } else if (resolved === "faceless") {
    renderFacelessView(viewContainer, handlers);
  } else if (resolved === "billing") {
    renderBillingView(viewContainer, handlers);
  } else if (resolved === "settings") {
    renderSettingsView(viewContainer, handlers);
  } else {
    renderDashboardView(viewContainer, handlers);
  }
}

export function navigateToView(viewName, { updateHash = true } = {}) {
  const resolved = VIEW_META[viewName] ? viewName : "dashboard";
  store.setState({ activeView: resolved });
  closeMobileSidebar();

  if (updateHash && typeof window !== "undefined") {
    const targetHash = `#/${resolved}`;
    if (window.location.hash !== targetHash) {
      window.location.hash = targetHash;
    }
  }

  const titleEl = document.getElementById("topbar-title");
  const subtitleEl = document.getElementById("topbar-subtitle");
  if (titleEl) titleEl.textContent = VIEW_META[resolved].title;
  if (subtitleEl) subtitleEl.textContent = VIEW_META[resolved].subtitle;

  document.querySelectorAll("[data-nav]").forEach((btn) => {
    const isActive = btn.getAttribute("data-nav") === resolved;
    btn.classList.toggle("active", isActive);
  });

  renderActiveView(resolved);
}

function syncChromeFromState(state) {
  // Credit balance pill
  const creditEl = document.getElementById("credit-balance-value");
  if (creditEl) {
    const bal = Number(state.credits?.balance || 0);
    creditEl.textContent = bal.toLocaleString("vi-VN");
  }

  // Auth status button
  const authBtn = document.getElementById("auth-action-btn");
  if (authBtn) {
    const dot = authBtn.querySelector("[data-auth-dot]");
    const label = authBtn.querySelector("[data-auth-label]");
    if (state.user) {
      const displayName =
        (state.profile?.display_name && String(state.profile.display_name).trim()) ||
        state.user.email ||
        "Đã đăng nhập";
      if (dot) dot.className = "w-2 h-2 rounded-full bg-emerald-400 shrink-0";
      if (label) label.textContent = `👤 ${displayName}`;
    } else {
      if (dot) dot.className = "w-2 h-2 rounded-full bg-slate-500 shrink-0";
      if (label) label.textContent = "Đăng nhập";
    }
  }

  // Tier badge
  const tierBadge = document.getElementById("user-tier-badge");
  if (tierBadge) {
    const tier = String(
      state.profile?.subscription_tier ||
        state.profile?.tier ||
        state.user?.user_metadata?.subscription_tier ||
        state.user?.app_metadata?.subscription_tier ||
        "free"
    ).toLowerCase();
    tierBadge.textContent = tier.toUpperCase();
    tierBadge.setAttribute("data-tier", tier);
  }

  // Local Sandbox Banner
  const sandboxBanner = document.getElementById("sandbox-mode-banner");
  if (sandboxBanner) {
    const isSandbox = Boolean(state.publicConfig?.local_sandbox_mode);
    if (!isSandbox) {
      sandboxBanner.classList.add("hidden");
      sandboxBanner.innerHTML = "";
    } else {
      sandboxBanner.classList.remove("hidden");
      const isLoggedIn = Boolean(state.accessToken);
      sandboxBanner.innerHTML = isLoggedIn
        ? `
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-2.5 min-w-0">
              <span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 shrink-0">
                LOCAL SANDBOX
              </span>
              <span class="text-xs text-slate-200 truncate">
                Đang chạy chế độ Sandbox cục bộ — Dữ liệu lưu tạm tại <code class="text-cyan-300">temp/sandbox_db.json</code> &amp; lưu trữ tại <code class="text-cyan-300">/local-media</code>.
              </span>
            </div>
            <button
              type="button"
              id="sandbox-open-wallet-btn"
              class="px-3 py-1.5 rounded-xl bg-violet-500/20 hover:bg-violet-500/30 border border-violet-500/40 text-xs font-semibold text-violet-200 transition shrink-0"
            >
              👑 Nâng cấp Gói / Ví Credit
            </button>
          </div>
        `
        : `
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-2.5 min-w-0">
              <span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 shrink-0">
                LOCAL SANDBOX
              </span>
              <span class="text-xs text-slate-200">
                Chế độ <strong>Local Sandbox</strong> đang bật — Trải nghiệm đầy đủ Studio không cần Supabase!
              </span>
            </div>
            <button
              type="button"
              id="sandbox-activate-btn"
              class="px-4 py-1.5 rounded-xl bg-gradient-to-r from-cyan-500 to-violet-500 hover:brightness-110 text-slate-950 text-xs font-bold shadow transition shrink-0"
            >
              ⚡ Kích hoạt Sandbox Demo
            </button>
          </div>
        `;

      sandboxBanner.querySelector("#sandbox-activate-btn")?.addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "Đang kích hoạt...";
        try {
          await loginWithSandbox();
          await refreshProjectsAndCredits();
        } catch (err) {
          showToast(err?.message || "Không thể kích hoạt Sandbox Demo", "error");
          btn.disabled = false;
          btn.textContent = "⚡ Kích hoạt Sandbox Demo";
        }
      });

      sandboxBanner.querySelector("#sandbox-open-wallet-btn")?.addEventListener("click", () => {
        openVietQRModal({
          onTopupCompleted() {
            refreshProjectsAndCredits();
          },
        });
      });
    }
  }

  // SSE Live indicator
  const sseDot = document.getElementById("sse-status-dot");
  const sseLabel = document.getElementById("sse-status-label");
  if (sseDot && sseLabel) {
    if (state.sseConnected) {
      sseDot.className = "w-2 h-2 rounded-full bg-cyan-400 pulse-dot";
      sseLabel.textContent = state.sseStage ? `Live · ${state.sseStage}` : "SSE Live";
    } else {
      sseDot.className = "w-2 h-2 rounded-full bg-slate-500";
      sseLabel.textContent = "Idle";
    }
  }

  // Sidebar recent job shortcut
  const recentBox = document.getElementById("sidebar-recent-job");
  if (recentBox) {
    const candidate =
      state.activeProject ||
      (state.projects || []).find((p) =>
        ["PROCESSING", "RENDERING", "PENDING", "AWAITING_REVIEW"].includes(p.status)
      ) ||
      (state.projects || [])[0];

    if (!candidate) {
      recentBox.innerHTML = `
        <div class="flex items-center justify-between gap-2">
          <span class="font-medium text-slate-300 truncate">Chưa có job đang chạy</span>
        </div>
        <p class="mt-1 text-[11px] text-slate-500">
          Tạo dự án mới hoặc chọn một job từ Dashboard để theo dõi trực tiếp.
        </p>
      `;
    } else {
      const pct = Math.min(100, Math.max(0, Number(candidate.progress_pct || 0)));
      recentBox.innerHTML = `
        <button
          type="button"
          id="sidebar-recent-job-btn"
          class="w-full text-left group space-y-1.5"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="font-semibold text-white group-hover:text-cyan-300 transition truncate">
              ${escapeHtml(candidate.title || "Untitled")}
            </span>
            <span class="text-[10px] font-mono text-cyan-300 shrink-0">${pct}%</span>
          </div>
          <div class="flex items-center justify-between text-[11px]">
            <span class="text-slate-400">${escapeHtml(candidate.status)}</span>
            <span class="text-violet-300 group-hover:underline">Mở nhanh →</span>
          </div>
          <div class="h-1.5 w-full rounded-full bg-[#0B0D12] overflow-hidden">
            <div class="h-full bg-gradient-to-r from-violet-500 to-cyan-400" style="width: ${pct}%"></div>
          </div>
        </button>
      `;

      recentBox.querySelector("#sidebar-recent-job-btn")?.addEventListener("click", async () => {
        closeMobileSidebar();
        if (candidate.status === "AWAITING_REVIEW") {
          try {
            const detail = await api.getProject(candidate.id);
            openReviewForProject(detail);
          } catch (err) {
            showToast(err?.message || "Không thể mở Review Editor", "error");
          }
        } else if (candidate.status === "COMPLETED") {
          openVideoPlayerModal(candidate);
        } else {
          openProgressForProject(candidate);
        }
      });
    }
  }
}

function wireShellControls() {
  document.querySelectorAll("[data-nav]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const view = btn.getAttribute("data-nav");
      navigateToView(view);
    });
  });

  document.getElementById("mobile-sidebar-toggle")?.addEventListener("click", openMobileSidebar);
  document.getElementById("sidebar-close-btn")?.addEventListener("click", closeMobileSidebar);
  document.getElementById("sidebar-backdrop")?.addEventListener("click", closeMobileSidebar);

  document.getElementById("credit-balance-btn")?.addEventListener("click", () => {
    openVietQRModal({
      onTopupCompleted() {
        refreshProjectsAndCredits();
      },
    });
  });

  document.getElementById("auth-action-btn")?.addEventListener("click", () => {
    openAuthModal({
      onSuccess() {
        refreshProjectsAndCredits();
      },
    });
  });

  window.addEventListener("studio:auth-changed", (e) => {
    const token = e?.detail?.accessToken;
    if (token) {
      refreshProjectsAndCredits().then(() => {
        navigateToView(store.getState().activeView || "dashboard");
      });
    } else {
      clearProgressPanel();
      clearReviewPanel();
      navigateToView("dashboard");
    }
  });

  window.addEventListener("studio:unauthorized", () => {
    clearProgressPanel();
    clearReviewPanel();
  });

  window.addEventListener("hashchange", () => {
    const hashedView = parseViewFromHash();
    if (hashedView && hashedView !== store.getState().activeView) {
      navigateToView(hashedView, { updateHash: false });
    }
  });
}

export async function bootstrapApp() {
  if (bootstrapped) return;
  bootstrapped = true;

  wireShellControls();
  store.subscribe(syncChromeFromState);
  syncChromeFromState(store.getState());

  // 1. Load public config first so Supabase Auth / limits are available immediately
  try {
    const publicConfig = await api.getPublicConfig();
    if (publicConfig) {
      store.setState({ publicConfig });
    }
  } catch (err) {
    console.warn("[app] Failed to load /api/v1/public-config:", err);
  }

  // 2. Initialize auth session
  await initAuth();

  // 3. Load providers, system diagnostics & default TTS voices in parallel
  const [providersRes, diagnosticsRes] = await Promise.allSettled([
    api.getProviders(),
    api.getSystemDiagnostics(),
  ]);
  if (diagnosticsRes.status === "fulfilled" && diagnosticsRes.value) {
    store.setState({ diagnostics: diagnosticsRes.value });
  }
  if (providersRes.status === "fulfilled" && providersRes.value) {
    store.setState({ providers: providersRes.value });
    const defaultTts = providersRes.value?.defaults?.tts || "edge_tts";
    try {
      const voiceRes = await api.getTtsVoices(defaultTts);
      if (Array.isArray(voiceRes?.voices)) {
        const prevVoices = store.getState().ttsVoices || {};
        store.setState({
          ttsVoices: { ...prevVoices, [defaultTts]: voiceRes.voices },
        });
      }
    } catch {
      // Voices will be fetched on demand by views/review editor
    }
  }

  // 4. Load billing credits, profile & user projects if logged in
  if (store.getState().accessToken) {
    await refreshProjectsAndCredits();
  }

  // 5. Honor initial URL hash or default Dashboard view
  const initialView = parseViewFromHash() || store.getState().activeView || "dashboard";
  navigateToView(initialView, { updateHash: !parseViewFromHash() });
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      bootstrapApp();
    });
  } else {
    bootstrapApp();
  }
}
