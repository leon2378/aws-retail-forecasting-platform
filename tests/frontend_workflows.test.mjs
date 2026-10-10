import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../frontend/helpers.js', import.meta.url), 'utf8');
const helpers = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const { escapeHtml, money, stockState, canCancel, canRetry, filterOrders, shouldAutoRefreshWorkspace, orderFingerprint, createSubmissionTracker } = helpers;
const appSource = await readFile(new URL('../frontend/app.js', import.meta.url), 'utf8');
const order = (extra = {}) => ({ customer: 'Alex Morgan', items: [{ sku: 'NS-001', quantity: 2 }], scenario: 'happy_path', ...extra });

test('uncertain responses keep the same idempotency key for a safe retry', () => {
  let sequence = 0;
  const tracker = createSubmissionTracker(() => `request-${++sequence}`);
  const original = tracker.prepare(order());
  tracker.markUncertain();
  assert.equal(tracker.prepare(order()).idempotency_key, original.idempotency_key);
  assert.equal(sequence, 1);
  assert.equal(tracker.isUncertain(), true);
});

test('an uncertain submission cannot silently change product quantities or scenario', () => {
  const tracker = createSubmissionTracker(() => 'request-1');
  tracker.prepare(order());
  tracker.markUncertain();
  assert.throws(() => tracker.prepare(order({ items: [{ sku: 'NS-001', quantity: 3 }] })), /Start fresh/);
  assert.throws(() => tracker.prepare(order({ scenario: 'payment_declined' })), /Start fresh/);
  assert.throws(() => tracker.prepare(order({ customer: 'Another customer' })), /Start fresh/);
  assert.equal(tracker.prepare(order()).idempotency_key, 'request-1');
});

test('an explicit fresh submission receives a new key after success or abandonment', () => {
  let sequence = 0;
  const tracker = createSubmissionTracker(() => `request-${++sequence}`);
  tracker.prepare(order()); tracker.markUncertain(); tracker.reset();
  assert.equal(tracker.isUncertain(), false);
  assert.equal(tracker.prepare(order()).idempotency_key, 'request-2');
});

test('normal form edits receive a new key before any uncertain result', () => {
  let sequence = 0;
  const tracker = createSubmissionTracker(() => `request-${++sequence}`);
  assert.equal(tracker.prepare(order()).idempotency_key, 'request-1');
  assert.equal(tracker.prepare(order({ customer: 'Casey' })).idempotency_key, 'request-2');
});

test('equivalent item ordering and trimmed names retain the original identity', () => {
  const first = order({ items: [{ sku: 'NS-002', quantity: 1 }, { sku: 'NS-001', quantity: 2 }] });
  const second = order({ customer: '  Alex Morgan  ', items: [{ sku: 'NS-001', quantity: 2 }, { sku: 'NS-002', quantity: 1 }] });
  assert.equal(orderFingerprint(first), orderFingerprint(second));
});

test('order and customer text cannot inject markup or attributes into rendered views', () => {
  assert.equal(escapeHtml('<img src=x onerror="alert(1)"> & \'customer\''), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;customer&#39;');
  assert.equal(escapeHtml(null), '');
});

test('completed and shipped orders cannot be cancelled; viewers cannot cancel any', () => {
  assert.equal(canCancel({ status: 'queued', shipment_status: 'pending' }, { can_create: true }), true);
  assert.equal(canCancel({ status: 'failed', shipment_status: 'pending' }, { can_create: true }), true);
  assert.equal(canCancel({ status: 'completed', shipment_status: 'shipped' }, { can_create: true }), false);
  assert.equal(canCancel({ status: 'processing', shipment_status: 'shipped' }, { can_create: true }), false);
  assert.equal(canCancel({ status: 'queued', shipment_status: 'pending' }, { can_create: false }), false);
});

test('stock status uses available units rather than stock already reserved', () => {
  assert.equal(stockState({ on_hand: 100, reserved: 100, available: 0, reorder_level: 10 }).tone, 'danger');
  assert.equal(stockState({ available: 10, reorder_level: 10 }).tone, 'warning');
  assert.equal(stockState({ available: 11, reorder_level: 10 }).tone, 'success');
  assert.equal(money(1250), '$12.50');
});

test('recovery is offered only for blocked fulfillment, not terminal payment declines', () => {
  assert.equal(canRetry({ status: 'failed', stage: 'fulfillment' }, { can_retry: true }), true);
  assert.equal(canRetry({ status: 'failed', stage: 'done', payment_status: 'declined' }, { can_retry: true }), false);
  assert.equal(canRetry({ status: 'processing', stage: 'fulfillment' }, { can_retry: true }), false);
  assert.equal(canRetry({ status: 'failed', stage: 'fulfillment' }, { can_retry: false }), false);
});

test('customer search and status filters combine without exposing nonmatching orders', () => {
  const orders = [{ id: 'a', reference: 'OF-0001', customer: 'Juniper Studio', status: 'completed' }, { id: 'b', reference: 'OF-0002', customer: 'Juniper Shop', status: 'queued' }, { id: 'c', reference: 'OF-0003', customer: 'River & Co.', status: 'completed' }];
  assert.deepEqual(filterOrders(orders, 'completed', '  JUNIPER  ').map((order) => order.id), ['a']);
  assert.deepEqual(filterOrders(orders, 'all', 'OF-0002').map((order) => order.id), ['b']);
  assert.equal(filterOrders(orders, 'failed').length, 0);
});

function idleWorkspace(mode = 'aws') {
  return { loaded: true, dashboard: { mode, metrics: { pending_orders: 0, queue_depth: 0 } }, operations: { mode, metrics: { pending_orders: 0, queue_depth: 0 }, outbox_pending: 0, queue: [], drills: [] }, orders: [] };
}

test('a known idle AWS workspace pauses automatic data reads, including terminal failures', () => {
  const workspace = idleWorkspace();
  assert.equal(shouldAutoRefreshWorkspace(workspace), false);
  workspace.orders = [{ status: 'completed' }, { status: 'cancelled' }, { status: 'failed' }];
  workspace.operations.drills = [{ status: 'passed' }, { status: 'failed' }];
  assert.equal(shouldAutoRefreshWorkspace(workspace), false);
});

test('an asynchronous running drill keeps AWS refresh active even with zero orders', () => {
  const workspace = idleWorkspace();
  workspace.operations.drills = [{ id: 'drill-1', status: 'running' }];
  assert.equal(shouldAutoRefreshWorkspace(workspace), true);
  workspace.operations.drills[0].status = 'passed';
  assert.equal(shouldAutoRefreshWorkspace(workspace), false);
});

test('queued orders, processing orders and pending dispatch independently keep refreshing', () => {
  for (const status of ['queued', 'processing']) {
    const workspace = idleWorkspace();
    workspace.orders = [{ status }];
    assert.equal(shouldAutoRefreshWorkspace(workspace), true, `${status} survives differently timed metric snapshots`);
  }
  for (const scope of ['dashboard', 'operations']) {
    for (const metric of ['pending_orders', 'queue_depth']) {
      const workspace = idleWorkspace();
      workspace[scope].metrics[metric] = 1;
      assert.equal(shouldAutoRefreshWorkspace(workspace), true, `${scope}.${metric}`);
    }
  }
  const queued = idleWorkspace(); queued.operations.queue = [{ status: 'pending' }];
  assert.equal(shouldAutoRefreshWorkspace(queued), true);
  const dispatch = idleWorkspace(); dispatch.operations.outbox_pending = 1;
  assert.equal(shouldAutoRefreshWorkspace(dispatch), true);
});

test('local demos and incomplete or unknown cloud activity state retain automatic refresh', () => {
  assert.equal(shouldAutoRefreshWorkspace(idleWorkspace('local_demo')), true);
  assert.equal(shouldAutoRefreshWorkspace(), true);
  assert.equal(shouldAutoRefreshWorkspace({ ...idleWorkspace(), loaded: false }), true);
  for (const scope of ['dashboard', 'operations']) {
    for (const mode of [undefined, 'unknown', 'local_demo']) {
      const workspace = idleWorkspace(); workspace[scope].mode = mode;
      assert.equal(shouldAutoRefreshWorkspace(workspace), true);
    }
    const workspace = idleWorkspace(); delete workspace[scope].metrics;
    assert.equal(shouldAutoRefreshWorkspace(workspace), true);
  }
  for (const count of [undefined, '0', -1, NaN, Infinity]) {
    const workspace = idleWorkspace(); workspace.operations.outbox_pending = count;
    assert.equal(shouldAutoRefreshWorkspace(workspace), true);
  }
  for (const field of ['queue', 'drills']) {
    const workspace = idleWorkspace(); delete workspace.operations[field];
    assert.equal(shouldAutoRefreshWorkspace(workspace), true);
  }
  assert.equal(shouldAutoRefreshWorkspace({ ...idleWorkspace(), orders: null }), true);
  const unknownOrder = idleWorkspace(); unknownOrder.orders = [{ status: 'unrecognized' }];
  assert.equal(shouldAutoRefreshWorkspace(unknownOrder), true);
  const unknownDrill = idleWorkspace(); unknownDrill.operations.drills = [{}];
  assert.equal(shouldAutoRefreshWorkspace(unknownDrill), true);
});

async function appHarness(mode = 'aws') {
  const workspace = idleWorkspace(mode);
  const requests = [], nodes = new Map();
  let interval, checks = 0;
  for (const selector of ['#page', '#toast-region', '#sign-out', '#refresh', '#environment', '#profile', '#breadcrumb', '#nav-order-count', '#nav-alert', '#sync-time']) {
    nodes.set(selector, { innerHTML: '', textContent: '', hidden: false, disabled: false, contains: () => false, setAttribute() {}, addEventListener() {} });
  }
  const session = { authenticated: true, display_name: 'Workspace operator', role: 'operator', permissions: {} };
  const auth = {
    initialize: async () => session,
    getSession: () => session,
    getConfig: () => ({ mode: mode === 'aws' ? 'cognito' : 'local_demo' }),
    getGeneration: () => 1,
    checkSession: async () => { checks += 1; return session; },
    request: async (path, options = {}) => {
      requests.push({ path, method: options.method || 'GET' });
      if (path === '/api/dashboard') return structuredClone(workspace.dashboard);
      if (path === '/api/catalog') return { products: [], currency: 'USD' };
      if (path === '/api/orders') return { orders: structuredClone(workspace.orders) };
      if (path === '/api/operations') return structuredClone(workspace.operations);
      if (options.method === 'POST') return { order: {} };
      throw new Error(`Unexpected request: ${path}`);
    },
  };
  const context = {
    ...helpers, esc: helpers.escapeHtml, createAuthClient: () => auth,
    document: { hidden: false, activeElement: null, querySelector: (selector) => nodes.get(selector) || null, querySelectorAll: () => [], addEventListener() {} },
    window: { setInterval: (callback) => { interval = callback; }, setTimeout() {}, clearTimeout() {}, addEventListener() {} },
    location: { hash: '#overview' },
    console,
  };
  const executable = appSource.replace(/^import[^\n]+\n/gm, '').replace(/start\(\);\s*$/, 'globalThis.harness = { start, state, loadWorkspace, mutate };');
  runInNewContext(executable, context, { filename: 'frontend/app.js' });
  await context.harness.start();
  requests.length = 0;
  return { ...context.harness, requests, tick: () => interval(), checks: () => checks };
}

test('idle AWS interval checks the session without reading workspace data; manual refresh and mutations still read', async () => {
  const app = await appHarness();
  await app.tick();
  assert.equal(app.checks(), 1);
  assert.equal(app.requests.length, 0);
  await app.loadWorkspace(true);
  assert.deepEqual(app.requests.map(({ path }) => path), ['/api/dashboard', '/api/catalog', '/api/orders', '/api/operations']);
  app.requests.length = 0;
  await app.mutate('/api/orders/example/cancel', {}, 'Cancelled');
  assert.equal(app.requests[0].method, 'POST');
  assert.equal(app.requests.filter(({ method }) => method === 'GET').length, 4);
});

test('AWS active work refreshes on Orders and a zero-order running drill refreshes on Recovery', async () => {
  const app = await appHarness();
  app.state.view = 'orders';
  app.state.orders = [{ status: 'queued' }];
  await app.tick();
  assert.equal(app.checks(), 1);
  assert.equal(app.requests.length, 4);
  app.requests.length = 0;
  app.state.view = 'recovery';
  app.state.operations.drills = [{ status: 'running' }];
  await app.tick();
  assert.equal(app.checks(), 2);
  assert.equal(app.requests.length, 4);
});

test('local idle overview keeps refreshing and retains its existing Orders-view pause', async () => {
  const app = await appHarness('local_demo');
  await app.tick();
  assert.equal(app.checks(), 1);
  assert.equal(app.requests.length, 4);
  app.requests.length = 0;
  app.state.view = 'orders';
  await app.tick();
  assert.equal(app.checks(), 1);
  assert.equal(app.requests.length, 0);
});
