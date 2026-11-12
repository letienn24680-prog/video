/**
 * Fetch-based SSE Client for AI Video Studio
 * Uses fetch() + ReadableStream + TextDecoder so Authorization: Bearer <token>
 * headers can be sent cleanly (unlike native browser EventSource).
 */

import { getAccessToken } from "./auth.js";
import { store } from "./state.js";

const STOP_EVENTS = new Set(["awaiting_review", "completed", "cancelled", "error"]);

export class ProjectSSEClient {
  /**
   * @param {string} projectId
   * @param {object} [callbacks]
   * @param {function} [callbacks.onStatus]
   * @param {function} [callbacks.onStageStarted]
   * @param {function} [callbacks.onProgress]
   * @param {function} [callbacks.onLog]
   * @param {function} [callbacks.onAwaitingReview]
   * @param {function} [callbacks.onCompleted]
   * @param {function} [callbacks.onError]
   * @param {function} [callbacks.onCancelled]
   * @param {function} [callbacks.onEvent]
   * @param {function} [callbacks.onConnectionChange]
   * @param {object} [options]
   */
  constructor(projectId, callbacks = {}, options = {}) {
    if (!projectId) {
      throw new Error("ProjectSSEClient requires a valid projectId");
    }
    this.projectId = String(projectId);
    this.callbacks = callbacks;
    this.maxRetries = options.maxRetries ?? 8;
    this.baseDelayMs = options.baseDelayMs ?? 1200;
    this.maxDelayMs = options.maxDelayMs ?? 15000;

    this._abortController = null;
    this._reconnectTimer = null;
    this._closed = false;
    this._stopReceived = false;
    this._retryCount = 0;
    this._lastEventId = null;
  }

  /**
   * Open the SSE stream (alias start() also provided).
   */
  connect() {
    if (this._closed) {
      this._closed = false;
    }
    this._stopReceived = false;
    this._clearReconnectTimer();
    this._openStream();
    return this;
  }

  start() {
    return this.connect();
  }

  /**
   * Permanently close the stream and cancel any pending reconnect attempts.
   */
  close() {
    this._closed = true;
    this._clearReconnectTimer();
    if (this._abortController) {
      try {
        this._abortController.abort();
      } catch {
        // Ignore abort errors
      }
      this._abortController = null;
    }
    this._setConnected(false);
  }

  _clearReconnectTimer() {
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }

  _setConnected(connected) {
    store.setState({ sseConnected: Boolean(connected) });
    this.callbacks.onConnectionChange?.(Boolean(connected));
  }

  async _openStream() {
    if (this._closed || this._stopReceived) return;

    this._abortController = new AbortController();
    const headers = new Headers({
      Accept: "text/event-stream",
      "Cache-Control": "no-cache",
    });

    const token = getAccessToken();
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    if (this._lastEventId) {
      headers.set("Last-Event-ID", this._lastEventId);
    }

    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(this.projectId)}/events`,
        {
          method: "GET",
          headers,
          signal: this._abortController.signal,
        }
      );

      if (!response.ok) {
        let message = `SSE connection failed (${response.status})`;
        try {
          const errJson = await response.json();
          message = errJson?.error?.message || errJson?.detail || message;
        } catch {
          // Ignore non-JSON body
        }

        const fatal = response.status === 401 || response.status === 403 || response.status === 404;
        this._setConnected(false);
        this.callbacks.onError?.({
          project_id: this.projectId,
          status: response.status,
          message,
          fatal,
        });
        if (!fatal) {
          this._scheduleReconnect();
        }
        return;
      }

      if (!response.body) {
        throw new Error("ReadableStream not supported on SSE response body");
      }

      this._retryCount = 0;
      this._setConnected(true);

      await this._consumeStream(response.body);

      this._setConnected(false);
      if (!this._closed && !this._stopReceived) {
        this._scheduleReconnect();
      }
    } catch (err) {
      if (err?.name === "AbortError" || this._closed) {
        this._setConnected(false);
        return;
      }

      this._setConnected(false);
      this.callbacks.onError?.({
        project_id: this.projectId,
        message: err?.message || "Mất kết nối luồng SSE",
        retryable: true,
      });

      if (!this._stopReceived) {
        this._scheduleReconnect();
      }
    }
  }

  async _consumeStream(readableStream) {
    const reader = readableStream.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    try {
      while (!this._closed && !this._stopReceived) {
        const { value, done } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");

        let boundaryIdx;
        while ((boundaryIdx = buffer.indexOf("\n\n")) !== -1) {
          const rawFrame = buffer.slice(0, boundaryIdx);
          buffer = buffer.slice(boundaryIdx + 2);
          if (rawFrame.trim()) {
            this._processFrame(rawFrame);
          }
        }
      }

      const trailing = decoder.decode();
      if (trailing) {
        buffer += trailing.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      }
      if (buffer.trim() && !this._closed && !this._stopReceived) {
        this._processFrame(buffer);
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // Ignore
      }
    }
  }

  _processFrame(rawFrame) {
    let eventName = "message";
    let eventId = null;
    const dataLines = [];

    const lines = rawFrame.split("\n");
    for (const line of lines) {
      if (!line || line.startsWith(":")) {
        continue;
      }
      const colonIdx = line.indexOf(":");
      const field = colonIdx === -1 ? line : line.slice(0, colonIdx);
      let val = colonIdx === -1 ? "" : line.slice(colonIdx + 1);
      if (val.startsWith(" ")) {
        val = val.slice(1);
      }

      if (field === "event") {
        eventName = val || "message";
      } else if (field === "id") {
        eventId = val;
        this._lastEventId = val;
      } else if (field === "data") {
        dataLines.push(val);
      }
    }

    if (dataLines.length === 0) {
      return;
    }

    const rawData = dataLines.join("\n");
    let parsedData = rawData;
    try {
      parsedData = JSON.parse(rawData);
    } catch {
      // Keep raw string if payload isn't JSON
    }

    this._dispatchEvent(eventName, parsedData, { id: eventId, event: eventName });
  }

  _dispatchEvent(eventName, data, meta) {
    if (eventName === "stage_started" && data?.stage) {
      store.setState({ sseStage: data.stage });
    } else if (eventName === "status" && data?.current_stage) {
      store.setState({ sseStage: data.current_stage });
    } else if (eventName === "progress" && data?.stage) {
      store.setState({ sseStage: data.stage });
    }

    this.callbacks.onEvent?.(eventName, data, meta);

    switch (eventName) {
      case "status":
        this.callbacks.onStatus?.(data, meta);
        break;
      case "stage_started":
        this.callbacks.onStageStarted?.(data, meta);
        break;
      case "progress":
        this.callbacks.onProgress?.(data, meta);
        break;
      case "log":
        this.callbacks.onLog?.(data, meta);
        break;
      case "awaiting_review":
        this._stopReceived = true;
        this.callbacks.onAwaitingReview?.(data, meta);
        break;
      case "completed":
        this._stopReceived = true;
        this.callbacks.onCompleted?.(data, meta);
        break;
      case "cancelled":
        this._stopReceived = true;
        this.callbacks.onCancelled?.(data, meta);
        break;
      case "error":
        this._stopReceived = true;
        this.callbacks.onError?.(data, meta);
        break;
      default:
        break;
    }

    if (STOP_EVENTS.has(eventName)) {
      this._clearReconnectTimer();
      this._setConnected(false);
    }
  }

  _scheduleReconnect() {
    if (this._closed || this._stopReceived) return;
    if (this._retryCount >= this.maxRetries) {
      this.callbacks.onError?.({
        project_id: this.projectId,
        message: "Đã vượt quá số lần thử kết nối lại SSE.",
        fatal: true,
      });
      return;
    }

    const delay = Math.min(
      this.maxDelayMs,
      Math.round(this.baseDelayMs * Math.pow(1.6, this._retryCount))
    );
    this._retryCount += 1;

    this._clearReconnectTimer();
    this._reconnectTimer = setTimeout(() => {
      this._openStream();
    }, delay);
  }
}
