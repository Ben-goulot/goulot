// API client. The CSRF token and the vault token live in memory only: never in localStorage,
// sessionStorage, IndexedDB or a cookie (docs/SECURITY.md).

let csrfToken: string | null = null;
let vaultToken: string | null = null;

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export function setVaultToken(token: string | null): void {
  vaultToken = token;
}

export function hasVaultToken(): boolean {
  return vaultToken !== null;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  if (vaultToken && (path.startsWith("/api/private/") || path.startsWith("/api/vault/"))) {
    headers["X-Vault-Token"] = vaultToken;
  }
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? (method === "GET" ? undefined : "{}") : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
  });
  const data = (await res.json().catch(() => ({}))) as { detail?: string };
  if (!res.ok) {
    if (res.status === 403 && path.startsWith("/api/private/")) vaultToken = null;
    throw new ApiError(res.status, data.detail ?? "Erreur.");
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
};

export async function exchange(token: string): Promise<void> {
  const res = await fetch("/auth/exchange", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
    credentials: "same-origin",
  });
  if (!res.ok) {
    const data = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new ApiError(res.status, data.detail ?? "Lien refusé.");
  }
}

export async function loadSession(): Promise<void> {
  const data = await api.get<{ csrf_token: string }>("/api/session");
  csrfToken = data.csrf_token;
}

export type VaultStatus = {
  configured: boolean;
  recovery_confirmed: boolean;
  passkeys: number;
  unlocked: boolean;
  expires_in: number;
  blocked_for: number;
  lock_minutes: number;
};
