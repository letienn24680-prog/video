/**
 * AI Video Studio REST API Client
 * Wraps fetch() with automatic Bearer JWT injection, structured error parsing,
 * 401 handling, FormData uploads, and Blob audio preview support.
 */

import { STORAGE_KEYS, showToast, store } from "./state.js";

const API_BASE = "";

export class ApiError extends Error {
  constructor(message, { status = 0, code = "API_ERROR", details = {} } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function resolveAccessToken() {
  const fromStore = store.getState().accessToken;
  if (fromStore && typeof fromStore === "string" && fromStore.trim()) {
    return fromStore.trim();
  }
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN);
    if (raw && raw.trim()) {
      return raw.trim();
    }
    const sessionJson = localStorage.getItem(STORAGE_KEYS.AUTH_SESSION);
    if (sessionJson) {
      const parsed = JSON.parse(sessionJson);
      if (parsed?.access_token) {
        return String(parsed.access_token).trim();
      }
    }
  } catch {
    // Ignore malformed localStorage
  }
  return null;
}

function buildQuery(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value).trim() !== "") {
      search.set(key, String(value));
    }
  });
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

function buildApiErrorFromPayload(status, statusText, payload, fallbackText = "Request failed") {
  const errObj = payload?.error;
  const message =
    errObj?.message ||
    payload?.detail ||
    payload?.message ||
    `HTTP ${status} ${statusText || fallbackText}`;
  const code = errObj?.code || `HTTP_${status}`;
  const details = errObj?.details || {};

  return new ApiError(message, {
    status,
    code,
    details,
  });
}

async function parseErrorResponse(response) {
  let payload = null;
  const contentType = response.headers.get("content-type") || "";
  try {
    if (contentType.includes("application/json")) {
      payload = await response.json();
    } else {
      const text = await response.text();
      payload = text ? { message: text } : null;
    }
  } catch {
    payload = null;
  }

  return buildApiErrorFromPayload(response.status, response.statusText, payload);
}

function handleUnauthorized(hadToken) {
  if (hadToken) {
    store.clearSession();
    showToast("Phiên đăng nhập đã hết hạn hoặc không hợp lệ. Vui lòng đăng nhập lại.", "warning");
  }
  window.dispatchEvent(new CustomEvent("studio:unauthorized", { detail: { hadToken } }));
}

async function request(path, options = {}) {
  const {
    method = "GET",
    body,
    headers = {},
    auth = true,
    responseType = "json",
    signal,
  } = options;

  const finalHeaders = new Headers(headers);
  const token = auth ? resolveAccessToken() : null;

  if (token && !finalHeaders.has("Authorization")) {
    finalHeaders.set("Authorization", `Bearer ${token}`);
  }

  let fetchBody = body;
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  if (body !== undefined && body !== null && !isFormData && typeof body === "object") {
    if (!finalHeaders.has("Content-Type")) {
      finalHeaders.set("Content-Type", "application/json");
    }
    fetchBody = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: finalHeaders,
      body: fetchBody,
      signal,
    });
  } catch (networkErr) {
    if (networkErr?.name === "AbortError") {
      throw networkErr;
    }
    throw new ApiError("Không thể kết nối tới máy chủ AI Video Studio.", {
      status: 0,
      code: "NETWORK_ERROR",
      details: { cause: networkErr?.message || String(networkErr) },
    });
  }

  if (!response.ok) {
    if (response.status === 401) {
      handleUnauthorized(Boolean(token));
    }
    throw await parseErrorResponse(response);
  }

  if (responseType === "raw") {
    return response;
  }

  if (responseType === "blob") {
    const blob = await response.blob();
    return { blob, headers: response.headers };
  }

  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

function parseXhrError(xhr) {
  let payload = null;
  const rawText = xhr.responseText || "";
  try {
    if (rawText) {
      payload = JSON.parse(rawText);
    }
  } catch {
    payload = rawText ? { message: rawText } : null;
  }

  return buildApiErrorFromPayload(xhr.status, xhr.statusText, payload, "Upload failed");
}

function uploadMultipartWithProgress(
  path,
  formData,
  { onProgress, signal, timeoutMs = 600000 } = {}
) {
  if (typeof XMLHttpRequest === "undefined") {
    return request(path, { method: "POST", body: formData, signal });
  }

  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      const abortErr = new Error("Quá trình tải lên đã bị hủy.");
      abortErr.name = "AbortError";
      reject(abortErr);
      return;
    }

    const xhr = new XMLHttpRequest();
    const token = resolveAccessToken();
    let settled = false;

    const cleanup = () => {
      if (signal && onSignalAbort) {
        signal.removeEventListener("abort", onSignalAbort);
      }
    };

    const onSignalAbort = () => {
      if (!settled) {
        xhr.abort();
      }
    };

    if (signal) {
      signal.addEventListener("abort", onSignalAbort, { once: true });
    }

    xhr.open("POST", `${API_BASE}${path}`, true);
    xhr.timeout = timeoutMs;
    if (token) {
      xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    }

    xhr.upload.onprogress = (e) => {
      if (typeof onProgress !== "function") return;
      if (e.lengthComputable && e.total > 0) {
        const percent = Math.min(95, Math.max(1, Math.round((e.loaded / e.total) * 95)));
        onProgress({
          loaded: e.loaded,
          total: e.total,
          percent,
          stage: e.loaded >= e.total ? "processing" : "uploading",
        });
      }
    };

    xhr.upload.onload = () => {
      if (typeof onProgress === "function") {
        onProgress({
          loaded: null,
          total: null,
          percent: 96,
          stage: "processing",
        });
      }
    };

    xhr.onload = () => {
      if (settled) return;
      settled = true;
      cleanup();

      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const parsed = xhr.responseText ? JSON.parse(xhr.responseText) : null;
          resolve(parsed);
        } catch {
          resolve(xhr.responseText || null);
        }
        return;
      }

      if (xhr.status === 401) {
        handleUnauthorized(Boolean(token));
      }
      reject(parseXhrError(xhr));
    };

    xhr.onerror = () => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(
        new ApiError("Lỗi kết nối mạng khi tải file lên máy chủ.", {
          status: 0,
          code: "NETWORK_ERROR",
        })
      );
    };

    xhr.ontimeout = () => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(
        new ApiError("Quá thời gian chờ tải file lên (timeout). Vui lòng thử lại.", {
          status: 0,
          code: "UPLOAD_TIMEOUT",
        })
      );
    };

    xhr.onabort = () => {
      if (settled) return;
      settled = true;
      cleanup();
      const abortErr = new Error("Đã hủy tải file lên.");
      abortErr.name = "AbortError";
      reject(abortErr);
    };

    xhr.send(formData);
  });
}

export const api = {
  /**
   * GET /api/v1/public-config (no auth required)
   */
  getPublicConfig() {
    return request("/api/v1/public-config", { auth: false });
  },

  /**
   * GET /api/v1/health
   */
  getHealth() {
    return request("/api/v1/health", { auth: false });
  },

  /**
   * GET /api/v1/providers
   */
  getProviders() {
    return request("/api/v1/providers", { auth: false });
  },

  /**
   * GET /api/v1/providers/tts/voices
   */
  getTtsVoices(provider, language) {
    const qs = buildQuery({ provider, language });
    return request(`/api/v1/providers/tts/voices${qs}`, { auth: false });
  },

  /**
   * POST /api/v1/projects/upload-source (multipart FormData with real XHR progress)
   */
  uploadSourceFile(file, options = {}) {
    const formData = new FormData();
    formData.append("file", file);
    return uploadMultipartWithProgress("/api/v1/projects/upload-source", formData, options);
  },

  /**
   * POST /api/v1/projects
   */
  createProject(payload) {
    return request("/api/v1/projects", {
      method: "POST",
      body: payload,
    });
  },

  /**
   * POST /api/v1/projects/batch
   */
  createBatchProjects(payload) {
    return request("/api/v1/projects/batch", {
      method: "POST",
      body: payload,
    });
  },

  /**
   * GET /api/v1/projects
   */
  listProjects(params = {}) {
    const qs = buildQuery({
      status: params.status,
      mode: params.mode,
      limit: params.limit,
      offset: params.offset,
    });
    return request(`/api/v1/projects${qs}`);
  },

  /**
   * GET /api/v1/projects/{projectId}
   */
  getProject(projectId) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}`);
  },

  /**
   * PATCH /api/v1/projects/{projectId}/scenes/{sceneId}
   */
  updateScene(projectId, sceneId, payload) {
    return request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}`,
      {
        method: "PATCH",
        body: payload,
      }
    );
  },

  /**
   * POST /api/v1/projects/{projectId}/scenes/{sceneId}/preview-tts
   * Returns { blob, durationMs, provider, voiceId, contentType }
   */
  async previewSceneTts(projectId, sceneId, payload = {}) {
    const { blob, headers } = await request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(
        sceneId
      )}/preview-tts`,
      {
        method: "POST",
        body: payload,
        responseType: "blob",
      }
    );
    const rawDuration = headers.get("X-Audio-Duration-Ms");
    const parsedDuration = rawDuration !== null ? Number.parseInt(rawDuration, 10) : 0;
    return {
      blob,
      durationMs: Number.isFinite(parsedDuration) ? parsedDuration : 0,
      provider: headers.get("X-TTS-Provider") || null,
      voiceId: headers.get("X-Voice-Id") || null,
      contentType: blob.type || headers.get("Content-Type") || "audio/mpeg",
    };
  },

  /**
   * POST /api/v1/projects/{projectId}/scenes/{sceneId}/swap-visual
   */
  swapSceneVisual(projectId, sceneId, payload) {
    return request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(
        sceneId
      )}/swap-visual`,
      {
        method: "POST",
        body: payload,
      }
    );
  },

  /**
   * POST /api/v1/projects/{projectId}/scenes/{sceneId}/upload-visual
   */
  uploadSceneVisual(projectId, sceneId, file) {
    const formData = new FormData();
    formData.append("file", file);
    return request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(
        sceneId
      )}/upload-visual`,
      {
        method: "POST",
        body: formData,
      }
    );
  },

  /**
   * POST /api/v1/projects/{projectId}/render
   */
  renderProject(projectId, payload = {}) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}/render`, {
      method: "POST",
      body: payload,
    });
  },

  /**
   * POST /api/v1/projects/{projectId}/cancel
   */
  cancelProject(projectId) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}/cancel`, {
      method: "POST",
    });
  },

  /**
   * GET /api/v1/billing/credits
   */
  getBillingCredits(params = {}) {
    const qs = buildQuery({
      limit: params.limit,
      offset: params.offset,
    });
    return request(`/api/v1/billing/credits${qs}`);
  },

  /**
   * POST /api/v1/billing/topup-intent
   */
  createTopupIntent(amountVnd, gateway = "sepay") {
    return request("/api/v1/billing/topup-intent", {
      method: "POST",
      body: {
        amount_vnd: Number(amountVnd),
        gateway,
      },
    });
  },

  /**
   * GET /api/v1/billing/transactions/{txId}
   */
  getTransactionStatus(txId) {
    return request(`/api/v1/billing/transactions/${encodeURIComponent(txId)}`);
  },

  /**
   * POST /api/v1/auth/sandbox-token (no auth required when LOCAL_SANDBOX_MODE=true)
   */
  getSandboxToken() {
    return request("/api/v1/auth/sandbox-token", {
      method: "POST",
      auth: false,
    });
  },

  /**
   * GET /api/v1/bgm/presets
   */
  getBgmPresets() {
    return request("/api/v1/bgm/presets", { auth: false });
  },

  /**
   * Return preview URL for a BGM mood preset WAV stream
   */
  getBgmPreviewUrl(presetId) {
    return `/api/v1/bgm/presets/${encodeURIComponent(presetId)}/preview`;
  },

  /**
   * POST /api/v1/projects/upload-bgm (multipart FormData)
   */
  uploadBgmFile(file) {
    const formData = new FormData();
    formData.append("file", file);
    return request("/api/v1/projects/upload-bgm", {
      method: "POST",
      body: formData,
    });
  },

  /**
   * POST /api/v1/projects/{projectId}/marketing-kit
   */
  generateMarketingKit(projectId) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}/marketing-kit`, {
      method: "POST",
    });
  },

  /**
   * POST /api/v1/projects/{projectId}/thumbnail
   */
  generateThumbnail(projectId, prompt = null) {
    const body = prompt && String(prompt).trim() ? { prompt: String(prompt).trim() } : {};
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}/thumbnail`, {
      method: "POST",
      body,
    });
  },

  /**
   * GET /api/v1/projects/{projectId}/subtitles/export?format=srt|ass
   * Returns { blob, filename }
   */
  async exportSubtitles(projectId, format = "srt") {
    const fmt = String(format || "srt").toLowerCase();
    const { blob, headers } = await request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/subtitles/export?format=${encodeURIComponent(
        fmt
      )}`,
      {
        method: "GET",
        responseType: "blob",
      }
    );
    const disposition = headers.get("Content-Disposition") || "";
    const match = /filename\*?=(?:UTF-8''|")?([^";\n]+)/i.exec(disposition);
    const filename = match?.[1]?.replace(/["']/g, "").trim() || `subtitles_${projectId}.${fmt}`;
    return { blob, filename };
  },

  /**
   * GET /api/v1/projects/batch/{batchId}/download-zip
   * Returns { blob, filename }
   */
  async downloadBatchZip(batchId) {
    const { blob, headers } = await request(
      `/api/v1/projects/batch/${encodeURIComponent(batchId)}/download-zip`,
      {
        method: "GET",
        responseType: "blob",
      }
    );
    const disposition = headers.get("Content-Disposition") || "";
    const match = /filename\*?=(?:UTF-8''|")?([^";\n]+)/i.exec(disposition);
    const filename = match?.[1]?.replace(/["']/g, "").trim() || `batch_${batchId}.zip`;
    return { blob, filename };
  },

  /**
   * POST /api/v1/billing/upgrade-tier
   */
  upgradeTier(targetTier) {
    return request("/api/v1/billing/upgrade-tier", {
      method: "POST",
      body: { target_tier: targetTier },
    });
  },

  /**
   * PATCH /api/v1/projects/{projectId}
   */
  updateProject(projectId, payload) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /**
   * DELETE /api/v1/projects/{projectId}
   */
  deleteProject(projectId) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
      method: "DELETE",
    });
  },

  /**
   * POST /api/v1/projects/{projectId}/retry
   */
  retryProject(projectId) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}/retry`, {
      method: "POST",
    });
  },

  /**
   * POST /api/v1/projects/{projectId}/duplicate
   */
  duplicateProject(projectId, payload = {}) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}/duplicate`, {
      method: "POST",
      body: payload,
    });
  },

  /**
   * POST /api/v1/projects/{projectId}/scenes
   */
  createScene(projectId, payload) {
    return request(`/api/v1/projects/${encodeURIComponent(projectId)}/scenes`, {
      method: "POST",
      body: payload,
    });
  },

  /**
   * DELETE /api/v1/projects/{projectId}/scenes/{sceneId}
   */
  deleteScene(projectId, sceneId) {
    return request(
      `/api/v1/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}`,
      {
        method: "DELETE",
      }
    );
  },

  /**
   * GET /api/v1/billing/history?limit={limit}
   */
  getBillingHistory(limit = 50) {
    const qs = buildQuery({ limit });
    return request(`/api/v1/billing/history${qs}`);
  },

  /**
   * GET /api/v1/account/profile
   */
  getAccountProfile() {
    return request("/api/v1/account/profile");
  },

  /**
   * PATCH /api/v1/account/profile
   */
  updateAccountProfile(payload) {
    return request("/api/v1/account/profile", {
      method: "PATCH",
      body: payload,
    });
  },

  /**
   * GET /api/v1/system/diagnostics
   */
  getSystemDiagnostics() {
    return request("/api/v1/system/diagnostics", { auth: false });
  },

  /**
   * POST /api/v1/system/cleanup
   */
  runSystemCleanup() {
    return request("/api/v1/system/cleanup", {
      method: "POST",
    });
  },

  /**
   * POST /api/v1/auth/sandbox-register
   */
  sandboxRegister(email, password) {
    return request("/api/v1/auth/sandbox-register", {
      method: "POST",
      auth: false,
      body: { email, password },
    });
  },

  /**
   * POST /api/v1/auth/sandbox-login
   */
  sandboxLogin(email, password) {
    return request("/api/v1/auth/sandbox-login", {
      method: "POST",
      auth: false,
      body: { email, password },
    });
  },

  /**
   * POST /api/v1/system/config
   */
  updateSystemConfig(updates) {
    return request("/api/v1/system/config", {
      method: "POST",
      body: { updates: updates || {} },
    });
  },
};
