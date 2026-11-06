/**
 * API Client
 * Single source of truth for backend authentication and request plumbing.
 *
 * The backend is protected by a single ADMIN_TOKEN (X-Auth-Token on REST,
 * ?token= on the websocket). The token is kept in localStorage so a reload
 * does not force a re-login.
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

/**
 * Build the websocket URL with the auth token appended.
 * Worker-safe: a dedicated worker exposes `self.location` instead of window.
 */
export function getWsUrl(): string {
  const token = getToken() || '';
  const loc =
    (typeof window !== 'undefined' && window.location) ||
    (typeof self !== 'undefined' ? self.location : undefined);
  const host = loc && loc.host ? loc.host : '';
  const isSecure = loc ? loc.protocol === 'https:' : false;
  const protocol = isSecure ? 'wss:' : 'ws:';
  const basePath = host ? `${protocol}//${host}/ws` : 'ws://127.0.0.1:8000/ws';
  return token ? `${basePath}?token=${encodeURIComponent(token)}` : basePath;
}

/**
 * Fetch a backend endpoint with the auth header attached.
 * 401 responses clear the stored token and notify the app so the login gate
 * can take over; callers still receive a rejected promise.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = authHeaders((init.headers as Record<string, string>) || {});
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });

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

/** Validate a candidate token against the backend before storing it. */
export async function verifyToken(token: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/auth/verify`, {
      headers: { 'X-Auth-Token': token },
    });
    return response.ok;
  } catch {
    return false;
  }
}

export { API_BASE, TOKEN_KEY };