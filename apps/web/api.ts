import { authClient } from './auth';

export type Profile = { id: string; email: string; role: 'MEMBER' | 'ORGANIZER' };
export type Team = { id: string; name: string; hackathon_id: string; captain_id: string };
const apiOrigin = import.meta.env.VITE_API_ORIGIN || '';
let sessionGeneration = 0;
let requests = new AbortController();

export function clearApiSession() {
  sessionGeneration++;
  requests.abort();
  requests = new AbortController();
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export async function apiResponse(path: string, init: RequestInit = {}): Promise<Response> {
  return authenticatedRequest(path, init, sessionGeneration, false);
}

async function authenticatedRequest(path: string, init: RequestInit, generation: number, refreshed: boolean): Promise<Response> {
  if (!authClient) throw new Error('Authentication is not configured.');
  // Never retain an access token in localStorage, component state or logs.
  const session = await authClient.getSession(refreshed ? {
    query: { disableCookieCache: true }, fetchOptions: { headers: { 'X-Force-Fetch': 'true' } },
  } : undefined);
  if (generation !== sessionGeneration) throw new Error('Please sign in again.');
  if (session.error) throw new ApiError(503, 'AUTH_UNAVAILABLE', 'Managed authentication is temporarily unavailable. Retry the same upload.');
  if (!session.data?.user) {
    if (!refreshed) return authenticatedRequest(path, init, generation, true);
    if (typeof window !== 'undefined') window.dispatchEvent(new Event('session-expired'));
    throw new Error('Please sign in again.');
  }
  const token = session.data.session?.token;
  if (!token) {
    if (!refreshed) return authenticatedRequest(path, init, generation, true);
    throw new ApiError(503, 'AUTH_UNAVAILABLE', 'Managed access token unavailable. Retry the same upload.');
  }
  if (generation !== sessionGeneration) throw new Error('Please sign in again.');
  const response = await fetch(`${apiOrigin}/api/v1${path}`, {
    ...init, signal: init.signal ? AbortSignal.any([requests.signal, init.signal]) : requests.signal,
    credentials: 'omit', headers: { ...init.headers, Authorization: `Bearer ${token}` },
  });
  if (response.status === 401) {
    if (!refreshed) return authenticatedRequest(path, init, generation, true);
    if (typeof window !== 'undefined') window.dispatchEvent(new Event('session-expired'));
    throw new ApiError(401, 'UNAUTHORIZED', 'Please sign in again.');
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const code = payload?.error?.code || 'REQUEST_FAILED';
    const recovery: Record<string, string> = {
      DEADLINE_CLOSED: 'New versions are closed. Recover an existing admitted upload instead.',
      GRACE_EXPIRED: 'Upload grace has expired. An already admitted completion intent may still recover.',
      IDEMPOTENCY_CONFLICT: 'This operation key belongs to different frozen metadata. Check version history before starting a separate version.',
      CAPTAIN_REQUIRED: 'Only the current team captain can perform this action. Your team data remains readable.',
      PARTS_MISMATCH: 'Storage parts do not match the proposed completion. Resume to reconcile the same upload.',
    };
    throw new ApiError(response.status, payload?.error?.code || 'REQUEST_FAILED',
      recovery[code] || (response.status === 429 ? 'Too many requests. Wait briefly, then retry the same operation.' :
        response.status === 413 ? 'The artifact exceeds the allowed file size.' :
        payload?.error?.message || `Request failed (${response.status}). Retry the same operation.`));
  }
  return response;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiResponse(path, init);
  return response.json() as Promise<T>;
}
