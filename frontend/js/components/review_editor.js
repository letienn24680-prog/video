/**
 * Stage 4 Interactive Component: Human-in-the-Loop (AWAITING_REVIEW) Editor
 * Supports both `translator` (edited_text / dubbing) and `faceless` (narration_text / visual storyboard).
 */

import { api } from "../core/api.js";
import { confirmDialog, showToast, store } from "../core/state.js";
import { renderLivePreviewPlayer } from "./live_preview_player.js";

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

const VISUAL_PROVIDERS = [
  { value: "pexels", label: "Pexels Stock" },
  { value: "pixabay", label: "Pixabay Stock" },
  { value: "flux", label: "Flux AI Image" },
  { value: "stable_diffusion", label: "Stable Diffusion AI" },
];

const VIDEO_EXTENSIONS = [".mp4", ".mov", ".mkv", ".webm", ".m4v"];
const IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp", ".gif"];

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDurationMs(startMs, endMs) {
  const diff = Math.max(0, Number(endMs || 0) - Number(startMs || 0));
  return `${(diff / 1000).toFixed(2)}s`;
}

function isBrowserPlayableUrl(url) {
  if (!url) return false;
  const s = String(url).trim();
  return s.startsWith("http://") || s.startsWith("https://") || s.startsWith("/") || s.startsWith("blob:") || s.startsWith("data:");
}

function detectMediaKind(url, visualSource) {
  const lower = String(url || "").toLowerCase().split("?")[0];
  if (VIDEO_EXTENSIONS.some((ext) => lower.endsWith(ext))) return "video";
  if (IMAGE_EXTENSIONS.some((ext) => lower.endsWith(ext))) return "image";
  if (visualSource === "ai_gen") return "image";
  return "video";
}

function upsertProjectInStore(updatedProject) {
  if (!updatedProject?.id) return;
  const current = store.getState().projects || [];
  const exists = current.some((p) => String(p.id) === String(updatedProject.id));
  const nextProjects = exists
    ? current.map((p) => (String(p.id) === String(updatedProject.id) ? { ...p, ...updatedProject } : p))
    : [updatedProject, ...current];
  store.setState({ projects: nextProjects });
}

function buildVoiceOptionsHtml(voices, selectedVoiceId) {
  const options = ['<option value="">Mặc định theo cấu hình</option>'];
  const seen = new Set([""]);

  for (const v of voices) {
    const id = String(v.voice_id || v.id || "");
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const label = v.name || v.display_name || id;
    const lang = v.language ? ` (${v.language})` : "";
    const gender = v.gender ? ` · ${v.gender}` : "";
    const selected = id === selectedVoiceId ? "selected" : "";
    options.push(
      `<option value="${escapeHtml(id)}" ${selected}>${escapeHtml(`${label}${lang}${gender}`)}</option>`
    );
  }

  if (selectedVoiceId && !seen.has(selectedVoiceId)) {
    options.push(
      `<option value="${escapeHtml(selectedVoiceId)}" selected>${escapeHtml(selectedVoiceId)}</option>`
    );
  }

  return options.join("");
}

function renderVisualPreviewHtml(scene) {
  const url = scene.visual_url;
  const srcTag = scene.visual_source ? escapeHtml(scene.visual_source) : "none";

  if (!url) {
    return `
      <div class="w-full h-36 rounded-xl bg-[#0B0D12] border border-dashed border-[#262E42] flex flex-col items-center justify-center text-center p-3 text-slate-500">
        <span class="text-xl mb-1">🎞️</span>
        <span class="text-[11px]">Chưa có visual nền</span>
      </div>
    `;
  }

  if (!isBrowserPlayableUrl(url)) {
    const fileName = String(url).split(/[/\\]/).pop() || url;
    return `
      <div class="w-full h-36 rounded-xl bg-[#0B0D12] border border-[#262E42] flex flex-col items-center justify-center text-center p-3">
        <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-cyan-500/15 text-cyan-300 border border-cyan-500/30 mb-1.5">
          ${srcTag}
        </span>
        <span class="text-xs font-medium text-slate-200 truncate max-w-full" title="${escapeHtml(url)}">
          ${escapeHtml(fileName)}
        </span>
        <span class="text-[10px] text-slate-500 mt-1">Tệp đã lưu trên máy chủ</span>
      </div>
    `;
  }

  const kind = detectMediaKind(url, scene.visual_source);
  if (kind === "image") {
    return `
      <div class="relative w-full h-36 rounded-xl overflow-hidden bg-[#0B0D12] border border-[#262E42] group">
        <img
          src="${escapeHtml(url)}"
          alt="Scene ${scene.scene_index + 1}"
          class="w-full h-full object-cover"
          loading="lazy"
        />
        <span class="absolute top-2 left-2 px-2 py-0.5 rounded text-[10px] font-mono bg-black/70 text-cyan-300 border border-cyan-500/30">
          ${srcTag}
        </span>
      </div>
    `;
  }

  return `
    <div class="relative w-full h-36 rounded-xl overflow-hidden bg-[#0B0D12] border border-[#262E42]">
      <video
        src="${escapeHtml(url)}"
        controls
        muted
        playsinline
        preload="metadata"
        class="w-full h-full object-cover"
      ></video>
      <span class="absolute top-2 left-2 px-2 py-0.5 rounded text-[10px] font-mono bg-black/70 text-cyan-300 border border-cyan-500/30 pointer-events-none">
        ${srcTag}
      </span>
    </div>
  `;
}

/**
 * Render the interactive HITL Review Editor into container.
 * @param {HTMLElement} container
 * @param {object} projectDetail
 * @param {object} [callbacks]
 * @param {(updatedProject: object) => void} [callbacks.onRenderStarted]
 * @param {() => void} [callbacks.onClose]
 */
export function renderReviewEditor(container, projectDetail, callbacks = {}) {
  if (!container || !projectDetail?.id) return null;

  const isTranslator = projectDetail.mode === "translator";
  const textField = isTranslator ? "edited_text" : "narration_text";
  const pipelineConfig = { ...(projectDetail.pipeline_config || {}) };
  const ttsProvider =
    pipelineConfig.tts_provider ||
    store.getState().providers?.defaults?.tts ||
    "edge_tts";
  const defaultVisualProvider =
    pipelineConfig.visual_provider ||
    store.getState().providers?.defaults?.visual ||
    "pexels";

  const scenes = [...(projectDetail.scenes || [])].sort(
    (a, b) => Number(a.scene_index ?? 0) - Number(b.scene_index ?? 0)
  );
  const sceneMap = new Map(scenes.map((s) => [String(s.id), { ...s }]));
  const speakerVoices = { ...(pipelineConfig.speaker_voices || {}) };
  let burnSubtitles = pipelineConfig.burn_subtitles !== false;

  const debounceTimers = new Map();
  const dirtyScenes = new Map(); // sceneId -> pending patch object
  const activeAudioUrls = new Map(); // sceneId -> objectUrl
  let livePlayer = null;

  const buildLiveDetailSnapshot = () => ({
    ...projectDetail,
    pipeline_config: {
      ...pipelineConfig,
      burn_subtitles: burnSubtitles,
      speaker_voices: speakerVoices,
    },
    scenes: Array.from(sceneMap.values()).sort(
      (a, b) => Number(a.scene_index ?? 0) - Number(b.scene_index ?? 0)
    ),
  });

  const refreshLivePreview = () => {
    livePlayer?.updateData(buildLiveDetailSnapshot());
  };

  const cleanupResources = () => {
    livePlayer?.destroy();
    livePlayer = null;
    for (const timer of debounceTimers.values()) {
      clearTimeout(timer);
    }
    debounceTimers.clear();
    for (const url of activeAudioUrls.values()) {
      try {
        URL.revokeObjectURL(url);
      } catch {
        // Ignore
      }
    }
    activeAudioUrls.clear();
  };

  let cachedVoices = store.getState().ttsVoices?.[ttsProvider] || [];

  // Extract unique speaker labels (always keep at least a default group if scenes exist)
  const speakerLabels = Array.from(
    new Set(
      scenes
        .map((s) => (s.speaker_label || "").trim())
        .filter(Boolean)
    )
  );

  container.classList.remove("hidden");
  container.innerHTML = `
    <div class="rounded-2xl studio-glass border border-[#262E42] overflow-hidden shadow-2xl">
      <!-- Top HITL Toolbar -->
      <div class="px-5 py-4 border-b border-[#262E42] bg-[#141925]/95 space-y-4">
        <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-3">
          <div class="min-w-0 space-y-1">
            <div class="flex flex-wrap items-center gap-2">
              <span class="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/40">
                AWAITING_REVIEW
              </span>
              <span class="px-2.5 py-0.5 rounded-full text-[11px] font-semibold ${
                isTranslator
                  ? "bg-cyan-500/15 text-cyan-300 border border-cyan-500/30"
                  : "bg-violet-500/15 text-violet-300 border border-violet-500/30"
              }">
                ${isTranslator ? "Translator & Dubbing" : "Faceless Storyboard"}
              </span>
              <span class="text-xs font-mono text-slate-400">
                <span id="re-scene-count-badge">${scenes.length}</span> scenes · ${escapeHtml(projectDetail.aspect_ratio || "9:16")}
              </span>
            </div>
            <h3 class="text-lg font-bold text-white truncate">${escapeHtml(projectDetail.title || "Review Project")}</h3>
          </div>

          <div class="flex flex-wrap items-center gap-2.5 shrink-0">
            <label class="inline-flex items-center gap-2 px-3 py-2 rounded-xl bg-[#191F2E] border border-[#262E42] text-xs font-medium text-slate-200 cursor-pointer select-none">
              <input
                id="re-burn-subtitles"
                type="checkbox"
                ${burnSubtitles ? "checked" : ""}
                class="rounded border-[#262E42] bg-[#0B0D12] text-violet-500 focus:ring-violet-500"
              />
              <span>Burn Phụ Đề (.ass)</span>
            </label>

            <button
              id="re-save-all-btn"
              type="button"
              class="px-3 py-2 rounded-xl text-xs font-semibold bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-300 border border-emerald-500/35 transition"
              title="Lưu ngay mọi thay đổi chưa đồng bộ"
            >
              💾 Lưu tất cả
            </button>

            <button
              id="re-add-scene-btn"
              type="button"
              class="px-3 py-2 rounded-xl text-xs font-semibold bg-[#191F2E] hover:bg-[#20273A] text-amber-300 border border-[#262E42] transition"
              title="Thêm một phân cảnh mới vào cuối timeline"
            >
              ➕ Thêm phân cảnh mới
            </button>

            <button
              id="re-export-srt-btn"
              type="button"
              class="px-3 py-2 rounded-xl text-xs font-semibold bg-[#191F2E] hover:bg-[#20273A] text-cyan-300 border border-[#262E42] transition"
              title="Tải xuống phụ đề .SRT"
            >
              ⬇ Xuất .SRT
            </button>

            <button
              id="re-export-ass-btn"
              type="button"
              class="px-3 py-2 rounded-xl text-xs font-semibold bg-[#191F2E] hover:bg-[#20273A] text-violet-300 border border-[#262E42] transition"
              title="Tải xuống phụ đề Karaoke .ASS"
            >
              ⬇ Xuất .ASS
            </button>

            <button
              id="re-close-btn"
              type="button"
              class="px-3.5 py-2 rounded-xl text-xs font-medium bg-[#191F2E] hover:bg-[#20273A] text-slate-300 border border-[#262E42] transition"
            >
              Đóng
            </button>

            <button
              id="re-confirm-render-btn"
              type="button"
              class="px-5 py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-gradient-to-r from-violet-600 via-fuchsia-600 to-cyan-500 hover:brightness-110 text-white shadow-lg shadow-violet-500/25 transition flex items-center gap-2"
            >
              <span>🎬</span>
              <span id="re-confirm-label">Confirm &amp; Render Video</span>
            </button>
          </div>
        </div>

        <!-- Speaker-level Bulk Voice Assignment -->
        <div id="re-bulk-speakers" class="rounded-xl bg-[#0B0D12]/80 border border-[#262E42] p-3.5">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-2.5">
            <div class="text-xs font-semibold text-slate-200 flex items-center gap-2">
              <span class="text-cyan-400">🎙️</span>
              Gán giọng đọc hàng loạt theo Speaker (TTS: <code class="text-cyan-300">${escapeHtml(ttsProvider)}</code>)
            </div>
            <span id="re-voice-load-status" class="text-[11px] text-slate-400">
              ${cachedVoices.length ? `${cachedVoices.length} giọng sẵn sàng` : "Đang tải danh sách giọng..."}
            </span>
          </div>

          <div id="re-speaker-rows" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2.5">
            ${
              speakerLabels.length > 0
                ? speakerLabels
                    .map(
                      (sp) => `
                  <div class="flex items-center gap-2 rounded-lg bg-[#121620] border border-[#262E42] p-2" data-bulk-speaker-row="${escapeHtml(sp)}">
                    <span class="px-2 py-1 rounded bg-violet-500/15 text-violet-300 border border-violet-500/30 text-[11px] font-semibold shrink-0 max-w-[110px] truncate" title="${escapeHtml(sp)}">
                      ${escapeHtml(sp)}
                    </span>
                    <select
                      data-bulk-speaker-select="${escapeHtml(sp)}"
                      class="re-voice-select flex-1 min-w-0 px-2.5 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400"
                    >
                      ${buildVoiceOptionsHtml(cachedVoices, speakerVoices[sp] || pipelineConfig.voice_id || "")}
                    </select>
                    <button
                      type="button"
                      data-bulk-speaker-apply="${escapeHtml(sp)}"
                      class="px-2.5 py-1.5 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 border border-cyan-500/30 text-[11px] font-semibold shrink-0 transition"
                    >
                      Áp dụng
                    </button>
                  </div>
                `
                    )
                    .join("")
                : `
                  <div class="flex items-center gap-2 rounded-lg bg-[#121620] border border-[#262E42] p-2 md:col-span-2" data-bulk-speaker-row="__all__">
                    <span class="px-2 py-1 rounded bg-violet-500/15 text-violet-300 border border-violet-500/30 text-[11px] font-semibold shrink-0">
                      Toàn bộ Scene
                    </span>
                    <select
                      data-bulk-speaker-select="__all__"
                      class="re-voice-select flex-1 min-w-0 px-2.5 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400"
                    >
                      ${buildVoiceOptionsHtml(cachedVoices, pipelineConfig.voice_id || "")}
                    </select>
                    <button
                      type="button"
                      data-bulk-speaker-apply="__all__"
                      class="px-2.5 py-1.5 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 border border-cyan-500/30 text-[11px] font-semibold shrink-0 transition"
                    >
                      Áp dụng tất cả
                    </button>
                  </div>
                `
            }
          </div>
        </div>
      </div>

      <!-- Embedded Live Storyboard & Karaoke Preview Player -->
      <div id="re-live-preview-root" class="px-5 pt-4"></div>

      <!-- Scene Timeline List -->
      <div id="re-scenes-list" class="p-5 space-y-4 max-h-[72vh] overflow-y-auto studio-scroll">
        ${
          scenes.length === 0
            ? `
          <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-8 text-center text-sm text-slate-400">
            Dự án chưa có scene nào để chỉnh sửa. Bạn vẫn có thể bấm Confirm &amp; Render Video nếu pipeline đã sẵn sàng.
          </div>
        `
            : scenes.map((scene) => renderSceneCardHtml(scene)).join("")
        }
      </div>

      <div class="px-5 py-3.5 border-t border-[#262E42] bg-[#0B0D12]/50 flex items-center justify-between gap-3">
        <span class="text-xs text-slate-400">Mọi chỉnh sửa tự động lưu, hoặc bấm <strong class="text-slate-200">Lưu tất cả</strong> trước khi Render.</span>
        <button
          id="re-add-scene-bottom-btn"
          type="button"
          class="px-3.5 py-2 rounded-xl text-xs font-semibold bg-[#191F2E] hover:bg-[#20273A] text-amber-300 border border-[#262E42] transition shrink-0"
        >
          ➕ Thêm phân cảnh mới
        </button>
      </div>
    </div>
  `;

  function renderSceneCardHtml(scene) {
    const sid = escapeHtml(scene.id);
    const locked = Boolean(scene.is_locked);
    const currentText = isTranslator
      ? scene.edited_text ?? scene.original_text ?? ""
      : scene.narration_text ?? scene.original_text ?? "";
    const ratePct = Math.max(-25, Math.min(25, Number(scene.tts_rate_pct ?? 0)));
    const selectedVoice = scene.voice_id || pipelineConfig.voice_id || "";
    const speaker = (scene.speaker_label || "").trim();
    const keywordsOrPrompt = scene.search_keywords || scene.image_prompt || "";

    return `
      <article
        class="scene-card ${locked ? "scene-locked" : ""} rounded-2xl bg-[#121620] border border-[#262E42] p-4 transition-all"
        data-scene-card="${sid}"
      >
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
          <!-- Left / Main Timeline & Script Controls (8 cols) -->
          <div class="lg:col-span-8 space-y-3">
            <!-- Scene Meta Row -->
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="flex flex-wrap items-center gap-2">
                <span class="px-2.5 py-1 rounded-lg bg-[#191F2E] border border-[#262E42] font-mono text-xs font-bold text-white">
                  #${Number(scene.scene_index ?? 0) + 1}
                </span>
                ${
                  speaker
                    ? `<span class="px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-violet-500/15 text-violet-300 border border-violet-500/30">${escapeHtml(speaker)}</span>`
                    : ""
                }
                ${
                  scene.emotion_tone
                    ? `<span class="px-2 py-0.5 rounded-full text-[11px] bg-fuchsia-500/15 text-fuchsia-300 border border-fuchsia-500/30">${escapeHtml(scene.emotion_tone)}</span>`
                    : ""
                }
                <span data-scene-duration="${sid}" class="text-[11px] font-mono text-cyan-300 bg-cyan-500/10 border border-cyan-500/25 px-2 py-0.5 rounded">
                  ${formatDurationMs(scene.start_ms, scene.end_ms)}
                </span>
                <button
                  type="button"
                  data-action="play-from-scene"
                  data-scene-id="${sid}"
                  class="px-2 py-0.5 rounded-lg bg-violet-500/15 hover:bg-violet-500/25 border border-violet-500/35 text-[11px] font-semibold text-violet-200 transition"
                  title="Phát Live Preview từ cảnh này"
                >
                  ▶ Play từ cảnh này
                </button>
                <span data-scene-save-state="${sid}" class="text-[11px] text-slate-500"></span>
              </div>

              <div class="flex items-center gap-2 flex-wrap">
                <!-- Timing Inputs (Seconds) -->
                <div class="flex items-center gap-1.5 text-[11px] font-mono text-slate-400">
                  <span>Start</span>
                  <input
                    type="number"
                    min="0"
                    step="0.1"
                    value="${(Number(scene.start_ms || 0) / 1000).toFixed(1)}"
                    data-field="start_sec"
                    data-scene-id="${sid}"
                    ${locked ? "disabled" : ""}
                    class="w-16 px-2 py-1 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400 disabled:opacity-50"
                  />
                  <span>→</span>
                  <span>End</span>
                  <input
                    type="number"
                    min="0"
                    step="0.1"
                    value="${(Number(scene.end_ms || 0) / 1000).toFixed(1)}"
                    data-field="end_sec"
                    data-scene-id="${sid}"
                    ${locked ? "disabled" : ""}
                    class="w-16 px-2 py-1 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400 disabled:opacity-50"
                  />
                  <span>s</span>
                </div>

                <!-- Lock Toggle -->
                <button
                  type="button"
                  data-action="toggle-lock"
                  data-scene-id="${sid}"
                  class="px-2.5 py-1 rounded-lg text-xs font-semibold border transition ${
                    locked
                      ? "bg-cyan-500/20 text-cyan-200 border-cyan-500/40"
                      : "bg-[#191F2E] text-slate-300 border-[#262E42] hover:text-white"
                  }"
                  title="${locked ? "Mở khóa scene để chỉnh sửa" : "Khóa scene tránh sửa nhầm"}"
                >
                  ${locked ? "🔒 Đã khóa" : "🔓 Khóa"}
                </button>

                <!-- Delete Scene -->
                <button
                  type="button"
                  data-action="delete-scene"
                  data-scene-id="${sid}"
                  ${locked || (sceneMap.size > 0 ? sceneMap.size : scenes.length) <= 1 ? "disabled" : ""}
                  class="px-2.5 py-1 rounded-lg text-xs font-semibold border border-rose-500/30 bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 transition disabled:opacity-40 disabled:cursor-not-allowed"
                  title="${
                    (sceneMap.size > 0 ? sceneMap.size : scenes.length) <= 1
                      ? "Không thể xóa phân cảnh cuối cùng"
                      : locked
                      ? "Mở khóa scene trước khi xóa"
                      : "Xóa phân cảnh này"
                  }"
                >
                  🗑️ Xóa
                </button>
              </div>
            </div>

            <!-- Original Text Reference -->
            ${
              scene.original_text
                ? `
              <div class="rounded-xl bg-[#0B0D12]/80 border border-[#262E42]/80 px-3 py-2 text-xs">
                <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mr-1.5">Gốc:</span>
                <span class="text-slate-400">${escapeHtml(scene.original_text)}</span>
              </div>
            `
                : ""
            }

            <!-- Editable Script / Subtitle Textarea -->
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">
                ${isTranslator ? "Nội dung dịch / Lồng tiếng (edited_text)" : "Lời thoại Narration (narration_text)"}
              </label>
              <textarea
                rows="2"
                data-field="${textField}"
                data-scene-id="${sid}"
                ${locked ? "disabled" : ""}
                class="w-full px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-violet-400 disabled:opacity-55 leading-relaxed"
              >${escapeHtml(currentText)}</textarea>
            </div>

            ${
              !isTranslator
                ? `
              <div class="flex items-center gap-2">
                <label class="text-[11px] text-slate-400 shrink-0">🔍 Từ khóa Visual:</label>
                <input
                  type="text"
                  value="${escapeHtml(scene.search_keywords || "")}"
                  data-field="search_keywords"
                  data-scene-id="${sid}"
                  ${locked ? "disabled" : ""}
                  placeholder="vd: dark futuristic city neon rain"
                  class="flex-1 min-w-0 px-2.5 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs font-mono text-slate-200 placeholder-slate-600 focus:outline-none focus:border-cyan-400 disabled:opacity-50"
                />
              </div>
            `
                : keywordsOrPrompt
                ? `
              <div class="text-[11px] text-slate-400 flex items-center gap-1.5 truncate">
                <span class="text-slate-500 shrink-0">🔍 Keywords / Prompt:</span>
                <span class="font-mono text-slate-300 truncate" title="${escapeHtml(keywordsOrPrompt)}">${escapeHtml(keywordsOrPrompt)}</span>
              </div>
            `
                : ""
            }

            <!-- Voice, Speed Slider & Nghe thử -->
            <div class="grid grid-cols-1 sm:grid-cols-12 gap-3 pt-1 items-center">
              <div class="sm:col-span-5">
                <label class="block text-[10px] uppercase tracking-wider text-slate-400 mb-1">Giọng TTS</label>
                <select
                  data-field="voice_id"
                  data-scene-id="${sid}"
                  ${locked ? "disabled" : ""}
                  class="re-voice-select w-full px-2.5 py-1.5 rounded-lg bg-[#0B0D12] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400 disabled:opacity-50"
                >
                  ${buildVoiceOptionsHtml(cachedVoices, selectedVoice)}
                </select>
              </div>

              <div class="sm:col-span-4">
                <div class="flex items-center justify-between text-[10px] uppercase tracking-wider text-slate-400 mb-1">
                  <span>Tốc độ đọc</span>
                  <span data-rate-display="${sid}" class="font-mono text-cyan-300">${ratePct >= 0 ? `+${ratePct}` : ratePct}%</span>
                </div>
                <input
                  type="range"
                  min="-25"
                  max="25"
                  step="5"
                  value="${ratePct}"
                  data-field="tts_rate_pct"
                  data-scene-id="${sid}"
                  ${locked ? "disabled" : ""}
                  class="studio-range w-full disabled:opacity-50"
                />
              </div>

              <div class="sm:col-span-3 flex sm:justify-end">
                <button
                  type="button"
                  data-action="preview-tts"
                  data-scene-id="${sid}"
                  class="w-full sm:w-auto px-3 py-2 rounded-xl bg-violet-500/15 hover:bg-violet-500/25 text-violet-200 border border-violet-500/35 text-xs font-semibold transition flex items-center justify-center gap-1.5"
                >
                  <span>🔊</span>
                  <span>Nghe thử</span>
                </button>
              </div>
            </div>

            <!-- Inline Audio Preview Player -->
            <div data-audio-slot="${sid}" class="hidden pt-1"></div>
          </div>

          <!-- Right Visual Preview & Swap/Upload Controls (4 cols) -->
          <div class="lg:col-span-4 flex flex-col justify-between gap-3 border-t lg:border-t-0 lg:border-l border-[#262E42] pt-3 lg:pt-0 lg:pl-4">
            <div data-visual-preview="${sid}">
              ${renderVisualPreviewHtml(scene)}
            </div>

            <div class="space-y-2 relative">
              <div class="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  data-action="toggle-swap-popover"
                  data-scene-id="${sid}"
                  ${locked ? "disabled" : ""}
                  class="px-2.5 py-2 rounded-xl bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-[11px] font-semibold text-cyan-300 transition disabled:opacity-50"
                >
                  🔄 Đổi video nền khác
                </button>

                <label
                  class="px-2.5 py-2 rounded-xl bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-[11px] font-semibold text-slate-200 transition text-center cursor-pointer ${
                    locked ? "opacity-50 pointer-events-none" : ""
                  }"
                >
                  <span>📤 Tải clip/ảnh riêng</span>
                  <input
                    type="file"
                    accept=".mp4,.mov,.mkv,.webm,.jpg,.jpeg,.png,.webp"
                    data-action="upload-visual-input"
                    data-scene-id="${sid}"
                    ${locked ? "disabled" : ""}
                    class="hidden"
                  />
                </label>
              </div>

              <!-- Swap Visual Popover -->
              <div
                data-swap-popover="${sid}"
                class="hidden rounded-xl bg-[#0B0D12] border border-cyan-500/40 p-3 space-y-2.5 shadow-xl"
              >
                <div class="flex items-center justify-between">
                  <span class="text-[11px] font-bold text-cyan-300">Tìm / Tạo Visual mới</span>
                  <button
                    type="button"
                    data-action="close-swap-popover"
                    data-scene-id="${sid}"
                    class="text-xs text-slate-400 hover:text-white"
                  >
                    ✕
                  </button>
                </div>
                <input
                  type="text"
                  data-swap-query="${sid}"
                  value="${escapeHtml(scene.search_keywords || scene.image_prompt || "")}"
                  placeholder="Từ khóa (VD: futuristic city night) hoặc AI prompt..."
                  class="w-full px-2.5 py-1.5 rounded-lg bg-[#121620] border border-[#262E42] text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-400"
                />
                <div class="grid grid-cols-2 gap-2">
                  <select
                    data-swap-provider="${sid}"
                    class="px-2 py-1.5 rounded-lg bg-[#121620] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400"
                  >
                    ${VISUAL_PROVIDERS.map(
                      (p) =>
                        `<option value="${p.value}" ${
                          p.value === defaultVisualProvider ? "selected" : ""
                        }>${escapeHtml(p.label)}</option>`
                    ).join("")}
                  </select>
                  <select
                    data-swap-media-type="${sid}"
                    class="px-2 py-1.5 rounded-lg bg-[#121620] border border-[#262E42] text-xs text-white focus:outline-none focus:border-cyan-400"
                  >
                    <option value="video" ${isTranslator ? "" : "selected"}>Video</option>
                    <option value="image">Image</option>
                  </select>
                </div>
                <button
                  type="button"
                  data-action="submit-swap-visual"
                  data-scene-id="${sid}"
                  class="w-full py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 text-xs font-bold transition"
                >
                  Tìm &amp; Thay thế Visual
                </button>
              </div>
            </div>
          </div>
        </div>
      </article>
    `;
  }

  const setSceneSaveBadge = (sceneId, text, tone = "muted") => {
    const el = container.querySelector(`[data-scene-save-state="${sceneId}"]`);
    if (!el) return;
    el.textContent = text;
    el.className = `text-[11px] ${
      tone === "saving"
        ? "text-cyan-400"
        : tone === "saved"
        ? "text-emerald-400"
        : tone === "error"
        ? "text-rose-400"
        : "text-slate-500"
    }`;
  };

  const persistScenePatch = async (sceneId, patch) => {
    const key = String(sceneId);
    const existingTimer = debounceTimers.get(key);
    if (existingTimer) {
      clearTimeout(existingTimer);
      debounceTimers.delete(key);
    }

    const mergedPatch = { ...(dirtyScenes.get(key) || {}), ...patch };
    dirtyScenes.delete(key);

    const scene = sceneMap.get(key);
    if (!scene) return null;

    setSceneSaveBadge(key, "Đang lưu...", "saving");
    try {
      const updated = await api.updateScene(projectDetail.id, key, mergedPatch);
      sceneMap.set(key, updated);
      setSceneSaveBadge(key, "Đã lưu", "saved");

      const durEl = container.querySelector(`[data-scene-duration="${key}"]`);
      if (durEl) {
        durEl.textContent = formatDurationMs(updated.start_ms, updated.end_ms);
      }
      return updated;
    } catch (err) {
      setSceneSaveBadge(key, "Lỗi lưu", "error");
      showToast(err?.message || "Không thể lưu thay đổi scene", "error");
      return null;
    }
  };

  const scheduleScenePatch = (sceneId, patch, delayMs = 550) => {
    const key = String(sceneId);
    const prev = dirtyScenes.get(key) || {};
    dirtyScenes.set(key, { ...prev, ...patch });

    const existing = debounceTimers.get(key);
    if (existing) clearTimeout(existing);

    setSceneSaveBadge(key, "Chưa lưu...", "muted");
    const timer = setTimeout(() => {
      debounceTimers.delete(key);
      persistScenePatch(key, {});
    }, delayMs);
    debounceTimers.set(key, timer);
  };

  const flushPendingSaves = async () => {
    const pendingIds = new Set([...debounceTimers.keys(), ...dirtyScenes.keys()]);
    for (const id of pendingIds) {
      await persistScenePatch(id, {});
    }
  };

  const syncAllVoiceSelects = () => {
    const voiceLoadEl = container.querySelector("#re-voice-load-status");
    if (voiceLoadEl) {
      voiceLoadEl.textContent = cachedVoices.length
        ? `${cachedVoices.length} giọng (${ttsProvider})`
        : `Không có giọng cho ${ttsProvider}`;
    }

    container.querySelectorAll("select.re-voice-select").forEach((sel) => {
      const sceneId = sel.getAttribute("data-scene-id");
      const bulkKey = sel.getAttribute("data-bulk-speaker-select");
      let currentVal = sel.value;
      if (sceneId && sceneMap.has(sceneId)) {
        currentVal = sceneMap.get(sceneId).voice_id || currentVal;
      } else if (bulkKey) {
        currentVal =
          bulkKey === "__all__"
            ? pipelineConfig.voice_id || currentVal
            : speakerVoices[bulkKey] || currentVal;
      }
      sel.innerHTML = buildVoiceOptionsHtml(cachedVoices, currentVal);
      if (currentVal) sel.value = currentVal;
    });
  };

  // Load TTS voices if not cached yet
  if (!cachedVoices.length) {
    api
      .getTtsVoices(ttsProvider)
      .then((res) => {
        const list = Array.isArray(res?.voices) ? res.voices : [];
        cachedVoices = list;
        const prevMap = store.getState().ttsVoices || {};
        store.setState({ ttsVoices: { ...prevMap, [ttsProvider]: list } });
        if (container.isConnected) {
          syncAllVoiceSelects();
        }
      })
      .catch(() => {
        const voiceLoadEl = container.querySelector("#re-voice-load-status");
        if (voiceLoadEl) voiceLoadEl.textContent = "Dùng giọng mặc định";
      });
  }

  const scenesListEl = container.querySelector("#re-scenes-list");
  const sceneCountBadgeEl = container.querySelector("#re-scene-count-badge");
  const livePreviewRoot = container.querySelector("#re-live-preview-root");
  const burnToggleEl = container.querySelector("#re-burn-subtitles");
  const saveAllBtn = container.querySelector("#re-save-all-btn");
  const addSceneBtn = container.querySelector("#re-add-scene-btn");
  const addSceneBottomBtn = container.querySelector("#re-add-scene-bottom-btn");
  const exportSrtBtn = container.querySelector("#re-export-srt-btn");
  const exportAssBtn = container.querySelector("#re-export-ass-btn");
  const confirmRenderBtn = container.querySelector("#re-confirm-render-btn");
  const confirmLabelEl = container.querySelector("#re-confirm-label");
  const closeBtn = container.querySelector("#re-close-btn");
  const bulkContainer = container.querySelector("#re-bulk-speakers");

  function renderScenesList() {
    const ordered = Array.from(sceneMap.values()).sort(
      (a, b) => Number(a.scene_index ?? 0) - Number(b.scene_index ?? 0)
    );
    if (sceneCountBadgeEl) {
      sceneCountBadgeEl.textContent = String(ordered.length);
    }
    if (scenesListEl) {
      scenesListEl.innerHTML =
        ordered.length === 0
          ? `
          <div class="rounded-xl bg-[#0B0D12] border border-[#262E42] p-8 text-center text-sm text-slate-400">
            Dự án chưa có scene nào để chỉnh sửa. Bạn vẫn có thể bấm Confirm &amp; Render Video nếu pipeline đã sẵn sàng.
          </div>
        `
          : ordered.map((scene) => renderSceneCardHtml(scene)).join("");
    }
    refreshLivePreview();
  }

  async function handleSaveAll() {
    if (!saveAllBtn) return;
    saveAllBtn.disabled = true;
    const origLabel = saveAllBtn.textContent;
    saveAllBtn.textContent = "Đang lưu...";
    try {
      await flushPendingSaves();
      showToast("Đã lưu toàn bộ thay đổi", "success");
    } catch (err) {
      showToast(err?.message || "Không thể lưu toàn bộ thay đổi", "error");
    } finally {
      saveAllBtn.disabled = false;
      saveAllBtn.textContent = origLabel;
    }
  }

  async function handleAddScene(triggerBtn) {
    const buttons = [addSceneBtn, addSceneBottomBtn].filter(Boolean);
    buttons.forEach((b) => {
      b.disabled = true;
    });
    const origLabel = triggerBtn?.textContent;
    if (triggerBtn) triggerBtn.textContent = "Đang thêm...";

    try {
      await flushPendingSaves();
      const ordered = Array.from(sceneMap.values()).sort(
        (a, b) => Number(a.scene_index ?? 0) - Number(b.scene_index ?? 0)
      );
      const maxEndMs = ordered.reduce((acc, s) => Math.max(acc, Number(s.end_ms || 0)), 0);
      const startMs = maxEndMs;
      const endMs = startMs + 3000;

      const payload = isTranslator
        ? {
            start_ms: startMs,
            end_ms: endMs,
            edited_text: "Nội dung phân cảnh mới...",
            original_text: "",
            voice_id: pipelineConfig.voice_id || undefined,
          }
        : {
            start_ms: startMs,
            end_ms: endMs,
            narration_text: "Nội dung lời bình phân cảnh mới...",
            search_keywords: "cinematic background",
            voice_id: pipelineConfig.voice_id || undefined,
          };

      const created = await api.createScene(projectDetail.id, payload);
      const createdId = String(created.id);
      sceneMap.set(createdId, created);
      renderScenesList();

      const newCard = scenesListEl?.querySelector(`[data-scene-card="${createdId}"]`);
      newCard?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      showToast(`Đã thêm Scene #${Number(created.scene_index ?? ordered.length) + 1}`, "success");
    } catch (err) {
      showToast(err?.message || "Không thể thêm phân cảnh mới", "error");
    } finally {
      buttons.forEach((b) => {
        b.disabled = false;
      });
      if (triggerBtn && origLabel) triggerBtn.textContent = origLabel;
    }
  }

  saveAllBtn?.addEventListener("click", handleSaveAll);
  addSceneBtn?.addEventListener("click", () => handleAddScene(addSceneBtn));
  addSceneBottomBtn?.addEventListener("click", () => handleAddScene(addSceneBottomBtn));

  if (livePreviewRoot) {
    livePlayer = renderLivePreviewPlayer(livePreviewRoot, buildLiveDetailSnapshot());
  }

  const handleSubtitleExport = async (btn, format) => {
    if (!btn) return;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = "Đang xuất...";
    try {
      await flushPendingSaves();
      const { blob, filename } = await api.exportSubtitles(projectDetail.id, format);
      triggerBlobDownload(blob, filename);
      showToast(`Đã tải xuống ${filename}`, "success");
    } catch (err) {
      showToast(err?.message || `Không thể xuất phụ đề .${format.toUpperCase()}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  };

  exportSrtBtn?.addEventListener("click", () => handleSubtitleExport(exportSrtBtn, "srt"));
  exportAssBtn?.addEventListener("click", () => handleSubtitleExport(exportAssBtn, "ass"));

  burnToggleEl?.addEventListener("change", (e) => {
    burnSubtitles = Boolean(e.target.checked);
    refreshLivePreview();
  });

  // Bulk speaker voice assignment
  bulkContainer?.addEventListener("click", async (e) => {
    const applyBtn = e.target.closest("[data-bulk-speaker-apply]");
    if (!applyBtn) return;

    const speakerKey = applyBtn.getAttribute("data-bulk-speaker-apply");
    const select = container.querySelector(`[data-bulk-speaker-select="${CSS.escape(speakerKey)}"]`);
    const chosenVoice = select?.value || "";
    if (!chosenVoice) {
      showToast("Vui lòng chọn một giọng đọc trước khi áp dụng", "warning");
      return;
    }

    if (speakerKey !== "__all__") {
      speakerVoices[speakerKey] = chosenVoice;
    } else {
      pipelineConfig.voice_id = chosenVoice;
    }

    const targets = Array.from(sceneMap.values()).filter((sc) => {
      if (sc.is_locked) return false;
      if (speakerKey === "__all__") return true;
      return (sc.speaker_label || "").trim() === speakerKey;
    });

    if (targets.length === 0) {
      showToast("Không có scene chưa khóa nào phù hợp để áp dụng", "info");
      return;
    }

    applyBtn.disabled = true;
    applyBtn.textContent = "Đang áp dụng...";

    let successCount = 0;
    for (const sc of targets) {
      const sid = String(sc.id);
      const updated = await persistScenePatch(sid, { voice_id: chosenVoice });
      if (updated) {
        successCount += 1;
        const sceneSelect = container.querySelector(`select[data-field="voice_id"][data-scene-id="${sid}"]`);
        if (sceneSelect) {
          sceneSelect.innerHTML = buildVoiceOptionsHtml(cachedVoices, chosenVoice);
          sceneSelect.value = chosenVoice;
        }
      }
    }

    applyBtn.disabled = false;
    applyBtn.textContent = speakerKey === "__all__" ? "Áp dụng tất cả" : "Áp dụng";
    showToast(`Đã gán giọng cho ${successCount}/${targets.length} scene`, "success");
  });

  // Input / change handlers on scenes list
  scenesListEl?.addEventListener("input", (e) => {
    const target = e.target;
    const sid = target.getAttribute("data-scene-id");
    const field = target.getAttribute("data-field");
    if (!sid || !field) return;

    const scene = sceneMap.get(sid);
    if (!scene || scene.is_locked) return;

    if (field === textField) {
      scene[textField] = target.value;
      scheduleScenePatch(sid, { [textField]: target.value });
      refreshLivePreview();
      return;
    }

    if (field === "search_keywords") {
      scene.search_keywords = target.value;
      scheduleScenePatch(sid, { search_keywords: target.value });
      return;
    }

    if (field === "start_sec" || field === "end_sec") {
      const startInput = container.querySelector(`input[data-field="start_sec"][data-scene-id="${sid}"]`);
      const endInput = container.querySelector(`input[data-field="end_sec"][data-scene-id="${sid}"]`);
      const parsedStart = Number.parseFloat(startInput?.value ?? "");
      const parsedEnd = Number.parseFloat(endInput?.value ?? "");
      if (!Number.isFinite(parsedStart) || !Number.isFinite(parsedEnd) || parsedStart < 0 || parsedEnd < 0) {
        return;
      }
      const startMs = Math.round(parsedStart * 1000);
      const endMs = Math.round(parsedEnd * 1000);
      if (endMs < startMs) return;

      scene.start_ms = startMs;
      scene.end_ms = endMs;
      refreshLivePreview();
      scheduleScenePatch(sid, { start_ms: startMs, end_ms: endMs }, 400);
      return;
    }

    if (field === "tts_rate_pct") {
      const rate = Number.parseInt(target.value, 10) || 0;
      scene.tts_rate_pct = rate;
      const badge = container.querySelector(`[data-rate-display="${sid}"]`);
      if (badge) badge.textContent = `${rate >= 0 ? `+${rate}` : rate}%`;
      scheduleScenePatch(sid, { tts_rate_pct: rate }, 350);
      refreshLivePreview();
    }
  });

  scenesListEl?.addEventListener("change", async (e) => {
    const target = e.target;
    const sid = target.getAttribute("data-scene-id");
    const field = target.getAttribute("data-field");

    // Upload visual file input
    if (target.getAttribute("data-action") === "upload-visual-input" && sid) {
      const file = target.files?.[0];
      if (!file) return;
      setSceneSaveBadge(sid, "Đang upload visual...", "saving");
      try {
        const updated = await api.uploadSceneVisual(projectDetail.id, sid, file);
        sceneMap.set(sid, updated);
        const previewEl = container.querySelector(`[data-visual-preview="${sid}"]`);
        if (previewEl) previewEl.innerHTML = renderVisualPreviewHtml(updated);
        refreshLivePreview();
        setSceneSaveBadge(sid, "Đã cập nhật visual", "saved");
        showToast(`Đã tải lên visual mới cho Scene #${updated.scene_index + 1}`, "success");
      } catch (err) {
        setSceneSaveBadge(sid, "Upload lỗi", "error");
        showToast(err?.message || "Upload visual thất bại", "error");
      } finally {
        target.value = "";
      }
      return;
    }

    if (!sid || !field) return;
    const scene = sceneMap.get(sid);
    if (!scene || scene.is_locked) return;

    if (field === "voice_id") {
      const voiceId = target.value || null;
      scene.voice_id = voiceId;
      await persistScenePatch(sid, { voice_id: voiceId });
      return;
    }

    if (field === "tts_rate_pct") {
      const rate = Number.parseInt(target.value, 10) || 0;
      scene.tts_rate_pct = rate;
      await persistScenePatch(sid, { tts_rate_pct: rate });
      return;
    }

    if (field === "start_sec" || field === "end_sec") {
      const startInput = container.querySelector(`input[data-field="start_sec"][data-scene-id="${sid}"]`);
      const endInput = container.querySelector(`input[data-field="end_sec"][data-scene-id="${sid}"]`);
      const parsedStart = Number.parseFloat(startInput?.value ?? "");
      const parsedEnd = Number.parseFloat(endInput?.value ?? "");
      const startMs = Number.isFinite(parsedStart) ? Math.round(parsedStart * 1000) : -1;
      const endMs = Number.isFinite(parsedEnd) ? Math.round(parsedEnd * 1000) : -1;

      if (startMs < 0 || endMs < 0 || endMs < startMs) {
        showToast("Thời điểm kết thúc phải lớn hơn hoặc bằng thời điểm bắt đầu (>= 0s)", "warning");
        if (startInput) startInput.value = (Number(scene.start_ms || 0) / 1000).toFixed(1);
        if (endInput) endInput.value = (Number(scene.end_ms || 0) / 1000).toFixed(1);
        return;
      }

      scene.start_ms = startMs;
      scene.end_ms = endMs;
      refreshLivePreview();
      await persistScenePatch(sid, { start_ms: startMs, end_ms: endMs });
    }
  });

  // Flush pending field edits immediately on blur
  scenesListEl?.addEventListener(
    "blur",
    (e) => {
      const target = e.target;
      const sid = target.getAttribute?.("data-scene-id");
      const field = target.getAttribute?.("data-field");
      if (
        sid &&
        (field === textField || field === "search_keywords" || field === "start_sec" || field === "end_sec") &&
        dirtyScenes.has(sid)
      ) {
        persistScenePatch(sid, {});
      }
    },
    true
  );

  // Click actions on scenes list
  scenesListEl?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-action");
    const sid = btn.getAttribute("data-scene-id");
    if (!sid) return;

    const scene = sceneMap.get(sid);
    if (!scene) return;

    if (action === "play-from-scene") {
      livePlayer?.seekToMs(Number(scene.start_ms || 0));
      livePreviewRoot?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      return;
    }

    if (action === "delete-scene") {
      if (scene.is_locked) {
        showToast("Phân cảnh đang bị khóa, hãy mở khóa trước khi xóa", "warning");
        return;
      }
      if (sceneMap.size <= 1) {
        showToast("Dự án cần giữ lại ít nhất 1 phân cảnh", "warning");
        return;
      }

      const confirmed = await confirmDialog({
        title: `Xóa Scene #${Number(scene.scene_index ?? 0) + 1}?`,
        message: "Thao tác này sẽ xóa phân cảnh khỏi timeline và không thể hoàn tác.",
        confirmText: "Xóa phân cảnh",
        cancelText: "Hủy",
        danger: true,
      });
      if (!confirmed) return;

      btn.disabled = true;
      const origText = btn.textContent;
      btn.textContent = "Đang xóa...";
      try {
        await api.deleteScene(projectDetail.id, sid);
        const timer = debounceTimers.get(sid);
        if (timer) {
          clearTimeout(timer);
          debounceTimers.delete(sid);
        }
        dirtyScenes.delete(sid);
        const audioUrl = activeAudioUrls.get(sid);
        if (audioUrl) {
          URL.revokeObjectURL(audioUrl);
          activeAudioUrls.delete(sid);
        }
        sceneMap.delete(sid);
        renderScenesList();
        showToast(`Đã xóa Scene #${Number(scene.scene_index ?? 0) + 1}`, "success");
      } catch (err) {
        showToast(err?.message || "Không thể xóa phân cảnh", "error");
        btn.disabled = false;
        btn.textContent = origText;
      }
      return;
    }

    if (action === "toggle-lock") {
      const nextLocked = !scene.is_locked;
      btn.disabled = true;
      const updated = await persistScenePatch(sid, { is_locked: nextLocked });
      btn.disabled = false;
      if (updated) {
        const card = container.querySelector(`[data-scene-card="${sid}"]`);
        if (card) {
          card.outerHTML = renderSceneCardHtml(updated);
        }
      }
      return;
    }

    if (action === "preview-tts") {
      const textarea = container.querySelector(`textarea[data-field="${textField}"][data-scene-id="${sid}"]`);
      const voiceSel = container.querySelector(`select[data-field="voice_id"][data-scene-id="${sid}"]`);
      const rateInput = container.querySelector(`input[data-field="tts_rate_pct"][data-scene-id="${sid}"]`);
      const text = (textarea?.value || scene[textField] || scene.original_text || "").trim();

      if (!text) {
        showToast("Scene chưa có nội dung thoại để nghe thử", "warning");
        return;
      }

      btn.disabled = true;
      const origHtml = btn.innerHTML;
      btn.innerHTML = `<span>⏳</span><span>Đang tạo...</span>`;

      try {
        const res = await api.previewSceneTts(projectDetail.id, sid, {
          text,
          voice_id: voiceSel?.value || scene.voice_id || pipelineConfig.voice_id || undefined,
          tts_provider: ttsProvider,
          tts_rate_pct: Number.parseInt(rateInput?.value ?? scene.tts_rate_pct ?? 0, 10) || 0,
        });

        const prevUrl = activeAudioUrls.get(sid);
        if (prevUrl) {
          URL.revokeObjectURL(prevUrl);
        }
        const blobUrl = URL.createObjectURL(res.blob);
        activeAudioUrls.set(sid, blobUrl);

        const audioSlot = container.querySelector(`[data-audio-slot="${sid}"]`);
        if (audioSlot) {
          audioSlot.classList.remove("hidden");
          audioSlot.innerHTML = `
            <div class="flex flex-col sm:flex-row sm:items-center gap-2 rounded-xl bg-[#0B0D12] border border-violet-500/35 px-3 py-2">
              <audio controls autoplay src="${blobUrl}" class="h-8 flex-1 min-w-0"></audio>
              <span class="text-[11px] font-mono text-slate-400 shrink-0">
                ${escapeHtml(res.provider || ttsProvider)} · ${(Number(res.durationMs || 0) / 1000).toFixed(1)}s
              </span>
            </div>
          `;
        }
      } catch (err) {
        showToast(err?.message || "Không thể tạo TTS nghe thử", "error");
      } finally {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
      return;
    }

    if (action === "toggle-swap-popover") {
      const pop = container.querySelector(`[data-swap-popover="${sid}"]`);
      pop?.classList.toggle("hidden");
      return;
    }

    if (action === "close-swap-popover") {
      const pop = container.querySelector(`[data-swap-popover="${sid}"]`);
      pop?.classList.add("hidden");
      return;
    }

    if (action === "submit-swap-visual") {
      const queryInput = container.querySelector(`[data-swap-query="${sid}"]`);
      const providerSel = container.querySelector(`[data-swap-provider="${sid}"]`);
      const mediaTypeSel = container.querySelector(`[data-swap-media-type="${sid}"]`);

      const queryText = (queryInput?.value || "").trim();
      const visualProvider = providerSel?.value || "pexels";
      const mediaType = mediaTypeSel?.value || "video";

      if (!queryText) {
        showToast("Vui lòng nhập từ khóa hoặc prompt để tìm visual", "warning");
        return;
      }

      btn.disabled = true;
      const origText = btn.textContent;
      btn.textContent = "Đang tìm & tải visual...";
      setSceneSaveBadge(sid, "Đang đổi visual...", "saving");

      try {
        const updated = await api.swapSceneVisual(projectDetail.id, sid, {
          query: queryText,
          prompt: queryText,
          visual_provider: visualProvider,
          media_type: mediaType,
        });
        sceneMap.set(sid, updated);
        const previewEl = container.querySelector(`[data-visual-preview="${sid}"]`);
        if (previewEl) previewEl.innerHTML = renderVisualPreviewHtml(updated);
        refreshLivePreview();
        const pop = container.querySelector(`[data-swap-popover="${sid}"]`);
        pop?.classList.add("hidden");
        setSceneSaveBadge(sid, "Đã đổi visual", "saved");
        showToast(`Đã đổi visual Scene #${updated.scene_index + 1}`, "success");
      } catch (err) {
        setSceneSaveBadge(sid, "Đổi visual lỗi", "error");
        showToast(err?.message || "Không thể đổi visual nền", "error");
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }
  });

  // Confirm & Render Video
  confirmRenderBtn?.addEventListener("click", async () => {
    confirmRenderBtn.disabled = true;
    confirmRenderBtn.classList.add("opacity-70");
    if (confirmLabelEl) confirmLabelEl.textContent = "Đang lưu & Khởi chạy Render...";

    try {
      await flushPendingSaves();
      const payload = {
        burn_subtitles: burnSubtitles,
        voice_id: pipelineConfig.voice_id || undefined,
        speaker_voices: Object.keys(speakerVoices).length > 0 ? speakerVoices : undefined,
      };
      const updated = await api.renderProject(projectDetail.id, payload);
      upsertProjectInStore(updated);
      store.setState({ activeProject: updated });
      showToast(`Đã xác nhận Review! Bắt đầu Render "${updated.title}"`, "success");

      cleanupResources();
      container.innerHTML = "";
      container.classList.add("hidden");
      callbacks.onRenderStarted?.(updated);
    } catch (err) {
      showToast(err?.message || "Không thể bắt đầu Render Video", "error");
      confirmRenderBtn.disabled = false;
      confirmRenderBtn.classList.remove("opacity-70");
      if (confirmLabelEl) confirmLabelEl.textContent = "Confirm & Render Video";
    }
  });

  closeBtn?.addEventListener("click", () => {
    cleanupResources();
    container.innerHTML = "";
    container.classList.add("hidden");
    callbacks.onClose?.();
  });

  return {
    destroy: cleanupResources,
  };
}
