import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { webcrypto } from 'node:crypto';

const source = await readFile(new URL('../frontend/auth.js', import.meta.url), 'utf8');
const { createAuthClient } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const cloudConfig = { mode: 'cognito', enabled: true, domain: 'https://orderflow.auth.ap-southeast-2.amazoncognito.com', issuer: 'https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_Preview', client_id: 'exampleClient123', callback_url: 'https://orders.example/', logout_url: 'https://orders.example/', scopes: ['openid', 'profile', 'orderflow/read', 'orderflow/write'] };
const session = (role = 'operator') => ({ authenticated: true, role, display_name: `Preview ${role}`, csrf_token: 'server-csrf-proof', expires_in: 900, permissions: { can_create: role !== 'viewer', can_retry: role !== 'viewer', can_drill: role === 'admin', can_manage_inventory: role === 'admin' } });
const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

function harness(mode = 'local_demo', overrides = {}) {
  const values = new Map();
  const calls = [];
  const locations = [];
  const replacements = [];
  let clock = 1000000;
  let expires = 0;
  let active = { authenticated: false };
  const locationApi = { origin: 'https://orders.example', pathname: '/', search: '', hash: '#recovery', assign: (url) => locations.push(url) };
  const storage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
  const config = mode === 'cognito' ? { ...cloudConfig, ...overrides } : { mode, enabled: true };
  let route = async (url, options) => {
    if (url.endsWith('/api/auth/config')) return reply(config);
    if (url.endsWith('/api/auth/login')) { active = session(JSON.parse(options.body).role); return reply(active); }
    if (url.endsWith('/api/auth/logout')) { active = { authenticated: false }; return reply({ authenticated: false }); }
    if (url.endsWith('/api/auth/session')) return reply(mode === 'cognito' ? session('viewer') : active);
    if (url.endsWith('/oauth2/token')) return reply({ token_type: 'Bearer', access_token: 'memory-only-access-token', id_token: 'never-used-id-token', refresh_token: 'never-used-refresh-token', expires_in: 900 });
    return reply({ accepted: true });
  };
  const client = createAuthClient({ apiBase: '', fetcher: async (url, options) => { calls.push({ url, options }); return route(url, options); }, cryptoApi: webcrypto, storage, locationApi, historyApi: { replaceState: (_, __, url) => replacements.push(url) }, now: () => clock, onExpired: () => expires++ });
  return { client, calls, values, locations, replacements, locationApi, config, setTime: (value) => { clock = value; }, getExpired: () => expires, setRoute: (value) => { route = value; } };
}

async function cloudSignedIn(overrides) {
  const h = harness('cognito', overrides);
  await h.client.initialize();
  await h.client.signIn();
  const authorize = new URL(h.locations.at(-1));
  h.locationApi.search = `?code=one-use-code&state=${authorize.searchParams.get('state')}`;
  await h.client.initialize();
  return h;
}

test('local preview starts anonymous and requires a selected role', async () => {
  const h = harness();
  assert.equal((await h.client.initialize()).authenticated, false);
  await assert.rejects(h.client.request('/api/catalog'), /Sign in/);
  await assert.rejects(h.client.signIn('owner'), /Choose a preview role/);
  assert.equal(h.calls.filter((call) => call.url.endsWith('/login')).length, 0);
});

test('viewer, operator and admin sessions keep server-confirmed permissions', async () => {
  for (const role of ['viewer', 'operator', 'admin']) {
    const h = harness(); await h.client.initialize();
    const value = await h.client.signIn(role);
    assert.equal(value.role, role);
    assert.equal(value.permissions.can_create, role !== 'viewer');
    assert.equal(value.permissions.can_drill, role === 'admin');
    assert.equal(value.permissions.can_manage_inventory, role === 'admin');
  }
});

test('local authenticated POSTs include the server CSRF token and same-origin cookies', async () => {
  const h = harness();
  await h.client.initialize();
  await h.client.signIn('operator');
  await h.client.request('/api/orders', { method: 'POST', body: '{}' });
  const call = h.calls.at(-1);
  assert.equal(call.options.headers['X-CSRF-Token'], 'server-csrf-proof');
  assert.equal(call.options.credentials, 'same-origin');
  assert.equal(call.options.headers.Authorization, undefined);
  assert.equal(h.client.getExpiresAt(), 1900000);
  assert.equal(h.values.size, 0);
});

test('a forbidden action keeps a healthy read session', async () => {
  const h = harness();
  await h.client.initialize();
  await h.client.signIn('viewer');
  h.setRoute(async () => reply({ error: 'Denied' }, 403));
  await assert.rejects(h.client.request('/api/orders', { method: 'POST', body: '{}' }), (error) => error.status === 403);
  assert.equal(h.client.getSession().authenticated, true);
  assert.equal(h.getExpired(), 0);
});

test('conflict errors retain the server code for safe workflow handling', async () => {
  const h = harness(); await h.client.initialize(); await h.client.signIn('operator');
  h.setRoute(async () => reply({ error: 'This request key belongs to another order.', code: 'idempotency_conflict' }, 409));
  await assert.rejects(h.client.request('/api/orders', { method: 'POST', body: '{}' }), (error) => error.status === 409 && error.code === 'idempotency_conflict');
  assert.equal(h.client.getSession().authenticated, true);
});

test('server session expiry clears identity and cancels pending requests', async () => {
  const h = harness();
  await h.client.initialize();
  await h.client.signIn('operator');
  h.setRoute(async () => reply({ error: 'Expired' }, 401));
  await assert.rejects(h.client.request('/api/catalog'), (error) => error.status === 401);
  assert.equal(h.client.getSession().authenticated, false);
  assert.equal(h.getExpired(), 1);
  assert.equal(h.client.getExpiresAt(), 0);
});

test('an in-flight order cannot return into a later session', async () => {
  const h = harness();
  await h.client.initialize();
  await h.client.signIn('operator');
  let release;
  h.setRoute(() => new Promise((resolve) => { release = resolve; }));
  const pending = h.client.request('/api/orders');
  h.client.clear();
  release(reply({ orders: ['private'] }));
  await assert.rejects(pending, (error) => error.name === 'AbortError');
  assert.equal(h.calls.at(-1).options.signal.aborted, true);
});

test('local logout authenticates its mutation and clears memory', async () => {
  const h = harness();
  await h.client.initialize();
  await h.client.signIn('operator');
  await h.client.signOut();
  const call = h.calls.at(-1);
  assert.ok(call.url.endsWith('/auth/logout'));
  assert.equal(call.options.headers['X-CSRF-Token'], 'server-csrf-proof');
  assert.equal(h.client.getSession().authenticated, false);
});

test('failed logout still removes displayed-session credentials', async () => {
  const h = harness();
  await h.client.initialize();
  await h.client.signIn('operator');
  h.setRoute(async () => { throw new Error('Offline'); });
  await assert.rejects(h.client.signOut(), /Offline/);
  assert.equal(h.client.getSession().authenticated, false);
});

test('Cognito authorize uses secretless code flow, cryptographic state, and S256 PKCE', async () => {
  const h = harness('cognito');
  await h.client.initialize();
  await h.client.signIn();
  const authorize = new URL(h.locations.at(-1));
  assert.equal(authorize.origin, cloudConfig.domain);
  assert.equal(authorize.pathname, '/oauth2/authorize');
  assert.equal(authorize.searchParams.get('response_type'), 'code');
  assert.equal(authorize.searchParams.get('code_challenge_method'), 'S256');
  assert.match(authorize.searchParams.get('state'), /^[A-Za-z0-9_-]{43}$/);
  assert.equal(authorize.searchParams.has('client_secret'), false);
  const transaction = JSON.parse([...h.values.values()][0]);
  const digest = Buffer.from(await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(transaction.verifier))).toString('base64url');
  assert.equal(authorize.searchParams.get('code_challenge'), digest);
  assert.equal(transaction.return_view, 'recovery');
});

test('callback is scrubbed before requests; only access token authorizes APIs and nothing persists', async () => {
  const h = await cloudSignedIn();
  assert.equal(h.replacements[0], '/#recovery');
  assert.equal(h.replacements.at(-1), '/#recovery');
  assert.equal(h.values.size, 0);
  assert.equal(h.client.getSession().role, 'viewer');
  await h.client.request('/api/catalog');
  const call = h.calls.at(-1);
  assert.equal(call.options.headers.Authorization, 'Bearer memory-only-access-token');
  assert.equal(call.options.credentials, 'omit');
  assert.equal(call.options.headers['X-CSRF-Token'], undefined);
  const exchange = h.calls.find((entry) => entry.url.endsWith('/oauth2/token'));
  assert.equal(exchange.options.body.get('code_verifier').length, 43);
  assert.equal(exchange.options.body.has('client_secret'), false);
  assert.equal(exchange.options.body.get('redirect_uri'), 'https://orders.example/');
});

test('a mismatched state is rejected before token exchange and cannot be replayed', async () => {
  const h = harness('cognito');
  await h.client.initialize(); await h.client.signIn();
  h.locationApi.search = '?code=untrusted&state=wrong-state';
  await assert.rejects(h.client.initialize(), /could not be verified/);
  assert.equal(h.values.size, 0);
  const expected = new URL(h.locations.at(-1)).searchParams.get('state');
  h.locationApi.search = `?code=untrusted&state=${expected}`;
  await assert.rejects(h.client.initialize(), /could not be verified/);
  assert.equal(h.calls.some((call) => call.url.endsWith('/oauth2/token')), false);
});

test('expired, duplicate and error callbacks cannot exchange codes', async () => {
  for (const kind of ['expired', 'duplicate', 'error']) {
    const h = harness('cognito');
    await h.client.initialize(); await h.client.signIn();
    const state = new URL(h.locations.at(-1)).searchParams.get('state');
    h.locationApi.search = `?code=attempt&state=${state}${kind === 'duplicate' ? '&code=other' : kind === 'error' ? '&error=access_denied' : ''}`;
    if (kind === 'expired') h.setTime(1600001);
    await assert.rejects(h.client.initialize(), /expired or could not be verified/);
    assert.equal(h.calls.some((call) => call.url.endsWith('/oauth2/token')), false);
    assert.equal(h.values.size, 0);
  }
});

test('expired memory access token is cleared before any protected request', async () => {
  const h = await cloudSignedIn();
  const count = h.calls.length;
  h.setTime(1900001);
  await assert.rejects(h.client.request('/api/catalog'), (error) => error.status === 401);
  assert.equal(h.calls.length, count);
  assert.equal(h.client.getSession().authenticated, false);
});

test('cloud logout uses only client and exact logout URI, never credentials in URL', async () => {
  const h = await cloudSignedIn();
  await h.client.signOut();
  const url = new URL(h.locations.at(-1));
  assert.equal(url.pathname, '/logout');
  assert.deepEqual([...url.searchParams.keys()].sort(), ['client_id', 'logout_uri']);
  assert.equal(url.searchParams.get('logout_uri'), 'https://orders.example/');
  assert.equal(h.client.getSession().authenticated, false);
});

test('unassigned cloud group is forbidden without an automatic authorization redirect', async () => {
  const h = harness('cognito');
  await h.client.initialize(); await h.client.signIn();
  const state = new URL(h.locations.at(-1)).searchParams.get('state');
  h.locationApi.search = `?code=attempt&state=${state}`;
  h.setRoute(async (url) => url.endsWith('/api/auth/config') ? reply(h.config) : url.endsWith('/oauth2/token') ? reply({ token_type: 'Bearer', access_token: 'valid-but-unassigned', expires_in: 900 }) : reply({ error: 'Access not assigned' }, 403));
  await assert.rejects(h.client.initialize(), (error) => error.status === 403);
  assert.equal(h.locations.length, 1);
  assert.equal(h.client.getSession().authenticated, false);
});

test('untrusted auth domains, foreign redirects and invalid scope sets are blocked', async () => {
  for (const config of [{ domain: 'https://signin.attacker.example/' }, { domain: 'http://orderflow.auth.ap-southeast-2.amazoncognito.com/' }, { domain: `${cloudConfig.domain}/foreign` }, { callback_url: 'https://attacker.example/' }, { callback_url: 'https://orders.example/auth/callback' }, { logout_url: 'https://orders.example/?next=foreign' }, { issuer: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Preview' }, { scopes: ['openid', 'admin'] }]) {
    const h = harness('cognito', config);
    await assert.rejects(h.client.initialize(), /configuration|permissions/);
    assert.equal(h.locations.length, 0);
  }
});

test('changing server-confirmed permissions cancels requests from the earlier role', async () => {
  const h = harness();
  await h.client.initialize(); await h.client.signIn('operator');
  let release;
  h.setRoute(async (url) => url.endsWith('/auth/session') ? reply(session('viewer')) : new Promise((resolve) => { release = resolve; }));
  const pending = h.client.request('/api/operations');
  await h.client.checkSession();
  release(reply({ summary: 'earlier-role-data' }));
  await assert.rejects(pending, (error) => error.name === 'AbortError');
  assert.equal(h.client.getSession().role, 'viewer');
  assert.equal(h.client.getSession().permissions.can_create, false);
});
