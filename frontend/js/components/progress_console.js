/**
 * Stage 4 Interactive Component: Real-time SSE Progress Console
 * Connects ProjectSSEClient to /api/v1/projects/{id}/events, renders a 6-step
 * studio pipeline stepper, animated % progress bar, color-coded terminal logs,
 * cancel action, and automatic HITL / completion transitions.
 */

import { api } from "../core/api.js";
import { ProjectSSEClient } from "../core/sse_client.js";
import { showToast, store } from "../core/state.js";

let activeConsoleSession = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatClockTime(date = new Date()) {
  try {
    return date.toLocaleTimeString("vi-VN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "--:--:--";
  }
}

function getPipelineSteps(mode) {
  if (mode === "faceless") {
    return [
      { key: "stage1", title: "Stage 1", subtitle: "Kịch bản & Storyboard" },
      { key: "stage2", title: "Stage 2", subtitle: "Visual & Ảnh AI" },
      { key: "stage3", title: "Stage 3", subtitle: "Chuẩn bị Review" },
      { key: "awaiting_review", title: "AWAITING_REVIEW", subtitle: "Duyệt HITL" },
      { key: "render", title: "Final Render", subtitle: "TTS, .ass & FFmpeg" },
      { key: "completed", title: "COMPLETED", subtitle: "Hoàn tất" },
    ];
  }
  return [
    { key: "stage1", title: "Stage 1", subtitle: "Tách Vocal & BGM" },
    { key: "stage2", title: "Stage 2", subtitle: "STT Word-level" },
    { key: "stage3", title: "Stage 3", subtitle: "Dịch thuật AI" },
    { key: "awaiting_review", title: "AWAITING_REVIEW", subtitle: "Duyệt HITL" },
    { key: "render", title: "Final Render", subtitle: "TTS & FFmpeg Mix" },
    { key: "completed", title: "COMPLETED", subtitle: "Hoàn tất" },
  ];
}

function resolveStepIndex(status, stage, progressPct, mode) {
  const s = String(status || "").toUpperCase();
  const st = String(stage || "").toLowerCase();
  const pct = Number(progressPct || 0);

  if (s === "COMPLETED" || st === "completed" || pct >= 100) return 5;
  if (
    s === "RENDERING" ||
    st.includes("render") ||
    st.includes("tts") ||
    st.includes("dub") ||
    st.includes("mix") ||
    st.includes("subtitle") ||
    st.includes("master") ||
    st.includes("upload") ||
    st.includes("stage4") ||
    st.includes("assemble")
  ) {
    return 4;
  }
  if (s === "AWAITING_REVIEW" || st === "awaiting_review") return 3;

  if (mode === "faceless") {
    if (st.includes("visual") || st.includes("stage2") || (pct >= 28 && pct < 55)) return 1;
    if (pct >= 55) return 2;
    return 0;
  }

  if (st.includes("translat") || st.includes("stage3") || pct >= 45) return 2;
  if (st.includes("stt") || st.includes("transcrib") || st.includes("stage2") || pct >= 28) return 1;
  return 0;
}

const LOG_LEVEL_STYLE = {
  INFO: {
    badge: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
    text: "text-slate-200",
  },
  SUCCESS: {
    badge: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
    text: "text-emerald-200",
  },
  WARNING: {
    badge: "bg-amber-500/15 text-amber-300 border-amber-500/30",
    text: "text-amber-200",
  },
  ERROR: {
    badge: "bg-rose-500/15 text-rose-300 border-rose-500/30",
    text: "text-rose-200",
  },
};

const STATUS_BADGE_MAP = {
  PENDING: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  PROCESSING: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  AWAITING_REVIEW: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  RENDERING: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  COMPLETED: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  FAILED: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  CANCELLED: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
};

const CANCELLABLE_STATUSES = new Set(["PENDING", "PROCESSING", "AWAITING_REVIEW", "RENDERING"]);

function upsertProjectInStore(updatedProject) {
  if (!updatedProject?.id) return;
  const state = store.getState();
  const current = state.projects || [];
  const exists = current.some((p) => String(p.id) === String(updatedProject.id));
  const nextProjects = exists
    ? current.map((p) => (String(p.id) === String(updatedProject.id) ? { ...p, ...updatedProject } : p))
    : [updatedProject, ...current];
  const prevActive = state.activeProject;
  const nextActive =
    prevActive && String(prevActive.id) === String(updatedProject.id)
      ? { ...prevActive, ...updatedProject }
      : prevActive;
  store.setState({ projects: nextProjects, activeProject: nextActive });
}

export function destroyProgressConsole() {
  if (activeConsoleSession) {
    activeConsoleSession.destroy();
    activeConsoleSession = null;
  }
}

/**
 * Render and connect the SSE Progress Console for a project.
 * @param {HTMLElement} container
 * @param {object} project
 * @param {object} [callbacks]
 * @param {(projectDetail: object) => void} [callbacks.onAwaitingReview]
 * @param {(projectDetail: object) => void} [callbacks.onCompleted]
 * @param {(project: object) => void} [callbacks.onCancelled]
 * @param {(errData: object) => void} [callbacks.onError]
 * @param {() => void} [callbacks.onClose]
 */
export function renderProgressConsole(container, project, callbacks = {}) {
  if (!container || !project?.id) return null;

  destroyProgressConsole();

  let currentStatus = String(project.status || "PENDING").toUpperCase();
  let currentStage = project.current_stage || "queued";
  let currentPct = Math.min(100, Math.max(0, Number(project.progress_pct || 0)));
  let currentErrorMessage = project.error_message ? String(project.error_message) : "";
  let completedDetail = currentStatus === "COMPLETED" ? project : null;
  let reviewTransitionTriggered = false;
  let completedTransitionTriggered = false;
  let isDisposed = false;
  const logs = [];

  const steps = getPipelineSteps(project.mode);

  container.classList.remove("hidden");
  container.innerHTML = `
    <div class="rounded-2xl studio-glass border border-[#262E42] overflow-hidden shadow-2xl">
      <!-- Header -->
      <div class="px-5 py-4 border-b border-[#262E42] bg-[#141925]/90 flex flex-col md:flex-row md:items-center justify-between gap-3">
        <div class="min-w-0 space-y-1">
          <div class="flex flex-wrap items-center gap-2">
            <span id="pc-live-indicator" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">
              <span class="w-1.5 h-1.5 rounded-full bg-cyan-400 pulse-dot"></span>
              SSE Live
            </span>
            <span id="pc-status-badge" class="px-2.5 py-0.5 rounded-full text-[11px] font-bold border ${STATUS_BADGE_MAP[currentStatus] || STATUS_BADGE_MAP.PENDING}">
              ${escapeHtml(currentStatus)}
            </span>
            <span class="text-[11px] font-mono text-slate-400">
              ${escapeHtml(project.mode === "faceless" ? "Faceless AI" : "Translator")} · ${escapeHtml(project.aspect_ratio || "9:16")}
            </span>
          </div>
          <h3 class="text-base sm:text-lg font-bold text-white truncate">${escapeHtml(project.title || "Untitled Project")}</h3>
          <div class="text-[11px] font-mono text-slate-500 truncate">ID: ${escapeHtml(project.id)}</div>
        </div>

        <div class="flex flex-wrap items-center gap-2 shrink-0">
          <button
            id="pc-open-review-btn"
            type="button"
            class="hidden px-3.5 py-2 rounded-xl text-xs font-semibold bg-amber-500/20 hover:bg-amber-500/30 text-amber-200 border border-amber-500/40 transition"
          >
            ✏️ Mở Review Editor
          </button>
          <button
            id="pc-open-video-btn"
            type="button"
            class="hidden px-3.5 py-2 rounded-xl text-xs font-semibold bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200 border border-emerald-500/40 transition"
          >
            ▶ Xem &amp; Tải Video
          </button>
          <button
            id="pc-cancel-btn"
            type="button"
            class="${CANCELLABLE_STATUSES.has(currentStatus) ? "" : "hidden"} px-3.5 py-2 rounded-xl text-xs font-semibold bg-rose-500/15 hover:bg-rose-500/25 text-rose-300 border border-rose-500/40 transition"
          >
            ✕ Hủy Job
          </button>
          <button
            id="pc-close-btn"
            type="button"
            class="px-3 py-2 rounded-xl text-xs font-medium bg-[#191F2E] hover:bg-[#20273A] text-slate-300 border border-[#262E42] transition"
            title="Thu gọn bảng tiến trình"
          >
            Đóng
          </button>
        </div>
      </div>

      <div class="p-5 space-y-5">
        <!-- Failure Recovery Banner (FAILED) -->
        <div
          id="pc-error-actions"
          class="${currentStatus === "FAILED" ? "" : "hidden"} rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 flex flex-col sm:flex-row sm:items-center justify-between gap-3"
        >
          <div class="min-w-0 space-y-0.5">
            <div class="text-xs font-bold text-rose-300 flex items-center gap-1.5">
              <span>⚠️</span>
              <span>Pipeline gặp lỗi trong quá trình xử lý</span>
            </div>
            <div id="pc-error-message" class="text-xs text-rose-200/90 break-words">
              ${escapeHtml(
                currentErrorMessage ||
                  "Đã xảy ra lỗi không xác định. Hãy kiểm tra terminal log hoặc cài đặt API key."
              )}
            </div>
          </div>
          <div class="flex flex-wrap items-center gap-2 shrink-0">
            <button
              id="pc-retry-btn"
              type="button"
              class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-rose-500/20 hover:bg-rose-500/30 text-rose-100 border border-rose-400/40 transition"
            >
              🔄 Chạy lại dự án
            </button>
            <button
              id="pc-open-settings-btn"
              type="button"
              class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-[#191F2E] hover:bg-[#20273A] text-cyan-300 border border-[#262E42] transition"
            >
              ⚙️ Mở Cài đặt API Key
            </button>
          </div>
        </div>

        <!-- CapCut/Runway Stage Stepper -->
        <div id="pc-stepper" class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2.5"></div>

        <!-- Animated Progress Bar -->
        <div class="space-y-2">
          <div class="flex items-center justify-between text-xs">
            <div class="flex items-center gap-2 min-w-0">
              <span class="text-slate-400">Giai đoạn hiện tại:</span>
              <span id="pc-stage-label" class="font-mono font-semibold text-cyan-300 truncate">${escapeHtml(currentStage)}</span>
            </div>
            <span id="pc-pct-label" class="font-mono font-bold text-sm text-white shrink-0">${currentPct}%</span>
          </div>
          <div class="h-3 w-full rounded-full bg-[#0B0D12] border border-[#262E42] overflow-hidden p-[1px]">
            <div
              id="pc-progress-bar"
              class="h-full rounded-full bg-gradient-to-r from-violet-500 via-fuchsia-500 to-cyan-400 transition-all duration-300 ease-out progress-shimmer"
              style="width: ${currentPct}%"
            ></div>
          </div>
        </div>

        <!-- Live Terminal Log Stream -->
        <div class="rounded-xl bg-[#090B10] border border-[#262E42] overflow-hidden">
          <div class="px-3.5 py-2 border-b border-[#262E42] bg-[#10141F] flex items-center justify-between gap-2">
            <div class="flex items-center gap-2">
              <span class="w-2.5 h-2.5 rounded-full bg-rose-500/80"></span>
              <span class="w-2.5 h-2.5 rounded-full bg-amber-500/80"></span>
              <span class="w-2.5 h-2.5 rounded-full bg-emerald-500/80"></span>
              <span class="ml-1 text-[11px] font-mono font-semibold uppercase tracking-wider text-slate-400">
                Pipeline Terminal Stream
              </span>
            </div>
            <div class="flex items-center gap-2">
              <span id="pc-log-count" class="text-[11px] font-mono text-slate-500">0 dòng</span>
              <button
                id="pc-clear-logs-btn"
                type="button"
                class="text-[11px] text-slate-400 hover:text-white px-2 py-0.5 rounded bg-[#191F2E] border border-[#262E42] transition"
              >
                Xóa log
              </button>
            </div>
          </div>
          <div
            id="pc-log-stream"
            class="h-56 overflow-y-auto studio-scroll p-3.5 font-mono text-xs space-y-1.5"
          ></div>
        </div>
      </div>
    </div>
  `;

  const stepperEl = container.querySelector("#pc-stepper");
  const statusBadgeEl = container.querySelector("#pc-status-badge");
  const liveIndicatorEl = container.querySelector("#pc-live-indicator");
  const errorActionsEl = container.querySelector("#pc-error-actions");
  const errorMessageEl = container.querySelector("#pc-error-message");
  const retryBtn = container.querySelector("#pc-retry-btn");
  const openSettingsBtn = container.querySelector("#pc-open-settings-btn");
  const stageLabelEl = container.querySelector("#pc-stage-label");
  const pctLabelEl = container.querySelector("#pc-pct-label");
  const progressBarEl = container.querySelector("#pc-progress-bar");
  const logStreamEl = container.querySelector("#pc-log-stream");
  const logCountEl = container.querySelector("#pc-log-count");
  const cancelBtn = container.querySelector("#pc-cancel-btn");
  const closeBtn = container.querySelector("#pc-close-btn");
  const clearLogsBtn = container.querySelector("#pc-clear-logs-btn");
  const openReviewBtn = container.querySelector("#pc-open-review-btn");
  const openVideoBtn = container.querySelector("#pc-open-video-btn");

  const renderStepper = () => {
    if (!stepperEl) return;
    const activeIdx = resolveStepIndex(currentStatus, currentStage, currentPct, project.mode);
    const isError = currentStatus === "FAILED";
    const isCancelled = currentStatus === "CANCELLED";

    stepperEl.innerHTML = steps
      .map((step, idx) => {
        const isDone =
          currentStatus === "COMPLETED" ||
          idx < activeIdx ||
          (step.key === "awaiting_review" && currentStatus === "AWAITING_REVIEW" && idx < activeIdx);
        const isCurrent = idx === activeIdx && currentStatus !== "COMPLETED";

        let cardClass = "bg-[#0F131C] border-[#262E42] text-slate-500";
        let numClass = "bg-[#191F2E] text-slate-400 border-[#262E42]";
        let icon = String(idx + 1);

        if (isError && isCurrent) {
          cardClass = "bg-rose-500/10 border-rose-500/50 text-rose-200";
          numClass = "bg-rose-500/20 text-rose-300 border-rose-500/40";
          icon = "!";
        } else if (isCancelled && isCurrent) {
          cardClass = "bg-zinc-500/10 border-zinc-500/40 text-zinc-300";
          numClass = "bg-zinc-600/20 text-zinc-300 border-zinc-500/40";
          icon = "✕";
        } else if (isDone) {
          cardClass = "bg-emerald-500/10 border-emerald-500/35 text-emerald-200";
          numClass = "bg-emerald-500/20 text-emerald-300 border-emerald-500/40";
          icon = "✓";
        } else if (isCurrent) {
          const isReview = step.key === "awaiting_review";
          cardClass = isReview
            ? "bg-amber-500/10 border-amber-500/50 text-amber-200 shadow-lg shadow-amber-500/10"
            : "bg-violet-500/15 border-violet-500/50 text-white shadow-lg shadow-violet-500/10";
          numClass = isReview
            ? "bg-amber-500 text-slate-950 border-amber-400"
            : "bg-gradient-to-br from-violet-500 to-cyan-400 text-slate-950 border-transparent";
        }

        return `
          <div class="rounded-xl border px-3 py-2.5 transition-all ${cardClass}">
            <div class="flex items-center gap-2">
              <span class="w-5 h-5 rounded-full border text-[11px] font-bold flex items-center justify-center shrink-0 ${numClass}">
                ${icon}
              </span>
              <span class="text-xs font-bold truncate">${escapeHtml(step.title)}</span>
            </div>
            <div class="mt-1 text-[11px] opacity-85 truncate pl-7">${escapeHtml(step.subtitle)}</div>
          </div>
        `;
      })
      .join("");
  };

  const updateProgressUI = ({ status, stage, progressPct, errorMessage }) => {
    if (status) {
      currentStatus = String(status).toUpperCase();
      if (currentStatus !== "FAILED" && errorMessage === undefined) {
        currentErrorMessage = "";
      }
    }
    if (errorMessage !== undefined && errorMessage !== null && String(errorMessage).trim() !== "") {
      currentErrorMessage = String(errorMessage).trim();
    }
    if (stage !== undefined && stage !== null && String(stage).trim() !== "") {
      currentStage = String(stage);
    }
    if (progressPct !== undefined && progressPct !== null && !Number.isNaN(Number(progressPct))) {
      currentPct = Math.min(100, Math.max(0, Math.round(Number(progressPct))));
    }

    if (statusBadgeEl) {
      statusBadgeEl.textContent = currentStatus;
      statusBadgeEl.className = `px-2.5 py-0.5 rounded-full text-[11px] font-bold border ${
        STATUS_BADGE_MAP[currentStatus] || STATUS_BADGE_MAP.PENDING
      }`;
    }
    if (stageLabelEl) {
      stageLabelEl.textContent = currentStage;
    }
    if (pctLabelEl) {
      pctLabelEl.textContent = `${currentPct}%`;
    }
    if (progressBarEl) {
      progressBarEl.style.width = `${currentPct}%`;
      if (["COMPLETED", "FAILED", "CANCELLED", "AWAITING_REVIEW"].includes(currentStatus)) {
        progressBarEl.classList.remove("progress-shimmer");
      } else {
        progressBarEl.classList.add("progress-shimmer");
      }
    }
    if (cancelBtn) {
      cancelBtn.classList.toggle("hidden", !CANCELLABLE_STATUSES.has(currentStatus));
    }
    if (openReviewBtn) {
      openReviewBtn.classList.toggle("hidden", currentStatus !== "AWAITING_REVIEW");
    }
    if (openVideoBtn) {
      openVideoBtn.classList.toggle("hidden", currentStatus !== "COMPLETED");
    }
    if (errorActionsEl) {
      errorActionsEl.classList.toggle("hidden", currentStatus !== "FAILED");
    }
    if (errorMessageEl) {
      errorMessageEl.textContent =
        currentErrorMessage ||
        "Đã xảy ra lỗi không xác định. Hãy kiểm tra terminal log hoặc cài đặt API key.";
    }

    upsertProjectInStore({
      id: project.id,
      status: currentStatus,
      current_stage: currentStage,
      progress_pct: currentPct,
      error_message: currentStatus === "FAILED" ? currentErrorMessage || null : null,
    });

    renderStepper();
  };

  const appendLog = (level, message, stageTag = "") => {
    if (!logStreamEl || isDisposed) return;
    const normLevel = LOG_LEVEL_STYLE[String(level || "INFO").toUpperCase()]
      ? String(level).toUpperCase()
      : "INFO";
    const style = LOG_LEVEL_STYLE[normLevel];
    const ts = formatClockTime();

    logs.push({ ts, level: normLevel, stageTag, message });
    if (logs.length > 300) {
      logs.shift();
      if (logStreamEl.firstElementChild) {
        logStreamEl.firstElementChild.remove();
      }
    }

    const row = document.createElement("div");
    row.className = "flex items-start gap-2 leading-relaxed";
    row.innerHTML = `
      <span class="text-slate-500 shrink-0">[${escapeHtml(ts)}]</span>
      <span class="px-1.5 py-0.2 rounded border text-[10px] font-bold shrink-0 ${style.badge}">${escapeHtml(normLevel)}</span>
      ${
        stageTag
          ? `<span class="text-violet-300/80 shrink-0">[${escapeHtml(stageTag)}]</span>`
          : ""
      }
      <span class="break-words ${style.text}">${escapeHtml(message)}</span>
    `;
    logStreamEl.appendChild(row);
    logStreamEl.scrollTop = logStreamEl.scrollHeight;

    if (logCountEl) {
      logCountEl.textContent = `${logs.length} dòng`;
    }
    store.setState({ sseLogs: logs.slice(-80) });
  };

  const handleAwaitingReviewTransition = async () => {
    if (reviewTransitionTriggered || isDisposed) return;
    reviewTransitionTriggered = true;
    updateProgressUI({ status: "AWAITING_REVIEW", stage: "awaiting_review" });
    appendLog("SUCCESS", "Đã hoàn tất tiền xử lý! Đang tải dữ liệu scene để Review HITL...", "awaiting_review");

    try {
      const detail = await api.getProject(project.id);
      if (isDisposed) return;
      store.setState({ activeProject: detail });
      upsertProjectInStore(detail);
      callbacks.onAwaitingReview?.(detail);
    } catch (err) {
      appendLog("ERROR", err?.message || "Không thể tải chi tiết dự án để mở Review Editor", "awaiting_review");
      showToast(err?.message || "Không thể tải chi tiết Review", "error");
    }
  };

  const handleCompletedTransition = async (eventData = {}) => {
    if (completedTransitionTriggered || isDisposed) return;
    completedTransitionTriggered = true;
    updateProgressUI({ status: "COMPLETED", stage: "completed", progressPct: 100 });
    appendLog("SUCCESS", "Render hoàn tất! Đang đồng bộ video đầu ra và số dư Credit...", "completed");

    try {
      const [detail, creditsResult] = await Promise.allSettled([
        api.getProject(project.id),
        api.getBillingCredits(),
      ]);

      if (creditsResult.status === "fulfilled" && creditsResult.value) {
        store.setState({ credits: creditsResult.value });
      }

      if (isDisposed) return;
      const resolvedDetail =
        detail.status === "fulfilled" && detail.value
          ? detail.value
          : {
              ...project,
              status: "COMPLETED",
              progress_pct: 100,
              output_video_url: eventData.output_video_url || project.output_video_url,
            };

      completedDetail = resolvedDetail;
      store.setState({ activeProject: resolvedDetail });
      upsertProjectInStore(resolvedDetail);
      callbacks.onCompleted?.(resolvedDetail);
    } catch (err) {
      appendLog("ERROR", err?.message || "Lỗi đồng bộ thông tin video hoàn tất", "completed");
    }
  };

  // Initial render
  updateProgressUI({
    status: currentStatus,
    stage: currentStage,
    progressPct: currentPct,
  });
  appendLog("INFO", `Đang kết nối SSE stream cho dự án "${project.title || project.id}"...`, "sse");

  const sseClient = new ProjectSSEClient(project.id, {
    onConnectionChange(connected) {
      if (!liveIndicatorEl || isDisposed) return;
      if (connected) {
        liveIndicatorEl.className =
          "inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-cyan-500/15 text-cyan-300 border border-cyan-500/30";
        liveIndicatorEl.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-cyan-400 pulse-dot"></span> SSE Live`;
      } else {
        liveIndicatorEl.className =
          "inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-slate-500/15 text-slate-400 border border-slate-500/30";
        liveIndicatorEl.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-slate-400"></span> ${
          ["COMPLETED", "AWAITING_REVIEW", "FAILED", "CANCELLED"].includes(currentStatus) ? "Stream Closed" : "Reconnecting"
        }`;
      }
    },

    onStatus(data) {
      if (!data) return;
      updateProgressUI({
        status: data.status,
        stage: data.current_stage,
        progressPct: data.progress_pct,
        errorMessage: data.error_message,
      });
      appendLog(
        "INFO",
        `Trạng thái: ${data.status || currentStatus} (${data.progress_pct ?? currentPct}%)`,
        data.current_stage || "snapshot"
      );
      if (data.error_message) {
        appendLog("ERROR", data.error_message, data.current_stage || "error");
      }
    },

    onStageStarted(data) {
      updateProgressUI({
        stage: data?.stage || currentStage,
        progressPct: data?.progress_pct ?? currentPct,
      });
      appendLog("INFO", data?.label || data?.message || `Bắt đầu ${data?.stage || "stage"}`, data?.stage || "");
    },

    onProgress(data) {
      updateProgressUI({
        stage: data?.stage || currentStage,
        progressPct: data?.progress_pct ?? currentPct,
      });
      if (data?.message) {
        appendLog(data?.level || "INFO", data.message, data?.stage || "");
      }
    },

    onLog(data) {
      const msg = typeof data === "string" ? data : data?.message || JSON.stringify(data);
      const level = data?.level || data?.severity || "INFO";
      appendLog(level, msg, data?.stage || "");
    },

    onAwaitingReview(data) {
      updateProgressUI({
        status: "AWAITING_REVIEW",
        stage: "awaiting_review",
        progressPct: data?.progress_pct ?? Math.max(currentPct, 55),
      });
      handleAwaitingReviewTransition();
    },

    onCompleted(data) {
      handleCompletedTransition(data || {});
    },

    onCancelled(data) {
      updateProgressUI({ status: "CANCELLED", stage: "cancelled" });
      appendLog("WARNING", data?.refunded ? "Dự án đã hủy và hoàn lại Credit." : "Dự án đã bị hủy.", "cancelled");
      api.getBillingCredits().then((c) => store.setState({ credits: c })).catch(() => {});
      callbacks.onCancelled?.(data);
    },

    onError(data) {
      const msg = data?.message || currentErrorMessage || "Lỗi xử lý pipeline";
      const isStreamFailure = data?.fatal || data?.status >= 400 || data?.code;
      if (!data?.retryable) {
        updateProgressUI({
          status: "FAILED",
          stage: "failed",
          errorMessage: data?.message || currentErrorMessage || msg,
        });
        api.getBillingCredits().then((c) => store.setState({ credits: c })).catch(() => {});
      }
      appendLog(data?.retryable ? "WARNING" : "ERROR", msg, data?.code || "error");
      if (isStreamFailure && !data?.retryable) {
        showToast(msg, "error");
        callbacks.onError?.(data);
      }
    },
  });

  sseClient.connect();

  clearLogsBtn?.addEventListener("click", () => {
    logs.length = 0;
    if (logStreamEl) logStreamEl.innerHTML = "";
    if (logCountEl) logCountEl.textContent = "0 dòng";
  });

  openReviewBtn?.addEventListener("click", () => {
    reviewTransitionTriggered = false;
    handleAwaitingReviewTransition();
  });

  openVideoBtn?.addEventListener("click", () => {
    if (completedDetail) {
      callbacks.onCompleted?.(completedDetail);
    } else {
      completedTransitionTriggered = false;
      handleCompletedTransition({});
    }
  });

  cancelBtn?.addEventListener("click", async () => {
    if (!cancelBtn) return;
    cancelBtn.disabled = true;
    const origText = cancelBtn.textContent;
    cancelBtn.textContent = "Đang hủy...";
    try {
      const updated = await api.cancelProject(project.id);
      sseClient.close();
      updateProgressUI({ status: updated.status || "CANCELLED", stage: updated.current_stage || "cancelled" });
      upsertProjectInStore(updated);
      appendLog("WARNING", "Đã gửi lệnh hủy job thành công.", "cancelled");
      showToast(`Đã hủy dự án "${project.title || project.id}"`, "success");
      api.getBillingCredits().then((c) => store.setState({ credits: c })).catch(() => {});
      callbacks.onCancelled?.(updated);
    } catch (err) {
      showToast(err?.message || "Không thể hủy dự án", "error");
      appendLog("ERROR", err?.message || "Hủy job thất bại", "cancel");
      cancelBtn.disabled = false;
      cancelBtn.textContent = origText;
    }
  });

  const controller = {
    client: sseClient,
    destroy() {
      isDisposed = true;
      sseClient.close();
    },
  };
  activeConsoleSession = controller;

  retryBtn?.addEventListener("click", async () => {
    if (!retryBtn) return;
    retryBtn.disabled = true;
    const origText = retryBtn.textContent;
    retryBtn.textContent = "Đang khởi chạy lại...";
    try {
      const retried = await api.retryProject(project.id);
      const nextProject = {
        ...project,
        ...(retried || {}),
        id: project.id,
        status: retried?.status || "PENDING",
        current_stage: retried?.current_stage || "queued",
        progress_pct: retried?.progress_pct ?? 0,
        error_message: null,
      };
      upsertProjectInStore(nextProject);
      showToast(`Đã khởi chạy lại dự án "${nextProject.title || project.id}"`, "success");
      api
        .listProjects({ limit: 50 })
        .then((list) => {
          if (Array.isArray(list)) store.setState({ projects: list });
        })
        .catch(() => {});
      renderProgressConsole(container, nextProject, callbacks);
    } catch (err) {
      showToast(err?.message || "Không thể chạy lại dự án", "error");
      appendLog("ERROR", err?.message || "Chạy lại dự án thất bại", "retry");
      retryBtn.disabled = false;
      retryBtn.textContent = origText;
    }
  });

  openSettingsBtn?.addEventListener("click", () => {
    controller.destroy();
    if (activeConsoleSession === controller) {
      activeConsoleSession = null;
    }
    container.innerHTML = "";
    container.classList.add("hidden");
    callbacks.onClose?.();
    window.location.hash = "#/settings";
  });

  closeBtn?.addEventListener("click", () => {
    controller.destroy();
    if (activeConsoleSession === controller) {
      activeConsoleSession = null;
    }
    container.innerHTML = "";
    container.classList.add("hidden");
    callbacks.onClose?.();
  });

  return controller;
}
