/**
 * Stage 5 Interactive Component: Live Storyboard & Karaoke Preview Player
 * Renders a synchronized 9:16 / 16:9 / 1:1 stage showing the active scene's
 * visual (video or Ken Burns image), live word-by-word Karaoke subtitles,
 * Play/Pause, timeline scrubber with clickable scene markers, and optional
 * scene-synchronized TTS audio playback.
 */

import { api } from "../core/api.js";
import { showToast, store } from "../core/state.js";

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

function formatMs(ms) {
  const safe = Math.max(0, Number(ms || 0));
  const totalSec = safe / 1000;
  const mins = Math.floor(totalSec / 60);
  const secs = (totalSec % 60).toFixed(1).padStart(4, "0");
  return `${String(mins).padStart(2, "0")}:${secs}`;
}

function isBrowserPlayableUrl(url) {
  if (!url) return false;
  const s = String(url).trim();
  return (
    s.startsWith("http://") ||
    s.startsWith("https://") ||
    s.startsWith("/") ||
    s.startsWith("blob:") ||
    s.startsWith("data:")
  );
}

function detectMediaKind(url, visualSource) {
  const lower = String(url || "").toLowerCase().split("?")[0];
  if (VIDEO_EXTENSIONS.some((ext) => lower.endsWith(ext))) return "video";
  if (IMAGE_EXTENSIONS.some((ext) => lower.endsWith(ext))) return "image";
  if (visualSource === "ai_gen") return "image";
  return "video";
}

function getAspectStageClass(aspectRatio) {
  if (aspectRatio === "16:9") {
    return "w-full max-w-[460px] aspect-video";
  }
  if (aspectRatio === "1:1") {
    return "w-full max-w-[300px] aspect-square";
  }
  return "w-full max-w-[240px] aspect-[9/16]";
}

function normalizeScenes(projectDetail) {
  return [...(projectDetail?.scenes || [])].sort(
    (a, b) => Number(a.scene_index ?? 0) - Number(b.scene_index ?? 0)
  );
}

function computeTotalDurationMs(scenes) {
  if (!scenes.length) return 5000;
  const maxEnd = scenes.reduce((acc, sc) => Math.max(acc, Number(sc.end_ms || 0)), 0);
  return Math.max(1000, maxEnd);
}

function getSceneText(scene, mode) {
  if (!scene) return "";
  if (mode === "translator") {
    return String(scene.edited_text ?? scene.original_text ?? "").trim();
  }
  return String(scene.narration_text ?? scene.original_text ?? "").trim();
}

function buildKaraokeTokens(scene, text) {
  const start = Number(scene?.start_ms || 0);
  const end = Math.max(start + 200, Number(scene?.end_ms || start + 1000));
  const rawWords = text ? text.split(/\s+/).filter(Boolean) : [];
  const wordTimestamps = Array.isArray(scene?.word_timestamps) ? scene.word_timestamps : [];

  // Use aligned word_timestamps if word count matches current edited text closely
  if (
    wordTimestamps.length > 0 &&
    rawWords.length > 0 &&
    Math.abs(wordTimestamps.length - rawWords.length) <= 1
  ) {
    return wordTimestamps.map((wt, idx) => ({
      word: rawWords[idx] ?? wt.word ?? "",
      start_ms: Number(wt.start_ms ?? start),
      end_ms: Number(wt.end_ms ?? end),
    }));
  }

  if (rawWords.length === 0) return [];
  const span = Math.max(200, end - start);
  const step = span / rawWords.length;
  return rawWords.map((word, idx) => ({
    word,
    start_ms: start + idx * step,
    end_ms: start + (idx + 1) * step,
  }));
}

/**
 * Render an interactive Live Preview Player into `container`.
 * @param {HTMLElement} container
 * @param {object} initialProjectDetail
 * @returns {{ updateData: (nextDetail: object) => void, seekToMs: (ms: number) => void, destroy: () => void }}
 */
export function renderLivePreviewPlayer(container, initialProjectDetail) {
  if (!container || !initialProjectDetail) {
    return { updateData() {}, seekToMs() {}, destroy() {} };
  }

  let projectDetail = initialProjectDetail;
  let scenes = normalizeScenes(projectDetail);
  let totalDurationMs = computeTotalDurationMs(scenes);
  let playheadMs = scenes[0]?.start_ms ? Number(scenes[0].start_ms) : 0;
  let isPlaying = false;
  let ttsTimelineEnabled = false;
  let rafId = null;
  let lastFrameTs = 0;
  let renderedVisualSceneId = null;
  let renderedVisualUrl = null;
  let lastTtsSceneKey = null;
  let isDestroyed = false;

  const ttsAudio = new Audio();
  const ttsBlobCache = new Map(); // cacheKey -> objectUrl
  let ttsFetchSeq = 0;

  container.innerHTML = `
    <div class="rounded-2xl bg-[#0B0D12] border border-[#262E42] p-4 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full bg-violet-400"></span>
          <h4 class="text-xs font-bold uppercase tracking-wider text-slate-200">
            Live Storyboard &amp; Karaoke Preview
          </h4>
          <span id="lpp-active-scene-pill" class="px-2 py-0.5 rounded-full text-[10px] font-mono bg-[#191F2E] text-cyan-300 border border-[#262E42]">
            Scene #1
          </span>
        </div>

        <label class="inline-flex items-center gap-2 px-2.5 py-1 rounded-lg bg-[#121620] border border-[#262E42] text-[11px] text-slate-300 cursor-pointer select-none">
          <input id="lpp-tts-toggle" type="checkbox" class="rounded border-[#262E42] bg-[#0B0D12] text-cyan-500" />
          <span>🔊 Nghe TTS theo timeline</span>
        </label>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-12 gap-4 items-center">
        <!-- Stage -->
        <div class="lg:col-span-5 flex justify-center">
          <div
            id="lpp-stage"
            class="live-preview-stage karaoke-preview-stage relative ${getAspectStageClass(
              projectDetail.aspect_ratio
            )} rounded-2xl overflow-hidden bg-black border border-[#262E42] shadow-2xl flex items-center justify-center"
          >
            <div id="lpp-visual-layer" class="absolute inset-0"></div>
            <div class="absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-black/30 pointer-events-none"></div>

            <!-- Live Karaoke Subtitle Overlay -->
            <div id="lpp-subtitle-overlay" class="absolute inset-x-3 flex justify-center pointer-events-none z-10"></div>
          </div>
        </div>

        <!-- Transport, Scrubber & Scene Markers -->
        <div class="lg:col-span-7 space-y-4">
          <div class="rounded-xl bg-[#121620] border border-[#262E42] p-3.5 space-y-3">
            <div class="flex items-center justify-between gap-3">
              <div class="flex items-center gap-2">
                <button
                  id="lpp-play-btn"
                  type="button"
                  class="px-4 py-2 rounded-xl bg-gradient-to-r from-violet-600 to-cyan-500 hover:brightness-110 text-white text-xs font-bold shadow transition flex items-center gap-1.5"
                >
                  <span id="lpp-play-icon">▶</span>
                  <span id="lpp-play-label">Phát Preview</span>
                </button>
                <button
                  id="lpp-restart-btn"
                  type="button"
                  class="px-3 py-2 rounded-xl bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-xs font-medium text-slate-300 transition"
                  title="Quay về đầu"
                >
                  ↺ 0:00
                </button>
              </div>

              <div class="font-mono text-xs text-slate-300">
                <span id="lpp-time-current" class="text-cyan-300 font-bold">00:00.0</span>
                <span class="text-slate-500"> / </span>
                <span id="lpp-time-total">${formatMs(totalDurationMs)}</span>
              </div>
            </div>

            <!-- Scrubber -->
            <div class="space-y-1.5">
              <input
                id="lpp-scrubber"
                type="range"
                min="0"
                max="${totalDurationMs}"
                step="50"
                value="${playheadMs}"
                class="studio-range w-full cursor-pointer"
              />
              <div id="lpp-scene-markers" class="flex flex-wrap gap-1.5 pt-1"></div>
            </div>
          </div>

          <div class="rounded-xl bg-[#121620] border border-[#262E42] p-3 text-xs text-slate-400 space-y-1">
            <div class="font-semibold text-slate-200 flex items-center justify-between">
              <span>Chi tiết Scene đang phát</span>
              <span id="lpp-tts-status" class="text-[11px] font-mono text-slate-500"></span>
            </div>
            <p id="lpp-scene-caption-preview" class="text-slate-300 leading-relaxed min-h-[2.25rem]">
              —
            </p>
          </div>
        </div>
      </div>
    </div>
  `;

  const stageEl = container.querySelector("#lpp-stage");
  const visualLayerEl = container.querySelector("#lpp-visual-layer");
  const subtitleOverlayEl = container.querySelector("#lpp-subtitle-overlay");
  const activeScenePillEl = container.querySelector("#lpp-active-scene-pill");
  const playBtn = container.querySelector("#lpp-play-btn");
  const playIconEl = container.querySelector("#lpp-play-icon");
  const playLabelEl = container.querySelector("#lpp-play-label");
  const restartBtn = container.querySelector("#lpp-restart-btn");
  const scrubberEl = container.querySelector("#lpp-scrubber");
  const timeCurrentEl = container.querySelector("#lpp-time-current");
  const timeTotalEl = container.querySelector("#lpp-time-total");
  const markersEl = container.querySelector("#lpp-scene-markers");
  const captionPreviewEl = container.querySelector("#lpp-scene-caption-preview");
  const ttsToggleEl = container.querySelector("#lpp-tts-toggle");
  const ttsStatusEl = container.querySelector("#lpp-tts-status");

  const findActiveScene = (ms) => {
    if (!scenes.length) return null;
    const match = scenes.find(
      (sc) => ms >= Number(sc.start_ms || 0) && ms < Number(sc.end_ms || 0)
    );
    if (match) return match;
    if (ms >= Number(scenes[scenes.length - 1].end_ms || 0)) {
      return scenes[scenes.length - 1];
    }
    return scenes[0];
  };

  const renderMarkers = (activeSceneId) => {
    if (!markersEl) return;
    markersEl.innerHTML = scenes
      .map((sc) => {
        const isCurr = String(sc.id) === String(activeSceneId);
        return `
          <button
            type="button"
            data-seek-ms="${Number(sc.start_ms || 0)}"
            class="px-2 py-1 rounded-lg text-[11px] font-mono border transition ${
              isCurr
                ? "bg-violet-500/25 border-violet-400 text-white font-bold"
                : "bg-[#0B0D12] border-[#262E42] text-slate-400 hover:text-white hover:border-slate-500"
            }"
            title="Nhảy tới Scene #${Number(sc.scene_index ?? 0) + 1} (${formatMs(sc.start_ms)})"
          >
            #${Number(sc.scene_index ?? 0) + 1}
          </button>
        `;
      })
      .join("");
  };

  const renderActiveVisual = (scene) => {
    if (!visualLayerEl) return;
    const sid = scene ? String(scene.id) : "none";
    const url = scene?.visual_url || "";
    if (sid === renderedVisualSceneId && url === renderedVisualUrl) {
      const videoEl = visualLayerEl.querySelector("video");
      if (videoEl) {
        if (isPlaying && videoEl.paused) {
          videoEl.play().catch(() => {});
        } else if (!isPlaying && !videoEl.paused) {
          videoEl.pause();
        }
      }
      return;
    }

    renderedVisualSceneId = sid;
    renderedVisualUrl = url;

    if (!scene || !url || !isBrowserPlayableUrl(url)) {
      visualLayerEl.innerHTML = `
        <div class="w-full h-full flex flex-col items-center justify-center text-center p-4 bg-gradient-to-br from-[#161B28] to-[#0B0D12] text-slate-400">
          <span class="text-2xl mb-1.5">🎬</span>
          <span class="text-xs font-semibold text-slate-200">Scene #${
            scene ? Number(scene.scene_index ?? 0) + 1 : 1
          }</span>
          <span class="text-[11px] text-slate-500 mt-1 break-all">
            ${url ? escapeHtml(String(url).split(/[/\\]/).pop()) : "Chưa có visual"}
          </span>
        </div>
      `;
      return;
    }

    const kind = detectMediaKind(url, scene.visual_source);
    if (kind === "image") {
      visualLayerEl.innerHTML = `
        <img
          src="${escapeHtml(url)}"
          alt="Scene ${Number(scene.scene_index ?? 0) + 1}"
          class="w-full h-full object-cover live-preview-kenburns"
        />
      `;
    } else {
      visualLayerEl.innerHTML = `
        <video
          src="${escapeHtml(url)}"
          muted
          loop
          playsinline
          ${isPlaying ? "autoplay" : ""}
          class="w-full h-full object-cover"
        ></video>
      `;
    }
  };

  const renderSubtitleOverlay = (scene) => {
    if (!subtitleOverlayEl) return;
    const cfg = projectDetail?.pipeline_config || {};
    const burnEnabled = cfg.burn_subtitles !== false;
    if (!burnEnabled || !scene) {
      subtitleOverlayEl.innerHTML = "";
      return;
    }

    const text = getSceneText(scene, projectDetail.mode);
    if (!text) {
      subtitleOverlayEl.innerHTML = "";
      return;
    }

    const style = cfg.subtitle_style || {};
    const pos = style.position || "bottom";
    const posClass =
      pos === "top"
        ? "top-4 bottom-auto"
        : pos === "middle"
        ? "top-1/2 -translate-y-1/2"
        : "bottom-4 top-auto";

    subtitleOverlayEl.className = `absolute inset-x-3 flex justify-center pointer-events-none z-10 ${posClass}`;

    const scaledFontPx = Math.max(12, Math.min(26, Math.round((Number(style.font_size) || 56) * 0.34)));
    const primaryColor = style.primary_color || "#FFFFFF";
    const highlightColor = style.highlight_color || "#FACC15";
    const outlineColor = style.outline_color || "#000000";
    const fontFamily = style.font_name || "Inter, sans-serif";

    const tokens = buildKaraokeTokens(scene, text);
    const wordsHtml = tokens
      .map((tk) => {
        const isCurrent = playheadMs >= tk.start_ms && playheadMs <= tk.end_ms;
        const isSpoken = playheadMs >= tk.start_ms;
        const color = isSpoken ? highlightColor : primaryColor;
        const activeClass = isCurrent ? "active is-active" : "";
        return `<span class="karaoke-word live-karaoke-word ${activeClass}" style="color: ${escapeHtml(
          color
        )}">${escapeHtml(tk.word)}</span>`;
      })
      .join(" ");

    subtitleOverlayEl.innerHTML = `
      <div
        class="karaoke-subtitle-line text-center leading-snug max-w-full"
        style="font-family: ${escapeHtml(fontFamily)}; font-size: ${scaledFontPx}px; text-shadow: 0 2px 6px ${escapeHtml(
      outlineColor
    )}, 0 0 3px ${escapeHtml(outlineColor)};"
      >
        ${wordsHtml}
      </div>
    `;
  };

  const triggerSceneTtsIfNeeded = async (scene) => {
    if (!ttsTimelineEnabled || !isPlaying || !scene || isDestroyed) return;
    const text = getSceneText(scene, projectDetail.mode);
    if (!text) return;

    const cfg = projectDetail.pipeline_config || {};
    const ttsProvider = cfg.tts_provider || store.getState().providers?.defaults?.tts || "edge_tts";
    const voiceId = scene.voice_id || cfg.voice_id || "";
    const ratePct = Number(scene.tts_rate_pct ?? 0);
    const cacheKey = `${scene.id}|${ttsProvider}|${voiceId}|${ratePct}|${text}`;

    if (lastTtsSceneKey === cacheKey) return;
    lastTtsSceneKey = cacheKey;

    let audioUrl = ttsBlobCache.get(cacheKey);
    const seq = ++ttsFetchSeq;

    if (!audioUrl) {
      if (ttsStatusEl) ttsStatusEl.textContent = "Đang tải TTS...";
      try {
        const res = await api.previewSceneTts(projectDetail.id, scene.id, {
          text,
          voice_id: voiceId || undefined,
          tts_provider: ttsProvider,
          tts_rate_pct: ratePct,
        });
        if (isDestroyed) return;
        audioUrl = URL.createObjectURL(res.blob);
        ttsBlobCache.set(cacheKey, audioUrl);
      } catch (err) {
        if (ttsStatusEl) ttsStatusEl.textContent = "TTS lỗi";
        return;
      }
    }

    if (isDestroyed || !isPlaying || !ttsTimelineEnabled || seq !== ttsFetchSeq) return;
    if (ttsStatusEl) ttsStatusEl.textContent = `Đang phát TTS #${Number(scene.scene_index ?? 0) + 1}`;
    ttsAudio.src = audioUrl;
    ttsAudio.play().catch(() => {});
  };

  const syncFrame = () => {
    const activeScene = findActiveScene(playheadMs);
    if (activeScenePillEl) {
      activeScenePillEl.textContent = activeScene
        ? `Scene #${Number(activeScene.scene_index ?? 0) + 1}`
        : "No Scene";
    }
    if (timeCurrentEl) timeCurrentEl.textContent = formatMs(playheadMs);
    if (timeTotalEl) timeTotalEl.textContent = formatMs(totalDurationMs);
    if (scrubberEl) {
      scrubberEl.max = String(totalDurationMs);
      scrubberEl.value = String(Math.round(playheadMs));
    }
    if (captionPreviewEl) {
      captionPreviewEl.textContent = activeScene
        ? getSceneText(activeScene, projectDetail.mode) || "(Không có lời thoại)"
        : "Chưa có scene nào";
    }

    renderActiveVisual(activeScene);
    renderSubtitleOverlay(activeScene);
    renderMarkers(activeScene?.id);
    triggerSceneTtsIfNeeded(activeScene);
  };

  const setPlaying = (nextPlaying) => {
    isPlaying = Boolean(nextPlaying);
    if (playIconEl) playIconEl.textContent = isPlaying ? "⏸" : "▶";
    if (playLabelEl) playLabelEl.textContent = isPlaying ? "Tạm dừng" : "Phát Preview";

    if (!isPlaying) {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      ttsAudio.pause();
      if (ttsStatusEl) ttsStatusEl.textContent = "";
      lastTtsSceneKey = null;
      syncFrame();
      return;
    }

    if (playheadMs >= totalDurationMs) {
      playheadMs = 0;
    }
    lastFrameTs = performance.now();
    const tick = (now) => {
      if (!isPlaying || isDestroyed) return;
      const dt = Math.min(120, Math.max(0, now - lastFrameTs));
      lastFrameTs = now;
      playheadMs += dt;
      if (playheadMs >= totalDurationMs) {
        playheadMs = totalDurationMs;
        syncFrame();
        setPlaying(false);
        return;
      }
      syncFrame();
      rafId = requestAnimationFrame(tick);
    };
    syncFrame();
    rafId = requestAnimationFrame(tick);
  };

  playBtn?.addEventListener("click", () => setPlaying(!isPlaying));
  restartBtn?.addEventListener("click", () => {
    lastTtsSceneKey = null;
    playheadMs = 0;
    syncFrame();
  });

  scrubberEl?.addEventListener("input", (e) => {
    lastTtsSceneKey = null;
    playheadMs = Math.max(0, Math.min(totalDurationMs, Number(e.target.value) || 0));
    syncFrame();
  });

  markersEl?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-seek-ms]");
    if (!btn) return;
    lastTtsSceneKey = null;
    playheadMs = Math.max(0, Math.min(totalDurationMs, Number(btn.getAttribute("data-seek-ms")) || 0));
    syncFrame();
  });

  ttsToggleEl?.addEventListener("change", (e) => {
    ttsTimelineEnabled = Boolean(e.target.checked);
    lastTtsSceneKey = null;
    if (!ttsTimelineEnabled) {
      ttsAudio.pause();
      if (ttsStatusEl) ttsStatusEl.textContent = "";
    } else if (isPlaying) {
      const sc = findActiveScene(playheadMs);
      triggerSceneTtsIfNeeded(sc);
    } else {
      showToast("Bật Play Preview để nghe TTS đồng bộ theo từng cảnh", "info");
    }
  });

  syncFrame();

  return {
    updateData(nextProjectDetail) {
      if (!nextProjectDetail || isDestroyed) return;
      projectDetail = nextProjectDetail;
      scenes = normalizeScenes(projectDetail);
      totalDurationMs = computeTotalDurationMs(scenes);
      playheadMs = Math.min(playheadMs, totalDurationMs);
      if (stageEl) {
        stageEl.className = `live-preview-stage karaoke-preview-stage relative ${getAspectStageClass(
          projectDetail.aspect_ratio
        )} rounded-2xl overflow-hidden bg-black border border-[#262E42] shadow-2xl flex items-center justify-center`;
      }
      renderedVisualSceneId = null;
      lastTtsSceneKey = null;
      syncFrame();
    },

    seekToMs(ms) {
      if (isDestroyed) return;
      lastTtsSceneKey = null;
      playheadMs = Math.max(0, Math.min(totalDurationMs, Number(ms) || 0));
      syncFrame();
    },

    destroy() {
      isDestroyed = true;
      setPlaying(false);
      ttsAudio.src = "";
      for (const url of ttsBlobCache.values()) {
        try {
          URL.revokeObjectURL(url);
        } catch {
          // Ignore
        }
      }
      ttsBlobCache.clear();
    },
  };
}
