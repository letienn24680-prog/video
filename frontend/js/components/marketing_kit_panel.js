/**
 * Stage 5 Interactive Component: AI Viral Marketing Kit & Thumbnail Generator
 * Generates viral hooks, SEO titles, description, hashtags, and AI thumbnails
 * for completed projects with 1-click Copy and Thumbnail download.
 */

import { api } from "../core/api.js";
import { showToast, store } from "../core/state.js";

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function updateProjectPipelineConfigInStore(projectId, patch) {
  const current = store.getState().projects || [];
  const nextProjects = current.map((p) => {
    if (String(p.id) !== String(projectId)) return p;
    return {
      ...p,
      pipeline_config: {
        ...(p.pipeline_config || {}),
        ...patch,
      },
    };
  });
  store.setState({ projects: nextProjects });
}

async function copyText(text, label = "Nội dung") {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast(`Đã sao chép ${label}!`, "success");
  } catch {
    showToast(`${label}: ${text}`, "info");
  }
}

/**
 * Render Marketing Kit & Thumbnail AI panel inside `container`.
 * @param {HTMLElement} container
 * @param {object} project
 * @param {(updatedProject: object) => void} [onUpdated]
 */
export function renderMarketingKitPanel(container, project, onUpdated) {
  if (!container || !project?.id) return;

  let currentProject = project;
  let marketingKit = project.pipeline_config?.marketing_kit || null;
  let thumbnailUrl = project.pipeline_config?.thumbnail_url || null;
  let customThumbPrompt = "";
  let loadingKit = false;
  let loadingThumb = false;

  const render = () => {
    const hooks = Array.isArray(marketingKit?.hooks) ? marketingKit.hooks : [];
    const titles = Array.isArray(marketingKit?.titles) ? marketingKit.titles : [];
    const description = marketingKit?.description || "";
    const hashtags = Array.isArray(marketingKit?.hashtags) ? marketingKit.hashtags : [];

    container.innerHTML = `
      <div class="rounded-2xl bg-[#0B0D12] border border-[#262E42] p-4 space-y-4">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div>
            <h4 class="text-sm font-bold text-white flex items-center gap-2">
              <span class="text-fuchsia-400">✨</span>
              AI Viral Marketing Kit &amp; Thumbnail
            </h4>
            <p class="text-[11px] text-slate-400 mt-0.5">
              Tự động sinh Hook mở đầu, tiêu đề SEO, mô tả, hashtag và ảnh bìa thu hút click.
            </p>
          </div>

          <button
            type="button"
            id="mk-generate-kit-btn"
            ${loadingKit ? "disabled" : ""}
            class="px-3.5 py-2 rounded-xl bg-gradient-to-r from-fuchsia-600 to-violet-600 hover:brightness-110 text-white text-xs font-bold shadow transition disabled:opacity-60 shrink-0"
          >
            ${
              loadingKit
                ? "⏳ Đang phân tích kịch bản..."
                : marketingKit
                ? "🔄 Tạo lại Hook, Tiêu đề SEO & Hashtag"
                : "✨ Tạo Hook Viral, Tiêu đề SEO & Hashtag (AI)"
            }
          </button>
        </div>

        <!-- Thumbnail Generator Section -->
        <div class="rounded-xl bg-[#121620] border border-[#262E42] p-3.5 space-y-3">
          <div class="flex flex-col sm:flex-row gap-2">
            <input
              id="mk-thumb-prompt"
              type="text"
              value="${escapeHtml(customThumbPrompt)}"
              placeholder="Mô tả Thumbnail tùy chọn (để trống AI sẽ tự thiết kế theo nội dung video)..."
              class="flex-1 px-3 py-2 rounded-xl bg-[#0B0D12] border border-[#262E42] text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-400"
            />
            <button
              type="button"
              id="mk-generate-thumb-btn"
              ${loadingThumb ? "disabled" : ""}
              class="px-3.5 py-2 rounded-xl bg-cyan-500/20 hover:bg-cyan-500/30 border border-cyan-500/40 text-cyan-200 text-xs font-bold transition disabled:opacity-60 shrink-0"
            >
              ${loadingThumb ? "⏳ Đang vẽ Thumbnail..." : "🖼️ Tạo Thumbnail AI"}
            </button>
          </div>

          ${
            thumbnailUrl
              ? `
            <div class="flex flex-col sm:flex-row items-start sm:items-center gap-3 pt-1">
              <div class="w-40 aspect-video rounded-xl overflow-hidden bg-black border border-[#262E42] shrink-0">
                <img
                  src="${escapeHtml(thumbnailUrl)}"
                  alt="AI Thumbnail"
                  class="w-full h-full object-cover"
                />
              </div>
              <div class="space-y-2 min-w-0">
                <div class="text-xs font-semibold text-emerald-300">Thumbnail AI đã sẵn sàng</div>
                <div class="flex flex-wrap gap-2">
                  <a
                    href="${escapeHtml(thumbnailUrl)}"
                    download="thumbnail_${escapeHtml(currentProject.id)}.png"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="px-3 py-1.5 rounded-lg bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/40 text-emerald-200 text-xs font-semibold transition"
                  >
                    ⬇ Tải Thumbnail
                  </a>
                  <button
                    type="button"
                    id="mk-copy-thumb-url"
                    class="px-3 py-1.5 rounded-lg bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-xs text-slate-300 transition"
                  >
                    Sao chép URL
                  </button>
                </div>
              </div>
            </div>
          `
              : ""
          }
        </div>

        <!-- Generated Marketing Copy -->
        ${
          marketingKit
            ? `
          <div class="space-y-3">
            ${
              hooks.length
                ? `
              <div class="space-y-1.5">
                <div class="text-[11px] font-bold uppercase tracking-wider text-violet-300">🎣 Viral Hooks (3 giây đầu)</div>
                <div class="space-y-1.5">
                  ${hooks
                    .map(
                      (hook, idx) => `
                    <div class="flex items-center justify-between gap-2 rounded-xl bg-[#121620] border border-[#262E42] px-3 py-2 text-xs">
                      <span class="text-slate-100 leading-relaxed">${escapeHtml(hook)}</span>
                      <button
                        type="button"
                        data-copy-hook="${idx}"
                        class="marketing-chip shrink-0 px-2.5 py-1 rounded-lg bg-[#191F2E] hover:bg-violet-500/20 border border-[#262E42] text-[11px] text-slate-300 hover:text-white transition"
                      >
                        Copy
                      </button>
                    </div>
                  `
                    )
                    .join("")}
                </div>
              </div>
            `
                : ""
            }

            ${
              titles.length
                ? `
              <div class="space-y-1.5">
                <div class="text-[11px] font-bold uppercase tracking-wider text-cyan-300">🏷️ Tiêu đề SEO đề xuất</div>
                <div class="space-y-1.5">
                  ${titles
                    .map(
                      (title, idx) => `
                    <div class="flex items-center justify-between gap-2 rounded-xl bg-[#121620] border border-[#262E42] px-3 py-2 text-xs">
                      <span class="text-slate-100 font-medium">${escapeHtml(title)}</span>
                      <button
                        type="button"
                        data-copy-title="${idx}"
                        class="marketing-chip shrink-0 px-2.5 py-1 rounded-lg bg-[#191F2E] hover:bg-cyan-500/20 border border-[#262E42] text-[11px] text-slate-300 hover:text-white transition"
                      >
                        Copy
                      </button>
                    </div>
                  `
                    )
                    .join("")}
                </div>
              </div>
            `
                : ""
            }

            ${
              description
                ? `
              <div class="space-y-1.5">
                <div class="flex items-center justify-between">
                  <span class="text-[11px] font-bold uppercase tracking-wider text-slate-300">📝 Mô tả bài đăng</span>
                  <button
                    type="button"
                    id="mk-copy-desc-btn"
                    class="marketing-chip px-2.5 py-1 rounded-lg bg-[#191F2E] hover:bg-[#20273A] border border-[#262E42] text-[11px] text-slate-300 hover:text-white transition"
                  >
                    Copy mô tả
                  </button>
                </div>
                <div class="rounded-xl bg-[#121620] border border-[#262E42] p-3 text-xs text-slate-300 leading-relaxed whitespace-pre-line">
                  ${escapeHtml(description)}
                </div>
              </div>
            `
                : ""
            }

            ${
              hashtags.length
                ? `
              <div class="space-y-1.5">
                <div class="flex items-center justify-between">
                  <span class="text-[11px] font-bold uppercase tracking-wider text-emerald-300"># Hashtags</span>
                  <button
                    type="button"
                    id="mk-copy-all-tags-btn"
                    class="marketing-chip px-2.5 py-1 rounded-lg bg-[#191F2E] hover:bg-emerald-500/20 border border-[#262E42] text-[11px] text-slate-300 hover:text-white transition"
                  >
                    Copy tất cả Hashtag
                  </button>
                </div>
                <div class="flex flex-wrap gap-1.5">
                  ${hashtags
                    .map(
                      (tag, idx) => `
                    <button
                      type="button"
                      data-copy-tag="${idx}"
                      class="marketing-chip px-2.5 py-1 rounded-full bg-emerald-500/10 hover:bg-emerald-500/20 border border-emerald-500/30 text-[11px] font-mono text-emerald-300 transition"
                      title="Bấm để sao chép"
                    >
                      ${escapeHtml(tag.startsWith("#") ? tag : `#${tag}`)}
                    </button>
                  `
                    )
                    .join("")}
                </div>
              </div>
            `
                : ""
            }
          </div>
        `
            : `
          <div class="rounded-xl bg-[#121620]/60 border border-dashed border-[#262E42] p-4 text-center text-xs text-slate-500">
            Bấm <strong>Tạo Hook Viral, Tiêu đề SEO &amp; Hashtag (AI)</strong> để AI sinh bộ nội dung đăng TikTok / Shorts / Reels từ kịch bản của bạn.
          </div>
        `
        }
      </div>
    `;

    // Wire events
    container.querySelector("#mk-thumb-prompt")?.addEventListener("input", (e) => {
      customThumbPrompt = e.target.value;
    });

    container.querySelector("#mk-generate-kit-btn")?.addEventListener("click", async () => {
      loadingKit = true;
      render();
      try {
        const res = await api.generateMarketingKit(currentProject.id);
        marketingKit = res?.marketing_kit || res;
        currentProject = {
          ...currentProject,
          pipeline_config: {
            ...(currentProject.pipeline_config || {}),
            marketing_kit: marketingKit,
          },
        };
        updateProjectPipelineConfigInStore(currentProject.id, { marketing_kit: marketingKit });
        showToast("Đã tạo AI Marketing Kit!", "success");
        onUpdated?.(currentProject);
      } catch (err) {
        showToast(err?.message || "Không thể tạo Marketing Kit", "error");
      } finally {
        loadingKit = false;
        render();
      }
    });

    container.querySelector("#mk-generate-thumb-btn")?.addEventListener("click", async () => {
      loadingThumb = true;
      render();
      try {
        const res = await api.generateThumbnail(
          currentProject.id,
          customThumbPrompt.trim() || undefined
        );
        thumbnailUrl = res?.thumbnail_url || res?.url || "";
        currentProject = {
          ...currentProject,
          pipeline_config: {
            ...(currentProject.pipeline_config || {}),
            thumbnail_url: thumbnailUrl,
          },
        };
        updateProjectPipelineConfigInStore(currentProject.id, { thumbnail_url: thumbnailUrl });
        showToast("Đã tạo Thumbnail AI thành công!", "success");
        onUpdated?.(currentProject);
      } catch (err) {
        showToast(err?.message || "Không thể tạo Thumbnail AI", "error");
      } finally {
        loadingThumb = false;
        render();
      }
    });

    container.querySelector("#mk-copy-thumb-url")?.addEventListener("click", () => {
      copyText(thumbnailUrl, "URL Thumbnail");
    });

    container.querySelectorAll("[data-copy-hook]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = Number(btn.getAttribute("data-copy-hook"));
        copyText(hooks[idx], "Hook Viral");
      });
    });

    container.querySelectorAll("[data-copy-title]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = Number(btn.getAttribute("data-copy-title"));
        copyText(titles[idx], "Tiêu đề SEO");
      });
    });

    container.querySelector("#mk-copy-desc-btn")?.addEventListener("click", () => {
      copyText(description, "Mô tả");
    });

    container.querySelector("#mk-copy-all-tags-btn")?.addEventListener("click", () => {
      const joined = hashtags.map((t) => (t.startsWith("#") ? t : `#${t}`)).join(" ");
      copyText(joined, "Tất cả Hashtag");
    });

    container.querySelectorAll("[data-copy-tag]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = Number(btn.getAttribute("data-copy-tag"));
        const t = hashtags[idx] || "";
        copyText(t.startsWith("#") ? t : `#${t}`, "Hashtag");
      });
    });
  };

  render();
}
