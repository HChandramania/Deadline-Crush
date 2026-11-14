import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { authClient } from './auth';
import './style.css';
import { api, clearApiSession, type Profile, type Team } from './api';

function TeamView() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [teams, setTeams] = useState<Team[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const retry = useRef<{ body: string; key: string } | null>(null);
  useEffect(() => {
    let active = true;
    Promise.all([api<Profile>('/users/me'), api<{ teams: Team[] }>('/teams')])
      .then(([me, list]) => { if (active) { setProfile(me); setTeams(list.teams); } })
      .catch(cause => { if (active) setError(cause instanceof Error ? cause.message : 'Unable to load teams.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    const body = JSON.stringify({ hackathon_id: fields.get('hackathon_id'), name: fields.get('name') });
    if (retry.current?.body !== body) retry.current = { body, key: crypto.randomUUID() };
    setBusy(true); setError('');
    try {
      const team = await api<Team>('/teams', { method: 'POST', headers: {
        'Content-Type': 'application/json', 'Idempotency-Key': retry.current.key,
      }, body });
      retry.current = null;
      setTeams(previous => previous.some(existing => existing.id === team.id) ? previous : [...previous, team]); form.reset();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to create team.'); }
    finally { setBusy(false); }
  }
  return <section aria-label="Your teams"><h2>Your teams</h2>
    {loading ? <p role="status">Loading teams…</p> : teams.length ? <ul>{teams.map(team =>
      <li key={team.id}>{team.name}{profile?.id === team.captain_id ? ' (captain)' : ''}</li>)}</ul> : <p>You have no teams yet.</p>}
    {error && <p role="alert">{error}</p>}
    {profile && <form onSubmit={create}><fieldset disabled={busy}>
      <legend>Create a team</legend>
      <label>Hackathon ID<input name="hackathon_id" required pattern="[a-fA-F0-9-]{36}" /></label>
      <label>Team name<input name="name" required maxLength={100} /></label>
      <button type="submit">{busy ? 'Creating…' : 'Create team'}</button>
    </fieldset></form>}
  </section>;
}

function App() {
  const [user, setUser] = useState<{ email: string; name: string } | null>(null);
  const [mode, setMode] = useState<'signup' | 'signin'>('signup');
  const [pending, setPending] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    let active = true;
    if (!authClient) { setPending(false); return; }
    authClient.getSession().then(({ data, error }) => {
      if (active) {
        setUser(data?.user ?? null);
        if (error) setError('Unable to restore your session. Please try again.');
      }
    }).catch(() => { if (active) setError('Unable to connect. Please try again.'); })
      .finally(() => { if (active) setPending(false); });
    return () => { active = false; };
  }, []);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!authClient) return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    const email = String(fields.get('email')).trim();
    const password = String(fields.get('password'));
    setPending(true); setError(''); setNotice('');
    try {
      const result = mode === 'signup'
        ? await authClient.signUp.email({ email, password, name: String(fields.get('name')).trim() })
        : await authClient.signIn.email({ email, password });
      if (result.error) {
        setError(result.error.message || 'Authentication failed. Please try again.');
        return;
      }
      const session = await authClient.getSession();
      if (session.error) { setError('Unable to read your session. Please sign in again.'); return; }
      setUser(session.data?.user ?? null);
      if (!session.data?.user) setNotice('Account created. Check your email if verification is required, then sign in.');
      form.reset();
    } catch (cause) {
      setError(cause instanceof Error && cause.message === 'Invalid email or password'
        ? 'Invalid email or password.' : 'Authentication failed. Please try again.');
    }
    finally { setPending(false); }
  }

  async function signOut() {
    if (!authClient) return;
    setPending(true); setError('');
    try {
      const result = await authClient.signOut();
      if (result.error) setError('Sign out failed. Please try again.');
      else { clearApiSession(); setUser(null); }
    } catch { setError('Unable to connect. Please try again.'); }
    finally { setPending(false); }
  }

  return <main><h1>Deadline Crush</h1><p>Sign in to your hackathon account. Submissions are not open yet.</p>
    {!authClient ? <p role="alert">Authentication is not configured.</p> : user ?
      <section aria-label="Your account"><h2>Welcome, {user.name}</h2><p>Signed in as {user.email}</p>
        <button disabled={pending} onClick={signOut}>Sign out</button><TeamView key={user.email} /></section> :
      <form onSubmit={submit} aria-busy={pending}><h2>{mode === 'signup' ? 'Create an account' : 'Sign in'}</h2>
        <fieldset disabled={pending}>
          {mode === 'signup' && <label>Name<input name="name" autoComplete="name" required maxLength={100} /></label>}
          <label>Email<input name="email" type="email" autoComplete="email" required maxLength={254} /></label>
          <label>Password<input name="password" type="password" autoComplete={mode === 'signup' ? 'new-password' : 'current-password'} minLength={8} maxLength={128} required /></label>
          <button type="submit">{mode === 'signup' ? 'Create account' : 'Sign in'}</button>
          <button type="button" onClick={() => { setMode(mode === 'signup' ? 'signin' : 'signup'); setError(''); setNotice(''); }}>
            {mode === 'signup' ? 'Already have an account? Sign in' : 'Need an account? Sign up'}</button>
        </fieldset>
      </form>}
    {pending && <p role="status">Please wait…</p>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<App />);
