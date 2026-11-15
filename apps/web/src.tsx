import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { authClient } from './auth';
import './style.css';
import { clearApiSession } from './api';
import { TeamView } from './workspace';

function App() {
  const [user, setUser] = useState<{ email: string; name: string } | null>(null);
  const [mode, setMode] = useState<'signup' | 'signin'>('signup');
  const [pending, setPending] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    const expired = () => { clearApiSession(); setUser(null); setError('Your session expired. Sign in to continue your admitted upload.'); };
    window.addEventListener('session-expired', expired);
    return () => window.removeEventListener('session-expired', expired);
  }, []);

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

  return <main><header className="app-header"><a className="brand" href="/">DC<span>Deadline Crush<small>HACK4HACK · SUBMISSION PORTAL</small></span></a><span className="muted">Build boldly. Submit confidently.</span></header>
    {!authClient ? <p role="alert">Authentication is not configured.</p> : user ?
      <section aria-label="Your account"><div className="account-bar"><span>Welcome, {user.name} · {user.email}</span>
        <button disabled={pending} onClick={signOut}>Sign out</button></div><TeamView key={user.email} /></section> :
      <form className="auth-card card" onSubmit={submit} aria-busy={pending}><span className="eyebrow">Your next big idea, delivered.</span><h1>Make the deadline.<br />Keep the momentum.</h1><p>Secure uploads, recoverable progress, and a clear record of every version.</p><h2>{mode === 'signup' ? 'Create an account' : 'Sign in'}</h2>
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
