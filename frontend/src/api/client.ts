/**
 * API client for the Governix backend.
 *
 * - Unwraps the {success, data, error, request_id} envelope.
 * - Keeps the access token in memory; the refresh token in sessionStorage
 *   (cleared when the tab closes). Refreshes once on 401 TOKEN_EXPIRED.
 * - Errors become ApiError with the backend's stable `code`.
 */

const BASE = "/api";
const REFRESH_KEY = "governix.refresh";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
    public requestId?: string,
  ) {
    super(message);
  }
}

let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;
let onSessionExpired: (() => void) | null = null;

export function setSessionExpiredHandler(handler: () => void) {
  onSessionExpired = handler;
}

export function setTokens(access: string | null, refresh?: string | null) {
  accessToken = access;
  if (refresh === null) sessionStorage.removeItem(REFRESH_KEY);
  else if (refresh) sessionStorage.setItem(REFRESH_KEY, refresh);
}

export function hasRefreshToken() {
  return Boolean(sessionStorage.getItem(REFRESH_KEY));
}

export function getRefreshToken() {
  return sessionStorage.getItem(REFRESH_KEY);
}

async function refresh(): Promise<boolean> {
  const token = getRefreshToken();
  if (!token) return false;
  refreshing ??= (async () => {
    try {
      const response = await fetch(`${BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: token }),
      });
      if (!response.ok) return false;
      const body = await response.json();
      setTokens(body.data.access_token, body.data.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      setTimeout(() => (refreshing = null), 0);
    }
  })();
  return refreshing;
}

async function raw(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (response.status === 401 && retry && getRefreshToken()) {
    if (await refresh()) return raw(path, init, false);
    setTokens(null, null);
    onSessionExpired?.();
  }
  return response;
}

async function unwrap<T>(response: Response): Promise<T> {
  let body: any = null;
  try {
    body = await response.json();
  } catch {
    throw new ApiError(response.status, "BAD_RESPONSE", "The server returned an unexpected response.");
  }
  if (!response.ok || body?.success === false) {
    const error = body?.error ?? {};
    throw new ApiError(
      response.status,
      error.code ?? "HTTP_ERROR",
      error.message ?? "Request failed.",
      error.details,
      body?.request_id,
    );
  }
  return body.data as T;
}

export type StreamHandler = (event: string, data: unknown) => void;

/**
 * POST that answers with server-sent events. Calls `onEvent` for each event as
 * it arrives and resolves when the stream ends. Non-2xx responses throw the
 * usual ApiError (rate limit, expired session, ...).
 */
async function stream(path: string, data: unknown, onEvent: StreamHandler, signal?: AbortSignal): Promise<void> {
  const response = await raw(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(data),
    signal,
  });
  if (!response.ok || !response.body) {
    await unwrap(response); // throws the backend's error
    throw new ApiError(response.status, "BAD_RESPONSE", "The server returned an unexpected response.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = (block: string) => {
    let event = "message";
    const lines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
    }
    if (lines.length) onEvent(event, JSON.parse(lines.join("\n")));
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      dispatch(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
    }
  }
  if (buffer.trim()) dispatch(buffer);
}

export interface UploadHandle<T> {
  promise: Promise<T>;
  abort: () => void;
}

/**
 * Multipart upload that reports progress (fetch cannot) and can be cancelled.
 * Refreshes the access token once on 401, like every other call.
 */
function uploadWithProgress<T>(path: string, form: FormData, onProgress: (fraction: number) => void): UploadHandle<T> {
  let xhr: XMLHttpRequest | null = null;
  let aborted = false;
  const send = (retry: boolean): Promise<T> =>
    new Promise<T>((resolve, reject) => {
      const request = new XMLHttpRequest();
      xhr = request;
      request.open("POST", `${BASE}${path}`);
      if (accessToken) request.setRequestHeader("Authorization", `Bearer ${accessToken}`);
      request.upload.onprogress = (event) => event.lengthComputable && onProgress(event.loaded / event.total);
      request.onabort = () => reject(new ApiError(0, "ABORTED", "Upload cancelled."));
      request.onerror = () => reject(new ApiError(0, "NETWORK_ERROR", "The upload was interrupted."));
      request.onload = async () => {
        if (request.status === 401 && retry && getRefreshToken() && (await refresh())) {
          if (aborted) return reject(new ApiError(0, "ABORTED", "Upload cancelled."));
          return send(false).then(resolve, reject);
        }
        unwrap<T>(new Response(request.responseText, { status: request.status })).then(resolve, reject);
      };
      request.send(form);
    });
  return {
    promise: send(true),
    abort: () => {
      aborted = true;
      xhr?.abort();
    },
  };
}

export const api = {
  stream,
  get: <T>(path: string) => raw(path).then(unwrap<T>),
  post: <T>(path: string, data?: unknown) =>
    raw(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
    }).then(unwrap<T>),
  patch: <T>(path: string, data: unknown) =>
    raw(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).then(unwrap<T>),
  put: <T>(path: string, data: unknown) =>
    raw(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).then(unwrap<T>),
  delete: <T>(path: string) => raw(path, { method: "DELETE" }).then(unwrap<T>),
  upload: <T>(path: string, form: FormData) => raw(path, { method: "POST", body: form }).then(unwrap<T>),
  uploadWithProgress,
  /** Authenticated binary download (e.g. page images) as an object URL. */
  blobUrl: async (path: string) => {
    const response = await raw(path);
    if (!response.ok) throw new ApiError(response.status, "HTTP_ERROR", "Could not load file.");
    return { url: URL.createObjectURL(await response.blob()), headers: response.headers };
  },
};

export function qs(params: Record<string, unknown>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)));
    else search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
