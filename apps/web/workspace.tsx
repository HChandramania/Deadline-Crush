import React, { useEffect, useRef, useState } from 'react';
import { api, apiResponse, type Profile, type Team } from './api';
import { usePoll } from './poll';
import { fileIdentity, runUpload, validateFile, type Draft, type UploadState } from './upload';

export type Version = { id: string; version_no: number; title: string; description: string; links: string[];
  filename: string; size_bytes: number; lifecycle: string; validation_status: string; admitted_at: string; finalized_at: string | null };
type History = { versions: Version[]; next_before_version_no: number | null };
type EventInfo = { id: string; name: string; deadline_at: string; grace_until: string; max_file_bytes: number };
type Detail = { job_status: string | null; attempts: number; findings: string[]; completed_at: string | null;
  previews: string[]; upload_session_id: string | null };
const date = (value: string | null) => value ? new Date(value).toLocaleString() : '—';
const message = (error: unknown) => error instanceof Error ? error.message : 'Temporarily unavailable. Retry the same operation.';
export function Badge({ value }: { value: string }) { return <span className={`badge ${value.toLowerCase()}`}>{value.replaceAll('_', ' ')}</span>; }
function versionStatus(version: Version) {
  return version.lifecycle === 'FINALIZED' ? version.validation_status : version.lifecycle === 'CREATED' ? 'UPLOADING' : version.lifecycle;
}

function Deadline({ event, offset }: { event?: EventInfo; offset: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  if (!event) return <p role="status">Deadline unavailable. The server still enforces admission.</p>;
  const remaining = new Date(event.deadline_at).getTime() - now - offset;
  const grace = new Date(event.grace_until).getTime() - now - offset;
  const duration = Math.max(0, remaining > 0 ? remaining : grace);
  return <aside className="deadline"><div><span className="eyebrow">{event.name}</span><h2>{remaining >= 0 ? 'Submission window open' : grace >= 0 ? 'Authorized upload grace active' : 'Grace expired'}</h2>
    <p>Deadline {date(event.deadline_at)} · Grace until {date(event.grace_until)}</p></div>
    <div className="clock">{Math.floor(duration / 3600000).toString().padStart(2, '0')}:{Math.floor(duration / 60000) % 60 < 10 ? '0' : ''}{Math.floor(duration / 60000) % 60}:{(Math.floor(duration / 1000) % 60).toString().padStart(2, '0')}<small>Informational · server decides eligibility</small></div>
    {remaining < 0 && <p className="wide">No new versions. Grace only permits previously admitted upload work. An admitted completion intent may recover after grace.</p>}</aside>;
}

export function VersionDetails({ version }: { version: Version }) {
  const result = usePoll<Detail>(version.id, signal => api(`/submissions/${version.id}/details`, { signal }), 7000);
  const [preview, setPreview] = useState<{ url: string; kind: string } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => { controller.current?.abort(); }, []);
  useEffect(() => { return () => { if (preview) URL.revokeObjectURL(preview.url); }; }, [preview]);
  async function open(kind?: string) {
    setBusy(true); setError(''); controller.current?.abort(); controller.current = new AbortController();
    try {
      if (kind) {
        const response = await apiResponse(`/submissions/${version.id}/preview/${kind}`, { signal: controller.current.signal });
        const blob = await response.blob();
        if (!controller.current.signal.aborted) setPreview({ kind, url: URL.createObjectURL(blob) });
      } else {
        const result = await api<{ url: string }>(`/submissions/${version.id}/download-url`, { signal: controller.current.signal });
        if (!controller.current.signal.aborted) { const link = document.createElement('a'); link.href = result.url; link.rel = 'noreferrer'; link.click(); }
      }
    } catch (cause) { if (!controller.current?.signal.aborted) setError(message(cause)); }
    finally { setBusy(false); }
  }
  return <section className="card detail"><div className="section-head"><h3>Version {version.version_no} · {version.title}</h3><Badge value={versionStatus(version)} /></div>
    <p>{version.description || 'No description provided.'}</p><p className="muted">{version.filename} · {(version.size_bytes / 1048576).toFixed(2)} MiB · Admitted {date(version.admitted_at)}</p>
    <p>Finalized {date(version.finalized_at)} · Validation completed {date(result.data?.completed_at ?? null)}</p>
    {version.links.map(link => <p key={link}>{/^https:\/\//i.test(link) ? <a href={link} target="_blank" rel="noreferrer">{link}</a> : link}</p>)}
    {result.error && <p role="alert">Status unavailable: {result.error}</p>}
    {!result.data && !result.error && <p role="status">Loading validation details…</p>}
    {result.data?.job_status === 'FAILED' && <p role="status">Processing needs operator attention. This is an infrastructure failure, not an invalid submission.</p>}
    {result.data && ['PENDING', 'RUNNING'].includes(result.data.job_status ?? '') && <p role="status">Validation {result.data.job_status?.toLowerCase()} · Attempt {result.data.attempts}. Updates appear automatically.</p>}
    {!!result.data?.findings.length && <><h4>What needs attention</h4><ul>{result.data.findings.map((finding, i) => <li key={i}>{finding.replaceAll('_', ' ')}. Correct the content and admit a new version while the submission window is open.</li>)}</ul></>}
    <div className="actions">{version.lifecycle === 'FINALIZED' && <button disabled={busy} onClick={() => void open()}>Download artifact</button>}
      {result.data?.previews.map(kind => <button key={kind} disabled={busy} onClick={() => void open(kind)}>{kind === 'ZIP_PREVIEW' ? 'README & file inventory' : 'PDF first page'}</button>)}</div>
    {version.lifecycle === 'FINALIZED' && !result.data?.previews.length && <p className="muted">Preview not available yet, or content could not be previewed.</p>}
    {error && <p role="alert">{error}</p>}
    {preview && (preview.kind === 'PDF_PREVIEW' ? <img className="pdf-preview" src={preview.url} alt="First page of submitted PDF" /> : <iframe className="preview" sandbox="" referrerPolicy="no-referrer" title="Untrusted archive README and inventory" src={preview.url} />)}
  </section>;
}

function UploadForm({ team, profile, recoverVersion, onRecoverHandled }: { team: Team; profile: Profile; recoverVersion: string | null; onRecoverHandled: () => void }) {
  const storageKey = `deadline-crush:${profile.id}:${team.id}`;
  const [draft, setDraft] = useState<Draft | null>(() => {
    try { return JSON.parse(localStorage.getItem(storageKey) || 'null'); } catch { return null; }
  });
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [finalized, setFinalized] = useState(false);
  const [error, setError] = useState('');
  const [progress, setProgress] = useState({ text: '', bytes: 0, sending: 0 });
  const operation = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  useEffect(() => () => operation.current?.abort(), []);
  useEffect(() => {
    if (inFlight.current) return;
    setFinalized(false);
    if (!draft?.session) return;
    const controller = new AbortController();
    api<UploadState>(`/uploads/${draft.session}?team_id=${team.id}`, { signal: controller.signal }).then(state => {
      if (controller.signal.aborted) return;
      const done = state.status === 'COMPLETED';
      setFinalized(done);
      setProgress({ text: done ? 'Submission finalized. Check validation status in version history.' :
        state.completion_pending ? 'Recovery pending. Retry the same completion intent.' : 'Upload restored from authoritative storage state.',
        bytes: done ? draft.body.size_bytes : state.completed_parts.reduce((n, part) => n + part.size_bytes, 0), sending: 0 });
    }).catch(cause => { if (!controller.signal.aborted) setError(message(cause)); });
    return () => controller.abort();
  }, [draft?.session, team.id]);
  useEffect(() => {
    if (recoverVersion && draft?.version !== recoverVersion) {
      try {
        const saved = JSON.parse(localStorage.getItem(`${storageKey}:version:${recoverVersion}`) || 'null') as Draft | null;
        if (saved?.version === recoverVersion) { localStorage.setItem(storageKey, JSON.stringify(saved)); setDraft(saved); setFile(null); setError(''); setProgress({ text: 'Reselect original file if parts are missing.', bytes: 0, sending: 0 }); }
        else setError('This browser has no matching file fingerprint for that version. Recover using the original browser upload record; do not substitute a different file.');
      } catch { setError('The saved upload record is unavailable. Use the original browser record.'); }
    }
    if (recoverVersion) onRecoverHandled();
  }, [recoverVersion, draft?.version]);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError('');
    const controller = new AbortController(); operation.current = controller;
    const fields = new FormData(event.currentTarget);
    let current = draft;
    try {
      if (!current) {
        if (!file) throw new Error('Select your ZIP or PDF artifact.');
        validateFile(file); setProgress({ text: 'Checking file identity…', bytes: 0, sending: 0 });
        current = { body: { title: String(fields.get('title')).trim(), description: String(fields.get('description')).trim(),
          links: String(fields.get('links')).split(/\r?\n/).map(s => s.trim()).filter(Boolean), filename: file.name,
          size_bytes: file.size, content_type: /\.pdf$/i.test(file.name) ? 'application/pdf' : 'application/zip' },
          fingerprint: await fileIdentity(file), admissionKey: crypto.randomUUID(), keys: {} };
        if (current.body.links.length > 10 || current.body.links.some(link => !/^https:\/\//i.test(link) || link.length > 2048))
          throw new Error('Provide at most 10 HTTPS links, each no longer than 2,048 characters.');
        controller.signal.throwIfAborted();
        setDraft(current);
      }
      const record = current;
      const save = () => {
        localStorage.setItem(storageKey, JSON.stringify(record));
        if (record.version) localStorage.setItem(`${storageKey}:version:${record.version}`, JSON.stringify(record));
      };
      save();
      await runUpload(team.id, record, file, controller.signal, save,
        (text, bytes, sending = 0) => { if (!controller.signal.aborted) { setProgress({ text, bytes, sending }); if (text.startsWith('Submission finalized')) setFinalized(true); } });
      setDraft({ ...record });
    } catch (cause) { if (!controller.signal.aborted) setError(message(cause)); }
    finally { inFlight.current = false; if (!controller.signal.aborted) setBusy(false); }
  }
  const size = draft?.body.size_bytes || file?.size || 0;
  return <section className="card"><span className="eyebrow">Captain workspace</span><h2>{finalized ? 'Upload finalized' : draft?.session ? 'Recover admitted upload' : draft ? 'Recover admission request' : 'Create a submission version'}</h2>
    <p>Metadata freezes at admission. A newer unfinished upload never replaces your current finalized version.</p>
    {!finalized && <form onSubmit={submit}><fieldset disabled={busy}>
      <label>Project title<input name="title" required maxLength={200} defaultValue={draft?.body.title} readOnly={!!draft} /></label>
      <label>Description<textarea name="description" maxLength={20000} defaultValue={draft?.body.description} readOnly={!!draft} rows={3} /></label>
      <label>Project links <span className="muted">one HTTPS URL per line, up to 10</span><textarea name="links" rows={2} defaultValue={draft?.body.links.join('\n')} readOnly={!!draft} /></label>
      <label>{draft ? `Reselect original file: ${draft.body.filename}` : 'Artifact · ZIP or PDF · maximum 100 MiB'}<input type="file" accept=".zip,.pdf" onChange={e => { setFile(e.target.files?.[0] || null); setError(''); }} /></label>
      <p className="muted">Files transfer directly to private storage in 8 MiB parts. After reopening this page, reselect the original unchanged file.</p>
      <button className="primary" disabled={busy} type="submit">{busy ? 'Working…' : draft ? 'Resume / recover same upload' : 'Admit version & upload'}</button>
    </fieldset></form>}
    {size > 0 && <div className="upload-progress" role="status" aria-live="polite"><p>{progress.text || 'Ready to upload'} <strong>{Math.floor(progress.bytes / size * 100)}%</strong></p>
      <progress max={size} value={progress.bytes} aria-label="Successfully transferred bytes" /><p>{progress.bytes.toLocaleString()} / {size.toLocaleString()} bytes transferred{progress.sending > 0 ? ` · ${progress.sending.toLocaleString()} bytes in current part` : ''}</p>
      <small>100% transferred is not finalization or acceptance.</small></div>}
    {error && <p role="alert">{error}</p>}
    {draft && !busy && <button onClick={() => { if (draft.version) localStorage.setItem(`${storageKey}:version:${draft.version}`, JSON.stringify(draft)); localStorage.removeItem(storageKey); setDraft(null); setFinalized(false); setProgress({ text: '', bytes: 0, sending: 0 }); }}>Start a separate version</button>}
  </section>;
}

export function Workspace({ profile, team, event, offset }: { profile: Profile; team: Team; event?: EventInfo; offset: number }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [before, setBefore] = useState<number | null>(null);
  const [recover, setRecover] = useState<string | null>(null);
  const result = usePoll(team.id + ':' + before, async signal => {
    const query = `?team_id=${team.id}`;
    const [history, current, members] = await Promise.all([
      api<History>(`/submissions/versions${query}${before ? `&before_version_no=${before}` : ''}`, { signal }),
      api<{ selected_version_id: string | null; version_no: number | null; validation_status: string | null }>(`/submissions/current${query}`, { signal }),
      api<{ members: { user_id: string; role: string }[] }>(`/teams/${team.id}/members`, { signal }),
    ]);
    return { history, current, members };
  });
  const captain = team.captain_id === profile.id && result.data?.members.members.some(m => m.user_id === profile.id && m.role === 'CAPTAIN');
  const version = result.data?.history.versions.find(v => v.id === selected);
  return <><Deadline event={event} offset={offset} />
    <div className="section-head"><div><span className="eyebrow">Team workspace</span><h1>{team.name}</h1></div><Badge value={captain ? 'CAPTAIN' : 'MEMBER'} /></div>
    {result.error && <p role="alert">Live data unavailable: {result.error} · Retrying with backoff.</p>}
    {!result.data && <p role="status">Loading team workspace…</p>}
    <div className="workspace-grid"><div>{captain ? <UploadForm team={team} profile={profile} recoverVersion={recover} onRecoverHandled={() => setRecover(null)} /> : <section className="card"><h2>Team member access</h2><p>You can view versions, validation findings and previews. Only the current captain can upload or finalize.</p></section>}
      <section className="card"><h2>Team members</h2><ul>{result.data?.members.members.map(m => <li key={m.user_id}>{m.user_id === profile.id ? 'You' : m.user_id} <Badge value={m.role} />{m.user_id === team.captain_id ? ' · team captain' : ''}</li>)}</ul></section></div>
      <div><section className="card current"><span className="eyebrow">Authoritative current version</span><h2>{result.data?.current.version_no ? `Version ${result.data.current.version_no}` : 'No finalized submission yet'}</h2>
        {result.data?.current.validation_status && <Badge value={result.data.current.validation_status} />}<p>The highest eligible finalized version wins, including NEEDS FIX. There is no automatic fallback.</p></section>
        <section className="card"><div className="section-head"><h2>Version history</h2><span className="muted">Updates every 5s</span></div>
          {result.data?.history.versions.length === 0 && <p>No versions yet. Your captain can admit the first version before the deadline.</p>}
          <ul className="versions">{result.data?.history.versions.map(v => <li key={v.id}><button onClick={() => setSelected(v.id)}><strong>v{v.version_no} · {v.title}</strong><Badge value={versionStatus(v)} /><small>{v.lifecycle} · {date(v.admitted_at)}{v.id === result.data?.current.selected_version_id ? ' · CURRENT' : ''}</small></button>
            {captain && ['CREATED', 'UPLOADING', 'COMPLETING'].includes(v.lifecycle) && <button onClick={() => setRecover(v.id)}>Recover this version</button>}</li>)}</ul>
          <div className="actions">{before && <button onClick={() => { setBefore(null); setSelected(null); }}>Latest versions</button>}{result.data?.history.next_before_version_no && <button onClick={() => { setBefore(result.data!.history.next_before_version_no); setSelected(null); }}>Older versions</button>}</div>
        </section></div></div>
    {version && <VersionDetails key={version.id} version={version} />}
  </>;
}

type LedgerRow = { team_id: string; team_name: string; version_id: string | null; version_no: number | null;
  title: string | null; validation_status: string | null; finalized_at: string | null; latest_version_no: number | null };
export function Organizer() {
  const [search, setSearch] = useState(''); const [filter, setFilter] = useState(''); const [after, setAfter] = useState<string | null>(null);
  const [team, setTeam] = useState<string | null>(null); const [error, setError] = useState(''); const [exporting, setExporting] = useState(false);
  const result = usePoll(`organizer:${search}:${filter}:${after}`, async signal => {
    const [metrics, ledger] = await Promise.all([
      api<{ teams: number; total_versions: number; finalized_versions: number; checking_versions: number; accepted_versions: number; failed_checks: number; queue_pending: number; validation: Record<string, number | null>; recent_activity: { minute: string; finalized: number }[] }>('/organizer/metrics', { signal }),
      api<{ rows: LedgerRow[]; next_after: string | null }>(`/organizer/submissions?search=${encodeURIComponent(search)}&status=${filter}${after ? `&after=${after}` : ''}`, { signal }),
    ]); return { metrics, ledger, at: new Date().toLocaleTimeString() };
  }, 10000);
  async function download() {
    setExporting(true); setError('');
    try { const response = await apiResponse('/organizer/export.csv'); const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = 'deadline-crush.csv'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) { setError(message(cause)); } finally { setExporting(false); }
  }
  const metrics = result.data?.metrics;
  return <><div className="section-head"><div><span className="eyebrow">Organizer operations</span><h1>Submission monitor</h1><p>Polling every 10 seconds · Last updated {result.data?.at ?? '—'}</p></div><button disabled={exporting} onClick={() => void download()}>{exporting ? 'Exporting…' : 'Export all teams · CSV'}</button></div>
    {result.error && <p role="alert">{result.error} · Retrying with backoff.</p>}{error && <p role="alert">{error}</p>}
    {!metrics && !result.error && <p role="status">Loading organizer data…</p>}
    {metrics && <><div className="metrics">{Object.entries({ Teams: metrics.teams, Versions: metrics.total_versions, Finalized: metrics.finalized_versions, Checking: metrics.checking_versions, Accepted: metrics.accepted_versions, 'Needs fix': metrics.failed_checks }).map(([label, count]) => <article className="card" key={label}><span>{label}</span><strong>{count}</strong></article>)}</div>
      <p className="card">Pending jobs: {metrics.queue_pending} · Active leases: {metrics.validation.active_leases ?? 0} · Retrying: {metrics.validation.retry_depth ?? 0} · Failed processing: {metrics.validation.failed ?? 0}. Counts cover all versions; ledger shows each team’s winning version.</p>
      <details className="card"><summary>Recent finalization activity · past hour</summary>{metrics.recent_activity?.length ? <ul>{metrics.recent_activity.map(item => <li key={item.minute}>{date(item.minute)} · {item.finalized} finalized</li>)}</ul> : <p>No finalizations in the past hour.</p>}</details></>}
    <section className="card"><form className="filters" onSubmit={e => { e.preventDefault(); const fields = new FormData(e.currentTarget); setSearch(String(fields.get('search'))); setAfter(null); }}><label>Search team<input name="search" maxLength={100} placeholder="Team name" /></label><label>Current status<select value={filter} onChange={e => { setFilter(e.target.value); setAfter(null); }}><option value="">All statuses</option>{['CHECKING', 'ACCEPTED', 'NEEDS_FIX'].map(s => <option key={s}>{s}</option>)}</select></label><button>Search</button></form>
      <div className="table-wrap"><table><caption>Team submission ledger</caption><thead><tr><th>Team</th><th>Current / latest attempt</th><th>Status</th><th>Finalized</th><th>Details</th></tr></thead><tbody>{result.data?.ledger.rows.map(row => <tr key={row.team_id}><td>{row.team_name}</td><td>{row.version_no ?? '—'} / {row.latest_version_no ?? '—'}</td><td>{row.validation_status ? <Badge value={row.validation_status} /> : 'No finalized version'}</td><td>{date(row.finalized_at)}</td><td><button onClick={() => setTeam(row.team_id)}>Inspect team</button></td></tr>)}</tbody></table></div>
      {result.data?.ledger.rows.length === 0 && <p>No matching teams.</p>}<div className="actions">{after && <button onClick={() => setAfter(null)}>First page</button>}{result.data?.ledger.next_after && <button onClick={() => setAfter(result.data!.ledger.next_after)}>Next page</button>}</div></section>
    {team && <OrganizerTeam key={team} team={team} />}
  </>;
}
function OrganizerTeam({ team }: { team: string }) {
  const [before, setBefore] = useState<number | null>(null); const [selected, setSelected] = useState<string | null>(null);
  const result = usePoll(`${team}:${before}`, signal => api<History>(`/organizer/teams/${team}/versions${before ? `?before=${before}` : ''}`, { signal }), 10000);
  const version = result.data?.versions.find(v => v.id === selected);
  return <section className="card"><h2>Team version drilldown</h2>{result.error && <p role="alert">{result.error}</p>}
    <div className="actions">{result.data?.versions.map(v => <button key={v.id} onClick={() => setSelected(v.id)}>v{v.version_no} · {v.title} · {v.validation_status}</button>)}</div>
    {result.data?.versions.length === 0 && <p>This team has no versions.</p>}{result.data?.next_before_version_no && <button onClick={() => { setBefore(result.data!.next_before_version_no); setSelected(null); }}>Older versions</button>}{before && <button onClick={() => { setBefore(null); setSelected(null); }}>Latest</button>}
    {version && <VersionDetails key={version.id} version={version} />}</section>;
}

export function TeamView() {
  const [profile, setProfile] = useState<Profile | null>(null); const [teams, setTeams] = useState<Team[]>([]);
  const [events, setEvents] = useState<EventInfo[]>([]); const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(''); const [organizer, setOrganizer] = useState(false);
  const [error, setError] = useState(''); const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false);
  const retry = useRef<{ body: string; key: string } | null>(null);
  const initialization = useRef<AbortController | null>(null);
  async function load(signal: AbortSignal) {
    setLoading(true); setError(''); const start = Date.now();
    try {
      const [me, list, info] = await Promise.all([api<Profile>('/users/me', { signal }), api<{ teams: Team[] }>('/teams', { signal }), api<{ server_time: string; events: EventInfo[] }>('/hackathons', { signal })]);
      if (!signal.aborted) {
        setProfile(me); setTeams(list.teams); setEvents(info.events); setOffset(new Date(info.server_time).getTime() - (start + Date.now()) / 2);
        const saved = localStorage.getItem(`deadline-crush:team:${me.id}`);
        setSelected(list.teams.some(team => team.id === saved) ? saved! : list.teams[0]?.id || '');
      }
    } catch (cause) { if (!signal.aborted) setError(message(cause)); } finally { if (!signal.aborted) setLoading(false); }
  }
  function reloadWorkspace() {
    initialization.current?.abort(); initialization.current = new AbortController();
    void load(initialization.current.signal);
  }
  useEffect(() => { reloadWorkspace(); return () => initialization.current?.abort(); }, []);
  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return; const form = event.currentTarget; const fields = new FormData(form);
    const body = JSON.stringify({ hackathon_id: fields.get('hackathon_id'), name: fields.get('name') });
    if (retry.current?.body !== body) retry.current = { body, key: crypto.randomUUID() }; setBusy(true); setError('');
    try { const team = await api<Team>('/teams', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': retry.current.key }, body });
      retry.current = null; setTeams(previous => previous.some(t => t.id === team.id) ? previous : [...previous, team]); setSelected(team.id);
      if (profile) localStorage.setItem(`deadline-crush:team:${profile.id}`, team.id);
      form.reset();
    } catch (cause) { setError(message(cause)); } finally { setBusy(false); }
  }
  const team = teams.find(t => t.id === selected);
  return <>{loading && <p role="status">Loading your workspace…</p>}{error && <p role="alert">{error}</p>}
    {error && !profile && !loading && <button onClick={reloadWorkspace}>Retry workspace</button>}
    {profile && <><nav className="workspace-nav"><button aria-pressed={!organizer} onClick={() => setOrganizer(false)}>Participant workspace</button>{profile.role === 'ORGANIZER' && <button aria-pressed={organizer} onClick={() => setOrganizer(true)}>Organizer dashboard</button>}
      {!organizer && teams.length > 0 && <label>Your team<select value={selected} onChange={e => { setSelected(e.target.value); localStorage.setItem(`deadline-crush:team:${profile.id}`, e.target.value); }}>{teams.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label>}</nav>
      {organizer && profile.role === 'ORGANIZER' ? <Organizer /> : <>{team ? <Workspace key={team.id} profile={profile} team={team} event={events.find(e => e.id === team.hackathon_id)} offset={offset} /> : <section className="card"><h1>Your team starts here</h1><p>Create a team to become its captain and start your submission.</p></section>}
        <details className="card" open={!teams.length}><summary>Create a team</summary><form onSubmit={create}><fieldset disabled={busy}><label>Hackathon<select name="hackathon_id" required><option value="">Select event</option>{events.map(e => <option value={e.id} key={e.id}>{e.name}</option>)}</select></label><label>Team name<input name="name" required maxLength={100} /></label><button className="primary">{busy ? 'Creating…' : 'Create team'}</button></fieldset></form>{!events.length && <p>No events available. Ask the organizer to configure an event.</p>}</details></>}
    </>}
  </>;
}
