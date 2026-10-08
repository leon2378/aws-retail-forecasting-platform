const TRANSACTION_KEY = 'supplysight.signin.transaction';
const TRANSACTION_TTL = 10 * 60 * 1000;
const noSession = () => ({ authenticated: false, permissions: {} });

export function createAuthClient({ fetcher = window.fetch.bind(window), cryptoApi = window.crypto, storage = window.sessionStorage, locationApi = window.location, historyApi = window.history, now = Date.now, onExpired = () => {}, apiBase = window.API_BASE || '' } = {}) {
  let config = null;
  let session = noSession();
  let accessToken = null;
  let tokenExpiresAt = 0;
  let sessionExpiresAt = 0;
  let generation = 0;
  const pending = new Set();
  const endpoint = String(apiBase).replace(/\/$/, '');
  const apiUrl = new URL(endpoint || '/', locationApi.origin);
  if (apiUrl.protocol !== 'https:' && !(apiUrl.origin === locationApi.origin && /^https?:$/.test(apiUrl.protocol))) throw new Error('The sign-in API must use a secure address.');

  function clear() {
    ++generation;
    for (const controller of pending) controller.abort();
    pending.clear();
    session = noSession();
    accessToken = null;
    tokenExpiresAt = 0;
    sessionExpiresAt = 0;
  }

  function expired() {
    clear();
    onExpired();
  }

  function staleError() {
    const error = new Error('This request belongs to an earlier session.');
    error.name = 'AbortError';
    return error;
  }

  async function request(path, options = {}, { publicRequest = false, sessionCheck = false } = {}) {
    if (!path.startsWith('/api/') || path.includes('://') || path.includes('\\')) throw new Error('Invalid API request.');
    const ticket = generation;
    const cloud = config?.mode === 'cognito';
    if (!publicRequest && !sessionCheck && !session.authenticated) throw Object.assign(new Error('Sign in to access this workspace.'), { status: 401 });
    if (!publicRequest && cloud && (!accessToken || tokenExpiresAt <= now())) {
      expired();
      throw Object.assign(new Error('Your session has ended. Sign in again.'), { status: 401 });
    }
    const controller = new AbortController();
    pending.add(controller);
    const method = (options.method || 'GET').toUpperCase();
    const headers = { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers };
    if (cloud && !publicRequest) headers.Authorization = `Bearer ${accessToken}`;
    if (!cloud && session.authenticated && !['GET', 'HEAD'].includes(method)) headers['X-CSRF-Token'] = session.csrf_token;
    try {
      const response = await fetcher(`${endpoint}${path}`, { ...options, method, headers, credentials: cloud ? 'omit' : 'same-origin', cache: 'no-store', signal: controller.signal });
      if (ticket !== generation) throw staleError();
      let data;
      try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response. Please try again.'); }
      if (ticket !== generation) throw staleError();
      if (!response.ok) {
        if (response.status === 401 && !publicRequest) expired();
        const message = response.status === 401 ? 'Your session has ended. Sign in again.' : response.status === 403 ? 'Your account does not have permission for this action.' : (typeof data.error === 'string' ? data.error : 'The request could not be completed. Please try again.');
        throw Object.assign(new Error(message), { status: response.status });
      }
      return data;
    } finally { pending.delete(controller); }
  }

  function validateConfig(value) {
    if (!value || value.enabled !== true || !['local_demo', 'cognito'].includes(value.mode)) throw new Error('Sign-in is not configured for this workspace.');
    if (value.mode === 'local_demo') {
      if (apiUrl.origin !== locationApi.origin) throw new Error('The local access preview requires a same-origin API.');
      return value;
    }
    let domain, issuer, callback, logout;
    try { domain = new URL(value.domain); issuer = new URL(value.issuer); callback = new URL(value.callback_url); logout = new URL(value.logout_url); } catch { throw new Error('The sign-in configuration is incomplete.'); }
    const plainUrl = (url) => !url.username && !url.password && !url.search && !url.hash && !url.port;
    const rootUrl = (url) => url.origin === locationApi.origin && url.pathname === '/' && !url.search && !url.hash && !url.username && !url.password;
    const domainMatch = domain.hostname.match(/^[a-z0-9-]+\.auth\.([a-z0-9-]+)\.amazoncognito\.com$/);
    if (domain.protocol !== 'https:' || !domainMatch || !plainUrl(domain) || domain.pathname !== '/' || issuer.protocol !== 'https:' || !plainUrl(issuer) || issuer.hostname !== `cognito-idp.${domainMatch[1]}.amazonaws.com` || !/^\/[A-Za-z0-9_-]+$/.test(issuer.pathname) || !rootUrl(callback) || !rootUrl(logout) || !/^[A-Za-z0-9]{1,128}$/.test(value.client_id || '')) throw new Error('The sign-in configuration could not be verified.');
    const scopes = Array.isArray(value.scopes) ? value.scopes : String(value.scopes || '').split(/\s+/);
    if (!['openid', 'profile', 'retail/read', 'retail/plan'].every((scope) => scopes.includes(scope)) || scopes.some((scope) => !['openid', 'profile', 'retail/read', 'retail/plan'].includes(scope))) throw new Error('The sign-in permissions are not configured correctly.');
    return { ...value, domain: domain.origin, callback_url: callback.href, logout_url: logout.href, scopes };
  }

  function acceptSession(value) {
    if (!value?.authenticated) { session = noSession(); return session; }
    if (!['viewer', 'planner'].includes(value.role) || !value.permissions || typeof value.display_name !== 'string' || (config.mode === 'local_demo' && typeof value.csrf_token !== 'string')) throw new Error('Your account permissions could not be verified.');
    session = { ...value, permissions: { can_simulate: value.permissions.can_simulate === true, can_run_drill: value.permissions.can_run_drill === true, can_demo_release: value.permissions.can_demo_release === true } };
    sessionExpiresAt = Number.isFinite(value.expires_in) && value.expires_in > 0 ? now() + value.expires_in * 1000 : 0;
    return session;
  }

  function consumeCallback() {
    const params = new URLSearchParams(locationApi.search);
    if (!['code', 'state', 'error'].some((key) => params.has(key))) return null;
    const callback = { code: params.get('code'), state: params.get('state'), error: params.has('error'), duplicates: ['code', 'state', 'error'].some((key) => params.getAll(key).length > 1) };
    historyApi.replaceState(null, '', `${locationApi.pathname}${locationApi.hash || ''}`);
    return callback;
  }

  function encode(bytes) {
    return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function random() {
    const bytes = new Uint8Array(32);
    cryptoApi.getRandomValues(bytes);
    return encode(bytes);
  }

  async function exchange(callback) {
    let transaction;
    try { const raw = storage.getItem(TRANSACTION_KEY); storage.removeItem(TRANSACTION_KEY); transaction = JSON.parse(raw || 'null'); } catch { throw new Error('Your sign-in attempt could not be verified. Please start again.'); }
    if (callback.error || callback.duplicates || !callback.code || callback.code.length > 4096 || !callback.state || !transaction || transaction.state !== callback.state || !/^[A-Za-z0-9_-]{43}$/.test(transaction.verifier || '') || !Number.isFinite(transaction.created_at) || transaction.created_at > now() || now() - transaction.created_at > TRANSACTION_TTL || transaction.redirect_uri !== config.callback_url) throw new Error('Your sign-in attempt has expired or could not be verified. Please start again.');
    const ticket = generation;
    const controller = new AbortController();
    pending.add(controller);
    try {
      const body = new URLSearchParams({ grant_type: 'authorization_code', client_id: config.client_id, code: callback.code, redirect_uri: config.callback_url, code_verifier: transaction.verifier });
      const response = await fetcher(`${config.domain}/oauth2/token`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body, credentials: 'omit', cache: 'no-store', signal: controller.signal });
      if (ticket !== generation) throw staleError();
      let value;
      try { value = await response.json(); } catch { throw new Error('Sign-in could not be completed. Please try again.'); }
      if (ticket !== generation) throw staleError();
      if (!response.ok || value.token_type !== 'Bearer' || typeof value.access_token !== 'string' || value.access_token.length < 8 || value.access_token.length > 16384 || !Number.isInteger(value.expires_in) || value.expires_in < 1 || value.expires_in > 86400) throw new Error('Sign-in could not be completed. Please try again.');
      accessToken = value.access_token;
      tokenExpiresAt = now() + value.expires_in * 1000;
      const returnView = ['forecast', 'replenishment', 'models', 'operations'].includes(transaction.return_view) ? transaction.return_view : 'forecast';
      historyApi.replaceState(null, '', `/#${returnView}`);
    } finally { pending.delete(controller); }
  }

  async function initialize() {
    const callback = consumeCallback();
    config = validateConfig(await request('/api/auth/config', {}, { publicRequest: true }));
    if (config.mode === 'cognito') {
      if (!callback) return session;
      await exchange(callback);
    } else if (callback) throw new Error('This local preview does not accept cloud sign-in callbacks.');
    try { return acceptSession(await request('/api/auth/session', {}, { sessionCheck: true })); } catch (error) { if (error.status === 401) return session; throw error; }
  }

  async function signIn(role) {
    clear();
    if (!config) throw new Error('Sign-in is still connecting. Please try again.');
    if (config.mode === 'local_demo') {
      if (!['viewer', 'planner'].includes(role)) throw new Error('Choose a preview role.');
      return acceptSession(await request('/api/auth/login', { method: 'POST', body: JSON.stringify({ role }) }, { publicRequest: true }));
    }
    const state = random();
    const verifier = random();
    const challenge = encode(new Uint8Array(await cryptoApi.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
    const returnView = locationApi.hash.slice(1);
    try { storage.setItem(TRANSACTION_KEY, JSON.stringify({ state, verifier, created_at: now(), redirect_uri: config.callback_url, return_view: returnView })); } catch { throw new Error('Allow temporary browser storage to complete sign-in.'); }
    const url = new URL(`${config.domain}/oauth2/authorize`);
    url.search = new URLSearchParams({ response_type: 'code', client_id: config.client_id, redirect_uri: config.callback_url, scope: config.scopes.join(' '), state, code_challenge: challenge, code_challenge_method: 'S256' });
    locationApi.assign(url.href);
    return session;
  }

  async function signOut() {
    const cloud = config?.mode === 'cognito';
    try {
      if (!cloud && session.authenticated) await request('/api/auth/logout', { method: 'POST', body: '{}' });
    } finally { clear(); try { storage.removeItem(TRANSACTION_KEY); } catch { /* Sign-out still clears in-memory access when storage is unavailable. */ } }
    if (cloud) {
      const url = new URL(`${config.domain}/logout`);
      url.search = new URLSearchParams({ client_id: config.client_id, logout_uri: config.logout_url });
      locationApi.assign(url.href);
    }
  }

  async function checkSession() {
    if (!session.authenticated) return session;
    const previous = JSON.stringify([session.role, session.permissions]);
    const next = acceptSession(await request('/api/auth/session', {}, { sessionCheck: true }));
    if (!next.authenticated) expired();
    if (next.authenticated && previous !== JSON.stringify([next.role, next.permissions])) {
      ++generation;
      for (const controller of pending) controller.abort();
    }
    return session;
  }

  return { initialize, signIn, signOut, request, checkSession, clear, getConfig: () => config, getSession: () => session, getGeneration: () => generation, getExpiresAt: () => config?.mode === 'cognito' ? tokenExpiresAt : sessionExpiresAt };
}
