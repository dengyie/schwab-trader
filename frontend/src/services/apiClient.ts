/**
 * API Client
 * Single source of truth for backend authentication and request plumbing.
 *
 * The backend is protected by a single ADMIN_TOKEN (X-Auth-Token on REST,
 * AUTH frame on the websocket — a browser worker cannot set headers, and the
 * URL variant would leak the token into access logs). The token is kept in
 * localStorage so a reload does not force a re-login.
 */

const TOKEN_KEY = 'schwab-admin-token';
const API_BASE = '/api';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

export function hasToken(): boolean {
  const token = getToken();
  return !!token && token.length > 0;
}

export function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken();
  return token ? { ...extra, 'X-Auth-Token': token } : extra;
}

/** Requests that hang on a half-open connection would otherwise spin forever. */
const REQUEST_TIMEOUT_MS = 10_000;

/**
 * Fetch a backend endpoint with the auth header attached.
 * 401 responses clear the stored token and notify the app so the login gate
 * can take over; callers still receive a rejected promise.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = authHeaders((init.headers as Record<string, string>) || {});
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    signal: init.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });

  if (response.status === 401) {
    clearToken();
    window.dispatchEvent(new CustomEvent('schwab:unauthorized'));
    throw new ApiError(401, 'Unauthorized');
  }
  return response;
}

/** Same as apiFetch but throws on non-2xx and returns parsed JSON. */
export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    throw new ApiError(response.status, `HTTP ${response.status} for ${path}`);
  }
  return (await response.json()) as T;
}

/**
 * GET a small piece of reference data that every request needs (session state,
 * tradability). One in-flight request is shared by all callers, and the last
 * answer is replayed after a failure so a network blip cannot flip the UI to
 * "everything is broken".
 */
export function createPolledGetter<T>(
  path: string,
  ttlMs: number,
  onFailure: (error: unknown) => T | null
): () => Promise<T | null> {
  let inFlight: Promise<T | null> | null = null;
  let last: T | null = null;
  let lastAt = 0;

  return () => {
    if (last !== null && Date.now() - lastAt < ttlMs) return Promise.resolve(last);
    if (inFlight) return inFlight;

    inFlight = apiJson<T>(path)
      .then((data) => {
        last = data;
        lastAt = Date.now();
        return data;
      })
      .catch((error) => onFailure(error) ?? last)
      .finally(() => {
        inFlight = null;
      });

    return inFlight;
  };
}

/** Validate a candidate token against the backend and establish the
 * HttpOnly session cookie. The admin token is used ONCE here (as the
 * X-Auth-Token header of this call) and never stored — the cookie that the
 * response sets carries every later request, and a WS handshake rides it
 * same-origin automatically. */
export async function verifyToken(token: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'X-Auth-Token': token },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** Probe whether the current session (cookie) is still live. */
export async function probeSession(): Promise<boolean> {
  try {
    const response = await apiFetch('/auth/verify');
    return response.ok;
  } catch {
    return false;
  }
}

/** Revoke the server-side session and drop the cookie. */
export async function destroySession(): Promise<void> {
  try {
    await fetch(`${API_BASE}/auth/session`, { method: 'DELETE' });
  } catch {
    /* the cookie will simply expire server-side */
  }
}

export { API_BASE, TOKEN_KEY };