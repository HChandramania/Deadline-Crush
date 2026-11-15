import { test, expect, type Page } from '@playwright/test';

const user = '11111111-1111-4111-8111-111111111111';
const team = '22222222-2222-4222-8222-222222222222';
const event = '33333333-3333-4333-8333-333333333333';
const version = '44444444-4444-4444-8444-444444444444';
const session = '55555555-5555-4555-8555-555555555555';
const deadline = new Date(Date.now() + 3600000).toISOString();
const grace = new Date(Date.now() + 4200000).toISOString();
const artifact = Buffer.from('test artifact bytes');

async function fixture(page: Page, role = 'CAPTAIN', interrupt = false, emptyTeam = false) {
  let logged = true, admitted = false, uploaded = false, finalized = false, checking = 0, puts = 0;
  let teamCreated = !emptyTeam;
  const keys: string[] = [];
  const v = () => ({ id: version, version_no: 1, title: 'Our project', description: 'A useful tool', links: [], filename: 'project.zip',
    size_bytes: artifact.length, lifecycle: finalized ? 'FINALIZED' : 'UPLOADING', validation_status: finalized ? (++checking > 2 ? 'ACCEPTED' : 'CHECKING') : 'NOT_STARTED', admitted_at: new Date().toISOString(), finalized_at: finalized ? new Date().toISOString() : null });
  await page.route('**/auth/**', async route => {
    if (route.request().url().includes('sign-out')) logged = false;
    await route.fulfill({ json: logged ? { user: { id: user, email: 'fixture@example.com', name: 'Participant', emailVerified: true, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() },
      session: { id: 'fixture', token: 'test-only-managed-access', userId: user, expiresAt: new Date(Date.now() + 86400000).toISOString() } } : null,
      headers: { 'set-auth-jwt': 'test-only-managed-access' } });
  });
  await page.route('**/r2-fixture', async route => {
    puts++;
    if (interrupt && puts <= 3) { await route.abort('failed'); return; }
    uploaded = true; await route.fulfill({ status: 200, headers: { ETag: '"fixture-etag"' } });
  });
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname.replace('/api/v1', '');
    expect(route.request().headers().authorization).toBe('Bearer test-only-managed-access');
    let data: unknown;
    if (path === '/users/me') data = { id: user, email: 'fixture@example.com', role: role === 'ORGANIZER' ? 'ORGANIZER' : 'MEMBER' };
    else if (path === '/teams') {
      const value = { id: team, name: 'Build Crew', hackathon_id: event, captain_id: role === 'MEMBER' ? 'other' : user };
      if (route.request().method() === 'POST') { teamCreated = true; data = value; }
      else data = { teams: teamCreated ? [value] : [] };
    }
    else if (path.startsWith('/teams/') && path.endsWith('/members')) data = { members: [{ user_id: user, role: role === 'MEMBER' ? 'MEMBER' : 'CAPTAIN' }] };
    else if (path === '/hackathons') data = { server_time: new Date().toISOString(), events: [{ id: event, name: 'Challenge 2026', deadline_at: deadline, grace_until: grace, max_file_bytes: 104857600 }] };
    else if (path === '/submissions/versions' && route.request().method() === 'POST') {
      keys.push(route.request().headers()['idempotency-key']); admitted = true;
      data = { version_id: version, upload_session_id: session, version_no: 1 };
    } else if (path === '/submissions/versions') data = { versions: admitted ? [v()] : [], next_before_version_no: null };
    else if (path === '/submissions/current') data = finalized ? { selected_version_id: version, version_no: 1, validation_status: v().validation_status } : { selected_version_id: null };
    else if (path === `/uploads/${session}` || path.endsWith('/initialize')) data = {
      status: finalized ? 'COMPLETED' : 'ACTIVE', part_size_bytes: 8388608, expected_parts: 1,
      completed_parts: uploaded ? [{ part_number: 1, etag: '"fixture-etag"', size_bytes: artifact.length }] : [], missing_parts: uploaded ? [] : [1], recoverable: true,
      completion_pending: false, parts_source: 'R2', deadline_at: deadline, grace_until: grace,
    };
    else if (path.endsWith('/parts/sign')) data = { parts: [{ part_number: 1, url: 'http://127.0.0.1:4173/r2-fixture', headers: {}, expires_at: grace }] };
    else if (path.endsWith('/complete')) { finalized = true; data = { status: 'FINALIZED', completion_intent_id: session }; }
    else if (path.endsWith('/details')) data = { job_status: 'SUCCEEDED', attempts: 1, findings: ['<script>untrusted</script>'], completed_at: new Date().toISOString(), previews: ['ZIP_PREVIEW'], upload_session_id: session };
    else if (path.includes('/preview/')) { await route.fulfill({ contentType: 'text/html', body: '<h1>README</h1><script>parent.pwned=true</script>' }); return; }
    else if (path === '/organizer/metrics') data = { teams: 1, total_versions: 1, finalized_versions: 1, checking_versions: 0, accepted_versions: 1, failed_checks: 0, queue_pending: 0, validation: { failed: 0 }, recent_activity: [] };
    else if (path === '/organizer/submissions') data = { rows: [{ team_id: team, team_name: 'Build Crew', version_id: version, version_no: 1, latest_version_no: 2, validation_status: 'ACCEPTED', finalized_at: deadline }], next_after: null };
    else if (path.includes('/organizer/teams/')) data = { versions: [v()], next_before_version_no: null };
    else if (path === '/organizer/export.csv') { await route.fulfill({ contentType: 'text/csv', body: 'team_name,version_no\r\nBuild Crew,1\r\n' }); return; }
    else { await route.fulfill({ status: 404, json: { error: { message: 'Fixture endpoint unavailable' } } }); return; }
    await route.fulfill({ json: data });
  });
  return { keys, get puts() { return puts; } };
}

test.describe('controlled browser contracts (mocked services)', () => {
  test.skip(!!process.env.PW_LIVE_URL, 'Run controlled tests with the isolated local Vite server');
  test('initial service failure can recover without signing out or reloading', async ({ page }) => {
    await fixture(page); let first = true;
    await page.route('**/api/v1/hackathons', async route => {
      if (first) { first = false; await route.fulfill({ status: 503, json: { error: { code: 'DEPENDENCY_FAILED', message: 'Service temporarily unavailable' } } }); }
      else await route.fallback();
    });
    await page.goto('/'); await expect(page.getByRole('alert')).toContainText('temporarily unavailable');
    await page.getByRole('button', { name: 'Retry workspace', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Build Crew', exact: true })).toBeVisible();
  });
  test('first team creation keeps the initially open form usable', async ({ page }) => {
    await fixture(page, 'CAPTAIN', false, true); await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Your team starts here' })).toBeVisible();
    const create = page.locator('details').filter({ has: page.locator('summary', { hasText: 'Create a team' }) });
    if ((await create.getAttribute('open')) === null) await create.locator('summary').click();
    await create.getByLabel('Hackathon').selectOption(event);
    await create.getByLabel('Team name', { exact: true }).fill('Build Crew');
    await create.getByRole('button', { name: 'Create team', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Build Crew', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Admit version & upload' })).toBeVisible();
  });
  test('reload restores the selected authorized team and rejects stale saved context', async ({ page }) => {
    await fixture(page);
    const second = '66666666-6666-4666-8666-666666666666';
    await page.route('**/api/v1/teams', route => route.fulfill({ json: { teams: [
      { id: team, name: 'First team', hackathon_id: event, captain_id: user },
      { id: second, name: 'Second team', hackathon_id: event, captain_id: user },
    ] } }));
    await page.goto('/'); await page.getByRole('combobox', { name: 'Your team', exact: true }).selectOption(second);
    await page.reload(); await expect(page.getByRole('heading', { name: 'Second team', exact: true })).toBeVisible();
    await page.evaluate(({ user }) => localStorage.setItem(`deadline-crush:team:${user}`, 'not-an-authorized-team'), { user });
    await page.reload(); await expect(page.getByRole('heading', { name: 'First team', exact: true })).toBeVisible();
  });
  test('captain uploads, sees live finalization, and restores state after reload', async ({ page }) => {
    const f = await fixture(page);
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Build Crew', exact: true })).toBeVisible();
    await page.getByLabel('Project title').fill('Our project');
    await page.getByLabel('Artifact ·').setInputFiles({ name: 'project.zip', mimeType: 'application/zip', buffer: artifact });
    await page.getByRole('button', { name: 'Admit version & upload' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Submission finalized' })).toBeVisible();
    await expect(page.getByText('ACCEPTED', { exact: true }).first()).toBeVisible({ timeout: 25000 });
    expect(f.keys).toHaveLength(1); expect(f.puts).toBe(1);
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Version 1', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Upload finalized' })).toBeVisible();
    await page.getByRole('button', { name: /v1 · Our project/ }).click();
    await expect(page.getByText('<script>untrusted</script>', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: 'README & file inventory' }).click();
    await expect(page.locator('iframe')).toHaveAttribute('sandbox', '');
    await page.screenshot({ path: 'test-results/participant-desktop.png', fullPage: true });
  });
  test('interruption and refresh resume the same admission with matching file', async ({ page }) => {
    const f = await fixture(page, 'CAPTAIN', true); await page.goto('/');
    await page.getByLabel('Project title').fill('Our project');
    await page.getByLabel('Artifact ·').setInputFiles({ name: 'project.zip', mimeType: 'application/zip', buffer: artifact });
    await page.getByRole('button', { name: 'Admit version & upload' }).click();
    await expect(page.getByRole('alert')).toContainText('Part upload interrupted');
    await page.getByRole('button', { name: 'Recover this version', exact: true }).click();
    await page.getByRole('button', { name: 'Start a separate version', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Create a submission version', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Recover this version', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Resume / recover same upload', exact: true })).toBeVisible();
    await page.reload();
    await page.getByLabel('Reselect original file').setInputFiles({ name: 'project.zip', mimeType: 'application/zip', buffer: Buffer.from('different bytes') });
    await page.getByRole('button', { name: 'Resume / recover same upload' }).click();
    await expect(page.getByRole('alert')).toContainText('does not match');
    await page.getByLabel('Reselect original file').setInputFiles({ name: 'project.zip', mimeType: 'application/zip', buffer: artifact });
    await page.getByRole('button', { name: 'Resume / recover same upload' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Submission finalized' })).toBeVisible();
    expect(f.keys).toHaveLength(1); expect(f.puts).toBe(4);
  });
  test('ordinary member has a read-only workspace on mobile', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 }); await fixture(page, 'MEMBER'); await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Team member access' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Admit version & upload' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Organizer dashboard' })).toHaveCount(0);
    await page.screenshot({ path: 'test-results/member-mobile.png', fullPage: true });
  });
  test('organizer views aggregate metrics, drilldown, and CSV download', async ({ page }) => {
    await fixture(page, 'ORGANIZER'); await page.goto('/'); await page.getByRole('button', { name: 'Organizer dashboard' }).click();
    await expect(page.getByRole('heading', { name: 'Submission monitor' })).toBeVisible();
    await expect(page.getByRole('cell', { name: 'Build Crew' })).toBeVisible();
    await page.getByRole('button', { name: 'Inspect team' }).click();
    await expect(page.getByRole('heading', { name: 'Team version drilldown' })).toBeVisible();
    const download = page.waitForEvent('download'); await page.getByRole('button', { name: 'Export all teams · CSV' }).click();
    expect((await download).suggestedFilename()).toBe('deadline-crush.csv');
    await page.screenshot({ path: 'test-results/organizer-desktop.png', fullPage: true });
  });
  test('signed-out managed session never opens protected workspace', async ({ page }) => {
    await fixture(page); await page.goto('/'); await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Create an account' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Build Crew', exact: true })).toHaveCount(0);
  });
});

test('LIVE managed sign-in and existing team status (no service mocking)', async ({ page }) => {
  test.skip(!process.env.PW_LIVE_URL || !process.env.PW_EMAIL || !process.env.PW_PASSWORD, 'Explicit isolated live URL and test identity required');
  await page.goto('/');
  await page.getByRole('button', { name: 'Already have an account? Sign in' }).click();
  await page.getByLabel('Email', { exact: true }).fill(process.env.PW_EMAIL!);
  await page.getByLabel('Password', { exact: true }).fill(process.env.PW_PASSWORD!);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Participant workspace' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('LIVE isolated participant admission, interrupted R2 upload, worker acceptance and restoration', async ({ page }) => {
  test.skip(!process.env.PW_LIVE_URL || (process.env.PW_INTERACTIVE !== '1' && (!process.env.PW_EMAIL || !process.env.PW_PASSWORD)) || !process.env.PW_LIVE_EVENT_ID || process.env.PW_LIVE_MUTATIONS !== 'isolated-only',
    'Explicit isolated fixture URL/event/identity and mutation opt-in required; never run against production');
  test.setTimeout(process.env.PW_INTERACTIVE === '1' ? 900000 : 240000);
  // Sanitized diagnostics only: no cookies, JWTs, signed URLs or credentials.
  page.on('response', async response => {
    const path = new URL(response.url()).pathname;
    if (path.endsWith('/get-session')) {
      const data = await response.json().catch(() => null);
      console.log('Managed session response:', JSON.stringify({ status: response.status(), user: !!data?.user,
        session: !!data?.session, jwtHeader: !!(await response.headerValue('set-auth-jwt')) }));
    } else if (path.startsWith('/api/v1/') && response.status() === 401) console.log('Application rejected access token; SDK refresh required.');
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Already have an account? Sign in' }).click();
  if (process.env.PW_INTERACTIVE === '1') {
    console.log('Sign in interactively in the opened browser using the existing participant account. Credentials are not captured by the test.');
    await expect(page.getByRole('button', { name: 'Participant workspace' })).toBeVisible({ timeout: 600000 });
  } else {
    await page.getByLabel('Email', { exact: true }).fill(process.env.PW_EMAIL!);
    await page.getByLabel('Password', { exact: true }).fill(process.env.PW_PASSWORD!);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Participant workspace' })).toBeVisible();
  }
  console.log('Live managed authentication verified.');
  const create = page.locator('details').filter({ has: page.locator('summary', { hasText: 'Create a team' }) });
  if ((await create.getAttribute('open')) === null) await create.locator('summary').click();
  await create.getByLabel('Hackathon').selectOption(process.env.PW_LIVE_EVENT_ID!);
  const name = `Phase5 browser ${Date.now()}`;
  await create.getByLabel('Team name', { exact: true }).fill(name);
  await create.getByRole('button', { name: 'Create team', exact: true }).click();
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  await expect(page.getByText('CAPTAIN', { exact: true }).first()).toBeVisible();
  console.log('Live team creation and captain assignment verified.');
  const zip = Buffer.from('UEsDBBQAAAAAAJyQb10mKzr+KwAAACsAAAAJAAAAUkVBRE1FLm1kRGVhZGxpbmUgQ3J1c2ggUGhhc2UgNSBicm93c2VyIHZlcmlmaWNhdGlvblBLAQIUABQAAAAAAJyQb10mKzr+KwAAACsAAAAJAAAAAAAAAAAAAACAAQAAAABSRUFETUUubWRQSwUGAAAAAAEAAQA3AAAAUgAAAAAA', 'base64');
  const file = { name: 'phase5-browser.zip', mimeType: 'application/zip', buffer: zip };
  await page.getByLabel('Project title').fill('Phase 5 live verification');
  await page.getByLabel('Artifact ·').setInputFiles(file);
  // Inject only a transport failure: all API/auth responses and eventual R2 bytes remain real.
  const interrupt = async (route: import('@playwright/test').Route) => {
    if (route.request().method() === 'PUT') await route.abort('internetdisconnected'); else await route.continue();
  };
  await page.route('**/*', interrupt);
  await page.getByRole('button', { name: 'Admit version & upload' }).click();
  await expect(page.getByRole('alert')).toContainText('Part upload interrupted', { timeout: 60000 });
  console.log('Live upload transport interruption verified.');
  await page.unroute('**/*', interrupt);
  await page.reload();
  await page.getByLabel('Reselect original file').setInputFiles(file);
  await page.getByRole('button', { name: 'Resume / recover same upload' }).click();
  await expect(page.getByRole('status').filter({ hasText: /Submission finalized|Recovery pending/ })).toBeVisible({ timeout: 60000 });
  await expect(page.getByText('ACCEPTED', { exact: true }).first()).toBeVisible({ timeout: 120000 });
  console.log('Live R2 resume, finalization and worker ACCEPTED verified.');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Version 1', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /v1 · Phase 5 live verification/ }).click();
  await expect(page.getByRole('button', { name: 'README & file inventory' })).toBeVisible();
  await page.getByRole('button', { name: 'README & file inventory' }).click();
  await expect(page.frameLocator('iframe').getByRole('heading', { name: 'README', exact: true })).toBeVisible();
  console.log('Live reload restoration and authorized private preview verified.');
  // If the explicitly approved application organizer role is present, verify it
  // using the same genuine managed session; no role is granted by this test.
  if (await page.getByRole('button', { name: 'Organizer dashboard', exact: true }).count()) {
    await page.getByRole('button', { name: 'Organizer dashboard', exact: true }).click();
    await expect(page.getByRole('cell', { name, exact: true })).toBeVisible();
    await page.getByRole('row').filter({ has: page.getByRole('cell', { name, exact: true }) }).getByRole('button', { name: 'Inspect team' }).click();
    await page.getByRole('button', { name: /v1 · Phase 5 live verification/ }).click();
    await expect(page.getByRole('button', { name: 'README & file inventory' })).toBeVisible();
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Export all teams · CSV' }).click();
    const csvDownload = await download;
    const stream = await csvDownload.createReadStream();
    const chunks: Buffer[] = [];
    for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
    const text = Buffer.concat(chunks).toString('utf8');
    expect(text).toContain(name); expect(text).toContain('ACCEPTED');
    console.log('Live approved organizer aggregates, drilldown and actual CSV verified.');
  }
});
