import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const [authSource, appSource, html] = await Promise.all(['../frontend/auth.js', '../frontend/app.js', '../frontend/index.html'].map((path) => readFile(new URL(path, import.meta.url), 'utf8')));
const mainMarkup = html.slice(html.indexOf('<main '), html.indexOf('</main>'));
const mainIds = [...mainMarkup.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);

function browser() {
  const nodes = new Map();
  const defaults = new Map();
  const nav = ['forecast', 'replenishment', 'models', 'operations'].map((view) => ({ dataset: { view }, addEventListener() {}, classList: { toggle() {} }, setAttribute() {}, removeAttribute() {} }));
  class Node {
    constructor(id = '') { this.id = id; this.hidden = false; this.disabled = false; this.textContent = ''; this.value = ''; this.children = []; this.handlers = new Map(); this.attributes = new Map(); this.classList = { toggle() {}, remove() {}, add() {} }; this.snapshot = null; }
    addEventListener(name, handler) { this.handlers.set(name, handler); }
    append(...children) { this.children.push(...children); this.textContent += children.map((child) => child.textContent || '').join(''); }
    replaceChildren(...children) { this.children = []; this.textContent = ''; this.append(...children); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    querySelectorAll(selector) { return selector === 'input' ? ['initial-stock', 'lead-time', 'review-period', 'safety-days', 'unit-cost', 'holding-cost', 'stockout-cost', 'order-cost'].map((id) => nodes.get(id)) : []; }
    cloneNode() { const clone = new Node(this.id); clone.snapshot = this.snapshot || new Map(mainIds.map((id) => [id, { ...defaults.get(id) }])); return clone; }
    replaceWith(clone) { for (const [id, value] of clone.snapshot || []) nodes.set(id, Object.assign(new Node(id), value)); nodes.set(this.id, clone); }
    insertAdjacentElement(_, node) { nodes.set(node.id, node); }
    reportValidity() { return true; }
    scrollIntoView() {}
  }
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = new Node(match[2]);
    node.hidden = /\bhidden\b/.test(match[0]);
    node.disabled = /\bdisabled\b/.test(match[0]);
    defaults.set(node.id, { hidden: node.hidden, disabled: node.disabled, textContent: '', value: '' });
    nodes.set(node.id, node);
  }
  const requests = [];
  let role = null;
  let nextStatus = 200;
  const session = () => role ? { authenticated: true, display_name: `Local ${role}`, role, csrf_token: 'csrf', expires_in: 3600, permissions: { can_simulate: role === 'planner', can_run_drill: role === 'planner', can_demo_release: role === 'planner' } } : { authenticated: false };
  const fetcher = async (url, options) => {
    requests.push({ url, options });
    let data;
    let status = 200;
    if (url.endsWith('/auth/config')) data = { enabled: true, mode: 'local_demo' };
    else if (url.endsWith('/auth/login')) { role = JSON.parse(options.body).role; data = session(); }
    else if (url.endsWith('/auth/logout')) { role = null; data = { authenticated: false }; }
    else if (url.endsWith('/auth/session')) data = session();
    else if (url.endsWith('/health')) data = { mode: 'local' };
    else { status = nextStatus === 200 ? 404 : nextStatus; data = { error: 'No published result exists yet for this selection' }; }
    return { ok: status < 400, status, json: async () => data };
  };
  const windows = new Map();
  const timerCallbacks = new Map();
  let nextTimer = 0;
  const location = { origin: 'http://127.0.0.1:8000', pathname: '/', search: '', hash: '#forecast', assign() {} };
  const context = vm.createContext({ console, URL, URLSearchParams, TextEncoder, Uint8Array, AbortController, btoa, Date, Intl, Number, Map, Set, Promise, Object, String, JSON, Error, setTimeout: (callback) => { timerCallbacks.set(++nextTimer, callback); return nextTimer; }, clearTimeout: (id) => timerCallbacks.delete(id), cancelAnimationFrame() {}, requestAnimationFrame() { return 1; }, ResizeObserver: class { observe() {} disconnect() {} }, location, history: { replaceState() {} }, document: { getElementById: (id) => nodes.get(id), createElement: () => new Node(), createTextNode: (text) => Object.assign(new Node(), { textContent: text }), createElementNS: () => new Node(), querySelectorAll: (selector) => selector === '.nav-button' ? nav : selector === '.view-panel' ? ['view-forecast', 'view-replenishment', 'view-models', 'view-operations'].map((id) => nodes.get(id)) : [], title: '' }, window: { fetch: fetcher, crypto: webcrypto, location, history: { replaceState() {} }, sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} }, addEventListener: (name, handler) => windows.set(name, handler) } });
  vm.runInContext(`${authSource.replace('export function', 'function')}\n${appSource.replace(/^import[^\n]+\n/, '')}`, context);
  return { nodes, requests, timerCallbacks, context, windows, run: (source) => vm.runInContext(source, context), setStatus: (status) => { nextStatus = status; } };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('signed-out page never requests or exposes planning data', async () => {
  const page = browser(); await settle();
  assert.equal(page.nodes.get('workspace').hidden, true);
  assert.equal(page.nodes.get('signin-screen').hidden, false);
  assert.deepEqual(page.requests.map((request) => request.url), ['/api/auth/config', '/api/auth/session']);
  page.run("switchView('operations'); loadForecast(); loadOperations(); simulate(); runReleaseDemo(); runRecoveryDrill();");
  await settle();
  assert.equal(page.requests.length, 2);
});

test('viewer can read and sees clear disabled planner actions', async () => {
  const page = browser(); await settle();
  await page.run("signIn('viewer')"); await settle();
  assert.equal(page.nodes.get('workspace').hidden, false);
  assert.equal(page.nodes.get('access-role').textContent, 'Viewer · Preview');
  assert.equal(page.nodes.get('simulation-permission-note').hidden, false);
  assert.equal(page.nodes.get('release-permission-note').hidden, false);
  assert.equal(page.nodes.get('simulate-button').disabled, true);
  assert.equal(page.nodes.get('release-demo-button').disabled, true);
  page.run("state.forecast = {}; state.operations = {mode:'local',capabilities:{can_run_drill:true}}; setDrillAvailability(); simulate(); runReleaseDemo(); runRecoveryDrill();");
  await settle();
  assert.equal(page.nodes.get('operations-drill-button').disabled, true);
  assert.equal(page.nodes.get('drill-permission-note').hidden, false);
  assert.equal(page.requests.filter((request) => request.options.method === 'POST').length, 1);
  assert.ok(page.requests.some((request) => request.url === '/api/catalog'));
});

test('planner permissions enable local actions without a frontend role override', async () => {
  const page = browser(); await settle();
  await page.run("signIn('planner')"); await settle();
  page.run("state.forecast = {}; state.operations = {mode:'local',capabilities:{can_run_drill:true}}; resetSimulation(); setDrillAvailability();");
  assert.equal(page.nodes.get('access-role').textContent, 'Planner · Preview');
  assert.equal(page.nodes.get('simulate-button').disabled, false);
  assert.equal(page.nodes.get('release-demo-button').disabled, false);
  assert.equal(page.nodes.get('operations-drill-button').disabled, false);
  assert.equal(page.nodes.get('simulation-permission-note').hidden, true);
});

test('sign-out clears sensitive DOM, cached records, selectors and request generations', async () => {
  const page = browser(); await settle();
  await page.run("signIn('planner')"); await settle();
  const sensitive = ['selected-product-name', 'kpi-total', 'forecast-chart', 'release-list', 'operations-last-success', 'operations-drill-proof', 'policy-table-body', 'product-select'];
  for (const id of sensitive) { page.nodes.get(id).textContent = 'private record'; page.nodes.get(id).children.push('private record'); }
  page.run("state.catalog = {secret:'rawdata'}; state.forecast = {secret:'forecast'}; state.simulation = {secret:'simulation'}; state.operations = {secret:'audit'};");
  await page.run('signOut()');
  assert.equal(page.nodes.get('workspace').hidden, true);
  for (const id of sensitive) { assert.equal(page.nodes.get(id).textContent, ''); assert.equal(page.nodes.get(id).children.length, 0); }
  assert.equal(page.run('[state.catalog,state.forecast,state.simulation,state.operations].every(value => value === null)'), true);
  assert.equal(page.run('auth.getSession().authenticated'), false);
  assert.ok(page.run('state.forecastRequest > 1 && state.operationsRequest > 1 && state.releaseRequest > 1'));
});

test('scheduled expiry hides the workspace and browser-history restoration requires sign-in', async () => {
  const page = browser(); await settle();
  await page.run("signIn('planner')"); await settle();
  const callback = [...page.timerCallbacks.values()].at(-1);
  callback();
  assert.equal(page.nodes.get('workspace').hidden, true);
  assert.match(page.nodes.get('signin-error').textContent, /session has ended/);
  page.windows.get('pageshow')({ persisted: true });
  assert.equal(page.nodes.get('workspace').hidden, true);
  assert.match(page.nodes.get('signin-error').textContent, /Sign in again/);
});

test('a 401 while loading clears all data instead of exposing an error over stale content', async () => {
  const page = browser(); await settle();
  await page.run("signIn('planner')"); await settle();
  page.setStatus(401);
  await page.run('loadOperations()');
  assert.equal(page.nodes.get('workspace').hidden, true);
  assert.equal(page.run('state.operations'), null);
  assert.equal(page.nodes.get('global-error').hidden, true);
  assert.match(page.nodes.get('signin-error').textContent, /session has ended/);
});
