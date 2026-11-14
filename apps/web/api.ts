import { authClient } from './auth';

export type Profile = { id: string; email: string; role: 'MEMBER' | 'ORGANIZER' };
export type Team = { id: string; name: string; hackathon_id: string; captain_id: string };
const apiOrigin = import.meta.env.VITE_API_ORIGIN || 'http://127.0.0.1:8787';
let sessionGeneration = 0;
let requests = new AbortController();

export function clearApiSession() {
  sessionGeneration++;
  requests.abort();
  requests = new AbortController();
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!authClient) throw new Error('Authentication is not configured.');
  const generation = sessionGeneration;
  // Never retain an access token in localStorage, component state or logs.
  const session = await authClient.getSession();
  if (session.error || !session.data?.user) throw new Error('Please sign in again.');
  const token = session.data.session?.token;
  if (!token) throw new Error('Please sign in again.');
  if (generation !== sessionGeneration) throw new Error('Please sign in again.');
  const response = await fetch(`${apiOrigin}/api/v1${path}`, {
    ...init, signal: requests.signal, credentials: 'omit', headers: { ...init.headers, Authorization: `Bearer ${token}` },
  });
  if (response.status === 401) throw new Error('Please sign in again.');
  if (!response.ok) throw new Error('The request could not be completed. Please try again.');
  return response.json() as Promise<T>;
}
