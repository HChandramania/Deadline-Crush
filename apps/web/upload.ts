import { api } from './api';

export const MAX_FILE = 104857600;
export type Part = { part_number: number; etag: string; size_bytes: number };
export type UploadState = { status: string; part_size_bytes: number; expected_parts: number;
  completed_parts: Part[]; missing_parts: number[]; recoverable: boolean; completion_pending: boolean;
  parts_source: string; deadline_at: string; grace_until: string };
export type Draft = { body: { title: string; description: string; links: string[]; filename: string;
  size_bytes: number; content_type: string }; fingerprint: string; admissionKey: string;
  session?: string; version?: string; keys: Record<string, string> };

export function validateFile(file: File) {
  if (!file.size || file.size > MAX_FILE) throw new Error('Select a file between 1 byte and 100 MiB.');
  if (!/\.(zip|pdf)$/i.test(file.name)) throw new Error('Select a ZIP archive or PDF document.');
}

// Bounded reads; identity covers every byte, not only filename/size/mtime.
export async function fileIdentity(file: File) {
  validateFile(file);
  const hashes: string[] = [];
  for (let start = 0; start < file.size; start += 8388608) {
    const digest = await crypto.subtle.digest('SHA-256', await file.slice(start, start + 8388608).arrayBuffer());
    hashes.push(Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join(''));
  }
  return hashes.join(':');
}

export function putPart(url: string, headers: Record<string, string>, blob: Blob, signal: AbortSignal,
                        progress: (bytes: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const fail = () => reject(new Error('Part upload interrupted. Retry to reconcile uploaded parts.'));
    xhr.open('PUT', url);
    xhr.timeout = 120000;
    Object.entries(headers).forEach(([key, value]) => xhr.setRequestHeader(key, value));
    xhr.upload.onprogress = event => progress(event.loaded);
    xhr.onload = () => {
      const etag = xhr.getResponseHeader('ETag');
      if (xhr.status >= 200 && xhr.status < 300 && etag) resolve(etag);
      else reject(new Error('R2 upload failed or ETag is not exposed by CORS. Retry this upload.'));
    };
    xhr.onerror = fail; xhr.ontimeout = fail; xhr.onabort = fail;
    xhr.onloadend = () => signal.removeEventListener('abort', abort);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) { reject(new DOMException('Cancelled', 'AbortError')); return; }
    xhr.send(blob);
  });
}

export async function runUpload(team: string, draft: Draft, file: File | null, signal: AbortSignal,
  save: () => void, report: (message: string, confirmed: number, sending?: number) => void) {
  const key = (action: string) => {
    if (!draft.keys[action]) { draft.keys[action] = crypto.randomUUID(); save(); }
    return draft.keys[action];
  };
  const post = <T,>(path: string, action: string, body?: unknown) => api<T>(`${path}?team_id=${team}`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key(action) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!draft.session) {
    report('Admitting immutable version', 0);
    const result = await api<{ upload_session_id: string; version_id: string }>(`/submissions/versions?team_id=${team}`, {
      method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'Idempotency-Key': draft.admissionKey },
      body: JSON.stringify(draft.body),
    });
    draft.session = result.upload_session_id; draft.version = result.version_id; save();
  }
  const base = `/uploads/${draft.session}`;
  let state = await api<UploadState>(`${base}?team_id=${team}`, { signal });
  if (state.status === 'INITIATING') state = await post<UploadState>(`${base}/initialize`, 'initialize');
  if (state.status === 'INITIATING') throw new Error('Upload initialization is recovering. Retry this admitted upload shortly.');
  if (state.status === 'COMPLETED') { report('Submission finalized. Validation may still be pending.', draft.body.size_bytes); return; }
  if (state.status === 'ACTIVE' && !state.recoverable) throw new Error('Upload grace has expired. No new parts can be sent.');
  if (!['ACTIVE', 'COMPLETING'].includes(state.status)) throw new Error(`Upload is ${state.status}. Check version history.`);
  let confirmed = state.completed_parts.reduce((n, p) => n + p.size_bytes, 0);
  report(state.completion_pending ? 'Recovery pending' : 'Uploading directly to R2', confirmed);
  if (state.missing_parts.length) {
    if (!file) throw new Error('Reselect the original file to resume missing parts.');
    if (file.name !== draft.body.filename || file.size !== draft.body.size_bytes || await fileIdentity(file) !== draft.fingerprint)
      throw new Error('This file does not match the admitted upload. Reselect the original unchanged file.');
    for (const n of state.missing_parts) {
      const blob = file.slice((n - 1) * state.part_size_bytes, n * state.part_size_bytes);
      let succeeded = false;
      for (let attempt = 0; attempt < 3; attempt++) {
        signal.throwIfAborted();
        report(attempt ? `Retrying part ${n}` : `Uploading part ${n} of ${state.expected_parts}`, confirmed);
        try {
          const signed = await post<{ parts: { url: string; headers: Record<string, string> }[] }>(`${base}/parts/sign`, `sign-${n}`, { part_numbers: [n] });
          await putPart(signed.parts[0].url, signed.parts[0].headers, blob, signal,
            bytes => report(`Uploading part ${n} of ${state.expected_parts}`, confirmed, bytes));
          succeeded = true; break;
        } catch (error) { if (signal.aborted || attempt === 2) throw error; }
      }
      if (succeeded) confirmed += blob.size;
      report('Part transferred', confirmed);
    }
  }
  // R2 listing/frozen server manifest is authoritative, including response-loss recovery.
  state = await api<UploadState>(`${base}?team_id=${team}`, { signal });
  if (state.status === 'COMPLETED') { report('Submission finalized. Validation pending.', draft.body.size_bytes); return; }
  if (state.missing_parts.length) throw new Error('Some parts are not confirmed by R2. Retry to resume.');
  report('Bytes transferred. Finalizing submission…', draft.body.size_bytes);
  const result = await post<{ status: string }>(`${base}/complete`, 'complete', {
    parts: [...state.completed_parts].sort((a, b) => a.part_number - b.part_number).map(({ part_number, etag }) => ({ part_number, etag })),
  });
  report(result.status === 'FINALIZED' ? 'Submission finalized. Validation pending.' : 'Completion intent admitted. Recovery pending.', draft.body.size_bytes);
}
