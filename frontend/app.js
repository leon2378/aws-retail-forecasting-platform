import { createAuthClient } from './auth.js';
import { views, scenarios, escapeHtml as esc, money, dateLabel, statusLabel, stockState, canCancel, canRetry, filterOrders, shouldAutoRefreshWorkspace, createSubmissionTracker } from './helpers.js';

const $ = (selector, root = document) => root.querySelector(selector);
const page = $('#page');
const icons = {
  plus: '<path d="M12 5v14 M5 12h14"/>',
  arrow: '<path d="M5 12h14 M14 7l5 5-5 5"/>',
  check: '<path d="m5 12 4 4 10-10"/>',
  package: '<path d="m3 7 9-4 9 4v10l-9 4-9-4Z M3 7l9 4 9-4 M12 11v10 M7 5l9 4"/>',
  orders: '<path d="M6 3h12v18H6z M9 7h6 M9 11h6 M9 15h4"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/>',
  money: '<path d="M12 3v18 M17 6H9a3 3 0 0 0 0 6h6a3 3 0 0 1 0 6H6"/>',
  recovery: '<path d="M4 11a8 8 0 1 1 2 7 M4 4v7h7 M12 8v4l3 2"/>',
  close: '<path d="m6 6 12 12 M6 18 18 6"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z M8 12l3 3 5-6"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.package}</svg>`;
const badge = (status, label = statusLabel(status)) => `<span class="badge badge-${esc(String(status).replace(/[^a-z_]/g, ''))}">${esc(label)}</span>`;
const state = { dashboard: null, products: [], orders: [], operations: null, currency: 'USD', view: 'overview', loaded: false, search: '', status: 'all', busy: false, refreshPromise: null, draft: null, detail: null };
const submission = createSubmissionTracker();
const auth = createAuthClient({ onExpired: () => { closeDialogs(); state.loaded = false; state.dashboard = null; state.products = []; state.orders = []; state.operations = null; state.draft = null; submission.reset(); renderSignIn('Your session has ended. Sign in to continue.'); } });
const permissions = () => auth.getSession().permissions || {};
const localMode = () => state.dashboard?.mode !== 'aws' && state.operations?.mode !== 'aws';
const message = (error) => error?.name === 'AbortError' ? '' : (error?.message || 'Something went wrong. Please try again.');

function toast(text, error = false) {
  if (!text) return;
  $('#toast-region').innerHTML = `<div class="toast${error ? ' error' : ''}">${esc(text)}</div>`;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { $('#toast-region').textContent = ''; }, error ? 9000 : 5500);
}

function closeDialogs() { document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close()); }

function updateShell() {
  const session = auth.getSession();
  $('#sign-out').hidden = !session.authenticated;
  $('#refresh').hidden = !session.authenticated;
  $('#environment').textContent = auth.getConfig()?.mode === 'cognito' ? 'AWS WORKSPACE' : 'LOCAL DEMO';
  $('#profile').innerHTML = session.authenticated ? `<span class="profile-avatar">${esc(session.display_name.split(/\s+/).map((value) => value[0]).slice(0, 2).join('').toUpperCase())}</span><span>${esc(session.display_name)}<small>${esc(session.role)} access</small></span>` : '<span class="profile-avatar">OF</span><span>Workspace access<small>Sign in to begin</small></span>';
  const label = state.view[0].toUpperCase() + state.view.slice(1);
  $('#breadcrumb').textContent = label;
  document.title = `${label} · OrderFlow`;
  document.querySelectorAll('[data-view]').forEach((link) => { if (link.dataset.view === state.view) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); });
  $('#nav-order-count').textContent = state.dashboard?.metrics?.total_orders ?? '—';
  $('#nav-alert').hidden = !(state.dashboard?.metrics?.dead_letters > 0);
}

function renderSignIn(error = '') {
  updateShell();
  const cloud = auth.getConfig()?.mode === 'cognito';
  page.setAttribute('aria-busy', 'false');
  page.innerHTML = `<div class="signin-layout"><div class="signin-story"><div class="eyebrow">EVERY ORDER, ACCOUNTED FOR</div><h1>Good operations.<br>Great peace of mind.</h1><p>A clear view of your orders, inventory and recovery. Follow every step from checkout to fulfillment.</p><div class="signin-features"><div>${icon('shield')}Inventory protected from duplicate orders</div><div>${icon('recovery')}Failures you can inspect and recover</div><div>${icon('clock')}A complete history of every action</div></div></div><form id="signin-form" class="signin-card"><h2>Welcome to OrderFlow</h2><p>${cloud ? 'Sign in with your workspace account.' : 'Choose a role to explore the local workspace.'}</p>${cloud ? '' : `<label class="role-option"><input type="radio" name="role" value="viewer"><span><strong>Viewer</strong><small>See orders, inventory and recovery history.</small></span></label><label class="role-option"><input type="radio" name="role" value="operator" checked><span><strong>Operator</strong><small>Create orders, process work and recover failures.</small></span></label><label class="role-option"><input type="radio" name="role" value="admin"><span><strong>Administrator</strong><small>Operator actions, plus restocking and recovery drills.</small></span></label>`}<div class="field-error" id="signin-error" role="alert">${esc(error)}</div><button class="button button-primary" type="submit">${cloud ? 'Sign in securely' : 'Enter workspace'} ${icon('arrow')}</button><p class="signin-disclaimer">${cloud ? 'Your workspace administrator assigns your permissions.' : 'Local demo roles are for evaluation. Deployed accounts use secure sign-in and assigned permissions.'} Payments and shipping are simulations.</p></form></div>`;
  $('#sync-time').textContent = 'Ready when you are';
  $('#signin-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('button[type=submit]', event.currentTarget);
    button.disabled = true;
    $('#signin-error').textContent = '';
    try {
      const session = await auth.signIn(new FormData(event.currentTarget).get('role'));
      if (session.authenticated) await loadWorkspace(true);
    } catch (error) { if ($('#signin-error')) $('#signin-error').textContent = message(error); }
    finally { if (button.isConnected) button.disabled = false; }
  });
}

async function loadWorkspace(showLoading = false) {
  if (!auth.getSession().authenticated) return renderSignIn();
  if (state.refreshPromise) return state.refreshPromise;
  const generation = auth.getGeneration();
  if (showLoading && !state.loaded) { page.setAttribute('aria-busy', 'true'); page.innerHTML = '<div class="initial-loading"><span class="spinner"></span>Loading orders and inventory…</div>'; }
  state.refreshPromise = (async () => {
    try {
      const results = await Promise.all([auth.request('/api/dashboard'), auth.request('/api/catalog'), auth.request('/api/orders'), auth.request('/api/operations')]);
      if (generation !== auth.getGeneration() || !auth.getSession().authenticated) return;
      [state.dashboard, , , state.operations] = results;
      state.products = results[1].products || [];
      state.currency = results[1].currency || 'USD';
      state.orders = results[2].orders || [];
      state.loaded = true;
      render();
      $('#sync-time').textContent = `Updated ${new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' }).format(new Date())}`;
    } catch (error) {
      if (!auth.getSession().authenticated || error.name === 'AbortError') return;
      if (state.loaded) toast(`Refresh failed. Showing the last loaded data. ${message(error)}`, true);
      else { page.innerHTML = `<div class="panel error-panel"><h2>We couldn’t load the workspace</h2><p>${esc(message(error))}</p><button class="button button-primary" data-action="refresh">Try again</button></div>`; }
    } finally { page.setAttribute('aria-busy', 'false'); state.refreshPromise = null; }
  })();
  return state.refreshPromise;
}

function header(title, description, actions = '') { return `<div class="page-header"><div><div class="eyebrow">NORTHSTAR SUPPLY</div><h1>${title}</h1><p>${description}</p></div><div class="page-actions">${actions}</div></div>`; }
function newOrderButton() { return permissions().can_create ? `<button class="button button-primary" data-action="new-order">${icon('plus')} Create order</button>` : '<span class="badge">Read-only access</span>'; }
function metric(label, value, note, symbol = 'orders') { return `<div class="metric-card"><div class="metric-icon">${icon(symbol)}</div><div class="metric-label">${label}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${note}</div></div>`; }
function orderTable(orders, compact = false) {
  if (!orders.length) return `<div class="empty-state"><h3>${state.search || state.status !== 'all' ? 'No matching orders' : 'A fresh start'}</h3><p>${state.search || state.status !== 'all' ? 'Try another search or status filter.' : 'Create your first demo order to follow its journey.'}</p>${!state.search && state.status === 'all' && permissions().can_create ? '<button class="button" data-action="new-order">Create an order</button>' : ''}</div>`;
  return `<div class="table-wrap"><table><caption class="sr-only">${compact ? 'Recent orders' : 'Orders in the workspace'}</caption><thead><tr><th scope="col">Order</th><th scope="col">Customer</th><th scope="col">Status</th><th scope="col">Total</th>${compact ? '' : '<th scope="col">Created</th>'}<th scope="col"><span class="sr-only">Details</span></th></tr></thead><tbody>${orders.map((order) => `<tr><td><button class="text-button order-reference" data-action="detail" data-id="${esc(order.id)}" aria-label="Open order ${esc(order.reference || order.id)}">${esc(order.reference || order.id)}</button><small>${esc(order.items?.reduce((sum, item) => sum + item.quantity, 0) || 0)} ${(order.items?.reduce((sum, item) => sum + item.quantity, 0) || 0) === 1 ? "item" : "items"}</small></td><td>${esc(order.customer)}</td><td>${badge(order.status)}</td><td><strong>${money(order.total_cents, state.currency)}</strong></td>${compact ? '' : `<td>${dateLabel(order.created_at)}</td>`}<td><button class="text-button" data-action="detail" data-id="${esc(order.id)}" aria-label="View order ${esc(order.reference || order.id)}">View ${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>`;
}

function activityList(events) {
  if (!events.length) return '<div class="empty-state">Your workspace activity will appear here.</div>';
  return `<div class="activity-list">${events.slice(0, 5).map((event) => `<div class="activity-item"><span class="activity-icon">${icon(/fail|blocked|retry|interrupted/.test(event.type || '') ? 'recovery' : 'check')}</span><div><p>${esc(event.title || statusLabel(event.type))}</p><small>${esc(event.detail || event.actor || 'Workspace event')}</small></div><time datetime="${esc(event.at || event.created_at)}">${dateLabel(event.at || event.created_at)}</time></div>`).join('')}</div>`;
}

function overview() {
  const metrics = state.dashboard.metrics || {};
  const blocked = metrics.dead_letters || 0;
  const drill = state.operations?.drills?.[0];
  return `${header('Operations overview', 'Every order has a journey. Keep yours moving.', newOrderButton())}<section class="hero"><div><div class="eyebrow">A LITTLE CLARITY GOES A LONG WAY</div><h2>${blocked ? 'A few orders need your attention.' : 'Your next great order starts here.'}</h2><p>${blocked ? `${blocked} blocked workflow${blocked === 1 ? '' : 's'} can be inspected and safely retried from Recovery.` : 'Explore a resilient order journey, from stock reservation to simulated delivery.'}</p><a class="button button-lime" href="${blocked ? '#recovery' : '#orders'}">${blocked ? 'Review blocked orders' : 'Explore orders'} ${icon('arrow')}</a></div><div class="hero-illustration" aria-hidden="true"><div class="parcel-line"></div><div class="parcel"></div><span class="parcel-check">${icon('check')}</span></div></section><div class="metric-grid">${metric('Total orders', metrics.total_orders ?? 0, 'All orders in this workspace')}${metric('Completed orders', metrics.completed_orders ?? 0, 'Payment and fulfillment finished', 'check')}${metric('Orders in progress', metrics.pending_orders ?? 0, `${metrics.queue_depth ?? 0} work items waiting`, 'clock')}${metric('Simulated revenue', money(metrics.revenue_cents, state.currency), 'Demo payments · USD', 'money')}</div><div class="dashboard-grid"><section class="panel"><div class="panel-header"><div><h2>Recent orders</h2><p>The latest journeys through your workspace</p></div><a class="text-button" href="#orders">View all ${icon('arrow')}</a></div>${orderTable((state.dashboard.recent_orders || []).slice(0, 5), true)}</section><section class="panel"><div class="panel-header"><div><h2>Workspace activity</h2><p>A record of what happened</p></div><span class="status-dot" aria-hidden="true"></span></div>${activityList(state.dashboard.activity || [])}</section></div><div class="bottom-grid"><section class="panel"><div class="panel-header"><h2>Workflow at a glance</h2><a class="text-button" href="#recovery">Details ${icon('arrow')}</a></div><div class="health-list"><div class="health-row"><span>Reserved inventory</span><strong>${esc(metrics.reserved_units ?? 0)} units</strong></div><div class="health-row"><span>Waiting work</span><strong>${esc(metrics.queue_depth ?? 0)} items</strong></div><div class="health-row"><span>Blocked workflows</span>${badge(blocked ? 'warning' : 'success', `${blocked} needing recovery`)}</div></div></section><section class="panel"><div class="recovery-callout"><span class="recovery-symbol">${icon('shield')}</span><div><h3>Confidence comes from practice.</h3><p>${drill ? `Last drill: ${statusLabel(drill.status).toLowerCase()} · ${dateLabel(drill.at, true)}` : 'Run an isolated drill to verify duplicate protection, stock safety and recovery.'}</p><a class="text-button" href="#recovery">Explore recovery ${icon('arrow')}</a></div></div></section></div>`;
}

function ordersPage() {
  const filtered = filterOrders(state.orders, state.status, state.search);
  return `${header('Orders', 'From checkout to fulfillment, every step in view.', newOrderButton())}<section class="panel"><div class="toolbar"><input class="search-input" id="order-search" type="search" placeholder="Search order or customer…" aria-label="Search order or customer" value="${esc(state.search)}"><label><span class="sr-only">Filter orders by status</span><select class="filter-select" id="order-status">${['all', 'queued', 'processing', 'completed', 'failed', 'cancelled'].map((status) => `<option value="${status}" ${state.status === status ? 'selected' : ''}>${status === 'all' ? 'All statuses' : statusLabel(status)}</option>`).join('')}</select></label><span class="small muted" id="order-result-count">${filtered.length} ${filtered.length === 1 ? "order" : "orders"}</span></div><div id="order-results">${orderTable(filtered)}</div></section><div class="notice info section-gap">Payments and shipping are simulated. Inventory is reserved when an order is accepted and released when it is cancelled.</div>`;
}

function inventoryPage() {
  const totals = state.products.reduce((sum, product) => ({ available: sum.available + product.available, reserved: sum.reserved + product.reserved, low: sum.low + (stockState(product).tone !== 'success' ? 1 : 0) }), { available: 0, reserved: 0, low: 0 });
  return `${header('Inventory', 'Stock you can trust, down to the last unit.', permissions().can_manage_inventory ? '<span class="badge badge-success">Restocking enabled</span>' : '<span class="badge">Inventory view</span>')}<div class="stock-card-grid">${metric('Available to order', totals.available, 'On-hand units minus active reservations', 'package')}${metric('Reserved units', totals.reserved, 'Held for orders awaiting fulfillment', 'clock')}${metric('Products running low', totals.low, `Across ${state.products.length} products`, 'orders')}</div><div class="inventory-summary">${icon('shield')}Orders reserve stock atomically. Duplicate submissions reuse the original order.</div><section class="panel"><div class="panel-header"><div><h2>Product catalog</h2><p>Available stock updates as orders move forward</p></div><span class="small muted">${state.products.length} products</span></div><div class="table-wrap"><table><caption class="sr-only">Product inventory</caption><thead><tr><th scope="col">Product</th><th scope="col">Unit price</th><th scope="col">On hand</th><th scope="col">Reserved</th><th scope="col">Available</th><th scope="col">Stock status</th>${permissions().can_manage_inventory ? '<th scope="col"><span class="sr-only">Restock</span></th>' : ''}</tr></thead><tbody>${state.products.map((product) => { const stock = stockState(product); return `<tr><td><div class="product-name"><span class="product-icon" aria-hidden="true">${icon('package')}</span><span><strong>${esc(product.name)}</strong><small>${esc(product.sku)} · ${esc(product.category)}</small></span></div></td><td>${money(product.price_cents, state.currency)}</td><td>${esc(product.on_hand)}</td><td>${esc(product.reserved)}</td><td><span class="stock-number">${esc(product.available)}</span><div class="stock-bar" aria-hidden="true"><span style="width:${Math.min(100, Math.max(0, product.available / Math.max(product.on_hand, 1) * 100))}%"></span></div></td><td>${badge(stock.tone, stock.label)}</td>${permissions().can_manage_inventory ? `<td><button class="text-button" data-action="restock" data-sku="${esc(product.sku)}">Add stock</button></td>` : ''}</tr>`; }).join('')}</tbody></table></div></section>`;
}

function workItems(items, blocked = false) {
  if (!items.length) return `<div class="empty-state"><h3>${blocked ? 'Nothing blocked' : 'All caught up'}</h3><p>${blocked ? 'Work that exhausts its retries will appear here.' : 'Create an order to add work to the queue.'}</p></div>`;
  return items.map((work) => { const order = state.orders.find((value) => value.id === work.order_id); return `<div class="queue-item"><span class="activity-icon">${icon(blocked ? 'recovery' : 'clock')}</span><div class="queue-item-main"><button class="text-button" data-action="detail" data-id="${esc(work.order_id)}">${esc(order?.reference || work.order_id)}</button><p>${esc(statusLabel(work.stage))} · ${esc(work.attempts)} of ${esc(work.max_attempts)} attempts</p>${work.last_error ? `<p>${esc(work.last_error)}</p>` : ''}<small>${dateLabel(work.updated_at || work.created_at, true)}</small></div>${blocked && permissions().can_retry ? `<button class="button" data-action="retry" data-id="${esc(work.order_id)}">${icon('recovery')} Retry</button>` : badge(work.status)}</div>`; }).join('');
}

function drillResult(drill) {
  if (!drill) return '<p>No drills have run yet. Start one to collect evidence.</p>';
  return `<div class="section-gap">${badge(drill.status, `Drill ${drill.status}`)}<div class="check-list">${(drill.checks || []).map((check) => `<div class="check-item ${check.passed ? '' : 'failed'}"><span class="check-icon" aria-hidden="true">${check.passed ? '✓' : '×'}</span><div><strong>${esc(check.name)}</strong><small>${esc(check.detail)}</small></div></div>`).join('')}</div><div class="drill-meta">${dateLabel(drill.at, true)} · ${esc(drill.duration_ms ?? '—')} ms · ${esc(drill.scope || 'isolated')} workspace</div></div>`;
}

function recoveryPage() {
  const operations = state.operations || {};
  const queue = (operations.queue || []).filter((work) => work.status === 'pending');
  const blocked = operations.dead_letters || [];
  return `${header('Recovery', 'Inspect a failure. Understand it. Recover safely.', localMode() && permissions().can_retry ? `<button class="button button-primary" data-action="process" ${!queue.length ? 'disabled' : ''}>${icon('arrow')} Process work</button>` : '')}<div class="notice info">${localMode() ? 'Local worker controls advance queued steps on demand. Temporary failures retry; persistent failures move to blocked work for inspection.' : 'AWS workers process queued steps automatically. Failed work appears here after its retry allowance is exhausted.'} Payments and shipping remain simulations.</div><div class="metric-grid">${metric('Waiting work', queue.length, 'Ready for processing', 'clock')}${metric('Blocked work', blocked.length, 'Requires an operator decision', 'recovery')}${metric('Reserved units', state.dashboard.metrics?.reserved_units ?? 0, 'Stock held by active orders', 'package')}${metric('Pending dispatch', operations.outbox_pending ?? 0, 'Committed work waiting to be dispatched', 'orders')}</div><div class="queue-grid"><div><section class="panel"><div class="panel-header"><div><h2>Blocked workflows</h2><p>Review the cause before retrying</p></div>${badge(blocked.length ? 'warning' : 'success', `${blocked.length} blocked`)}</div>${workItems(blocked, true)}</section><section class="panel section-gap"><div class="panel-header"><div><h2>Work queue</h2><p>${localMode() ? 'Advance up to 20 work items per click' : 'Processed by AWS workers'}</p></div><span class="small muted">${queue.length} waiting</span></div>${workItems(queue)}</section></div><section class="panel"><div class="panel-header"><div><h2>Recovery drill</h2><p>Evidence, collected in isolation</p></div>${icon('shield')}</div><div class="drill-panel"><h3>Practice before you need it.</h3><p>The drill checks duplicate submissions, competing reservations and recovery after a worker failure. It uses a separate workspace.</p>${permissions().can_drill ? '<button class="button button-primary" data-action="drill">Run recovery drill '+icon('arrow')+'</button>' : '<span class="badge">Administrator access required</span>'}<div id="drill-result">${drillResult(operations.drills?.[0])}</div></div></section></div><section class="panel section-gap"><div class="panel-header"><h2>Operations activity</h2></div>${activityList(operations.activity || [])}</section>`;
}

function render() {
  if (!auth.getSession().authenticated) return renderSignIn();
  if (!state.loaded) return;
  const focused = document.activeElement;
  const orderField = focused && page.contains(focused) && ['order-search', 'order-status'].includes(focused.id) ? { id: focused.id, start: focused.selectionStart, end: focused.selectionEnd } : null;
  updateShell();
  page.innerHTML = ({ overview, orders: ordersPage, inventory: inventoryPage, recovery: recoveryPage })[state.view]();
  page.setAttribute('aria-busy', 'false');
  const search = $('#order-search');
  if (search) search.addEventListener('input', () => { state.search = search.value; const filtered = filterOrders(state.orders, state.status, state.search); $('#order-results').innerHTML = orderTable(filtered); $('#order-result-count').textContent = `${filtered.length} ${filtered.length === 1 ? 'order' : 'orders'}`; });
  $('#order-status')?.addEventListener('change', (event) => { state.status = event.target.value; render(); });
  if (orderField) {
    const replacement = $(`#${orderField.id}`);
    replacement?.focus({ preventScroll: true });
    if (orderField.id === 'order-search' && typeof orderField.start === 'number') replacement?.setSelectionRange(orderField.start, orderField.end);
  }
}

function dialogHeader(title, description, id) { return `<div class="dialog-header"><div><h2 id="${id}">${title}</h2><p>${description}</p></div><button class="icon-button" type="button" data-action="close-dialog" aria-label="Close dialog">${icon('close')}</button></div>`; }

function draftLine(line, index) { return `<div class="order-line"><label><span class="sr-only">Product ${index + 1}</span><select data-line-sku="${index}" required>${state.products.map((product) => `<option value="${esc(product.sku)}" ${line.sku === product.sku ? 'selected' : ''}>${esc(product.name)} · ${product.available} available</option>`).join('')}</select></label><label><span class="sr-only">Quantity for product ${index + 1}</span><input data-line-quantity="${index}" type="number" min="1" max="100" value="${esc(line.quantity)}" required></label><button class="icon-button" type="button" data-action="remove-line" data-index="${index}" aria-label="Remove product ${index + 1}" ${state.draft.items.length === 1 ? 'disabled' : ''}>${icon('close')}</button></div>`; }
function draftTotal() { return state.draft.items.reduce((total, item) => total + (state.products.find((product) => product.sku === item.sku)?.price_cents || 0) * (Number(item.quantity) || 0), 0); }

function showOrderForm() {
  if (!permissions().can_create) return;
  if (!state.products.length) return toast('The product catalog is empty.', true);
  if (!state.draft) state.draft = { customer: '', items: [{ sku: state.products[0].sku, quantity: 1 }], scenario: 'happy_path' };
  const dialog = $('#order-dialog');
  dialog.innerHTML = `<form id="create-order-form">${dialogHeader('Create an order', 'A simulated purchase, with a real workflow.', 'order-dialog-title')}<div class="dialog-body"><label class="field"><span>Customer name</span><input name="customer" id="customer" maxlength="80" autocomplete="off" placeholder="e.g. Alex Morgan" value="${esc(state.draft.customer)}" required></label><div class="field"><span>Products</span><div id="order-lines">${state.draft.items.map(draftLine).join('')}</div><button class="text-button" type="button" data-action="add-line">+ Add another product</button></div><label class="field"><span>Workflow scenario</span><select name="scenario" id="scenario">${Object.entries(scenarios).map(([value, title]) => `<option value="${value}" ${value === state.draft.scenario ? 'selected' : ''}>${title}</option>`).join('')}</select><small>Choose a scenario to explore the recovery behavior.</small></label><div class="order-total"><span>Order total · ${esc(state.currency)}</span><strong id="draft-total">${money(draftTotal(), state.currency)}</strong></div><div class="simulation-label">No real payment is taken and no shipment is sent. Submitting reserves stock in this demo workspace.</div><div class="field-error" id="order-error" role="alert"></div></div><div class="dialog-footer"><button class="button" type="button" data-action="fresh-order" ${submission.isUncertain() ? '' : 'hidden'}>Start fresh</button><button class="button" type="button" data-action="close-dialog">Cancel</button><button class="button button-primary" type="submit">${submission.isUncertain() ? 'Retry same submission' : 'Create order'} ${icon('arrow')}</button></div></form>`;
  if (!dialog.open) dialog.showModal();
  $('#create-order-form').addEventListener('input', syncDraft);
  $('#create-order-form').addEventListener('change', syncDraft);
  $('#create-order-form').addEventListener('submit', submitOrder);
}

function syncDraft() {
  if (!state.draft) return;
  state.draft.customer = $('#customer').value;
  state.draft.scenario = $('#scenario').value;
  state.draft.items = [...document.querySelectorAll('[data-line-sku]')].map((select) => ({ sku: select.value, quantity: Number($(`[data-line-quantity="${select.dataset.lineSku}"]`).value) }));
  $('#draft-total').textContent = money(draftTotal(), state.currency);
}

async function submitOrder(event) {
  event.preventDefault();
  syncDraft();
  const button = $('button[type=submit]', event.currentTarget);
  button.disabled = true;
  $('#order-error').textContent = '';
  let result;
  try {
    if (new Set(state.draft.items.map((item) => item.sku)).size !== state.draft.items.length) throw Object.assign(new Error('Each product can appear once. Update its quantity instead.'), { status: 400 });
    const payload = submission.prepare({ ...state.draft, customer: state.draft.customer.trim(), items: state.draft.items.map((item) => ({ ...item })) });
    result = await auth.request('/api/orders', { method: 'POST', body: JSON.stringify(payload) });
  } catch (error) {
    if (error.name !== 'AbortError' && (!error.status || error.status >= 500)) submission.markUncertain();
    if ($('#order-error')) $('#order-error').textContent = `${message(error)}${submission.isUncertain() ? ' Retry with the same details to retrieve the original result safely.' : ''}`;
    if (submission.isUncertain()) { $('button[data-action=fresh-order]', $('#order-dialog')).hidden = false; button.textContent = 'Retry same submission'; }
  } finally { if (button.isConnected) button.disabled = false; }
  if (!result) return;
  submission.reset(); state.draft = null;
  $('#order-dialog').close();
  toast(result.replayed ? 'Original order retrieved. No duplicate reservation was created.' : 'Order created. Inventory reserved and work queued.');
  await loadWorkspace();
  await showDetail(result.order.id);
}

async function showDetail(id) {
  try {
    const { order } = await auth.request(`/api/orders/${encodeURIComponent(id)}`);
    if (!auth.getSession().authenticated) return;
    state.detail = order;
    const dialog = $('#detail-dialog');
    dialog.innerHTML = `${dialogHeader(esc(order.reference || order.id), `${esc(order.customer)} · Created ${dateLabel(order.created_at, true)}`, 'detail-dialog-title')}<div class="dialog-body"><div class="order-overview"><div><span>Order status</span>${badge(order.status)}</div><div><span>Payment</span><strong>${esc(statusLabel(order.payment_status))}</strong></div><div><span>Shipping</span><strong>${esc(statusLabel(order.shipment_status))}</strong></div></div><h3>Order items</h3><div class="detail-items">${(order.items || []).map((item) => `<div class="detail-item"><span>${esc(item.quantity)} × ${esc(item.name || item.sku)}</span><strong>${money(item.price_cents * item.quantity, state.currency)}</strong></div>`).join('')}<div class="detail-item"><strong>Total</strong><strong>${money(order.total_cents, state.currency)}</strong></div></div>${order.last_error ? `<div class="notice">${esc(order.last_error)}</div>` : ''}<h3>Order timeline</h3><ol class="timeline">${(order.timeline || []).map((event) => `<li><strong>${esc(event.title || statusLabel(event.type))}</strong><p>${esc(event.detail || '')}</p><time datetime="${esc(event.at)}">${dateLabel(event.at, true)}${event.actor ? ` · ${esc(event.actor)}` : ''}</time></li>`).join('')}</ol><div class="simulation-label section-gap">Payment and shipping events are simulations. This timeline records actual actions in the demo workflow.</div><div class="field-error" id="detail-error" role="alert"></div></div><div class="dialog-footer">${canCancel(order, permissions()) ? `<button class="button button-danger" data-action="cancel-order" data-id="${esc(order.id)}">Cancel order</button>` : ''}${canRetry(order, permissions()) ? `<button class="button" data-action="retry" data-id="${esc(order.id)}">Retry fulfillment</button>` : ''}<button class="button button-primary" data-action="close-dialog">Done</button></div>`;
    if (!dialog.open) dialog.showModal();
  } catch (error) { toast(message(error), true); }
}

function showRestock(sku) {
  if (!permissions().can_manage_inventory) return;
  const product = state.products.find((value) => value.sku === sku);
  if (!product) return;
  const dialog = $('#restock-dialog');
  dialog.innerHTML = `<form id="restock-form">${dialogHeader('Add inventory', esc(product.name), 'restock-dialog-title')}<div class="dialog-body"><div class="notice info">${esc(product.available)} units available · ${esc(product.reserved)} reserved</div><label class="field"><span>Units to add</span><input name="quantity" type="number" value="10" min="1" max="1000" required><small>Restocking adds on-hand stock without changing reservations.</small></label><div class="field-error" id="restock-error" role="alert"></div></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dialog">Cancel</button><button type="submit" class="button button-primary">Add stock</button></div></form>`;
  dialog.showModal();
  $('#restock-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const button = $('button[type=submit]', event.currentTarget); button.disabled = true;
    try { const quantity = Number(new FormData(event.currentTarget).get('quantity')); await auth.request(`/api/inventory/${encodeURIComponent(sku)}/adjust`, { method: 'POST', body: JSON.stringify({ quantity }) }); dialog.close(); toast(`${quantity} units added to ${product.name}.`); await loadWorkspace(); }
    catch (error) { if ($('#restock-error')) $('#restock-error').textContent = message(error); }
    finally { if (button.isConnected) button.disabled = false; }
  });
}

async function mutate(path, body, success, button) {
  if (state.busy) return;
  state.busy = true; if (button) button.disabled = true;
  try { const result = await auth.request(path, { method: 'POST', body: JSON.stringify(body) }); toast(typeof success === 'function' ? success(result) : success); await loadWorkspace(); return result; }
  catch (error) { toast(message(error), true); }
  finally { state.busy = false; if (button?.isConnected) button.disabled = false; }
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const { action, id, sku } = button.dataset;
  if (action === 'refresh') return loadWorkspace(true);
  if (action === 'close-dialog') return button.closest('dialog').close();
  if (action === 'new-order') return showOrderForm();
  if (action === 'detail') return showDetail(id);
  if (action === 'restock') return showRestock(sku);
  if (action === 'add-line') { syncDraft(); if (state.draft.items.length >= 10) return toast('An order can contain up to 10 products.', true); state.draft.items.push({ sku: state.products.find((product) => !state.draft.items.some((item) => item.sku === product.sku))?.sku || state.products[0].sku, quantity: 1 }); return showOrderForm(); }
  if (action === 'remove-line') { syncDraft(); state.draft.items.splice(Number(button.dataset.index), 1); return showOrderForm(); }
  if (action === 'fresh-order') { if (!window.confirm('The earlier order may already exist. Review the Orders page before creating another order. Start a new submission?')) return; submission.reset(); state.draft = null; return showOrderForm(); }
  if (action === 'process') return mutate('/api/operations/process', { limit: 20 }, (result) => `Processed ${result.processed} work item${result.processed === 1 ? '' : 's'}. Review the updated timeline.`, button);
  if (action === 'retry') { const result = await mutate(`/api/operations/retry/${encodeURIComponent(id)}`, {}, 'Workflow queued for a safe retry.', button); if (result && $('#detail-dialog').open) await showDetail(id); return; }
  if (action === 'cancel-order') { if (!window.confirm('Cancel this order and release its reserved stock?')) return; const result = await mutate(`/api/orders/${encodeURIComponent(id)}/cancel`, {}, 'Order cancelled. Reserved stock released.', button); if (result) await showDetail(id); return; }
  if (action === 'drill') return mutate('/api/operations/drill', {}, (result) => result.drill ? `Recovery drill ${result.drill.status}. Review the checks below.` : 'Recovery drill started. Results will appear when it finishes.', button);
});

$('#refresh').addEventListener('click', () => loadWorkspace(true));
$('#sign-out').addEventListener('click', async (event) => { event.currentTarget.disabled = true; try { await auth.signOut(); } catch (error) { toast(message(error), true); } finally { closeDialogs(); submission.reset(); state.draft = null; state.loaded = false; state.dashboard = null; state.products = []; state.orders = []; state.operations = null; renderSignIn(); $('#sign-out').disabled = false; } });
window.addEventListener('hashchange', () => { state.view = views.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview'; if (auth.getSession().authenticated) render(); else updateShell(); });
window.setInterval(async () => {
  if (document.hidden || state.busy || document.querySelector('dialog[open]') || !auth.getSession().authenticated || (state.view === 'orders' && auth.getConfig()?.mode !== 'cognito')) return;
  try {
    const generation = auth.getGeneration();
    await auth.checkSession();
    if (!auth.getSession().authenticated) return;
    if (shouldAutoRefreshWorkspace(state)) await loadWorkspace();
    else if (generation !== auth.getGeneration()) render();
  } catch (error) { if (error.status !== 401) toast(message(error), true); }
}, 15000);

async function start() {
  state.view = views.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  try { const session = await auth.initialize(); if (session.authenticated) await loadWorkspace(true); else renderSignIn(); }
  catch (error) { updateShell(); page.setAttribute('aria-busy', 'false'); page.innerHTML = `<div class="panel error-panel"><h2>Workspace connection unavailable</h2><p>${esc(message(error))}</p><button class="button button-primary" id="reconnect">Reconnect</button></div>`; $('#reconnect').addEventListener('click', start); }
}
start();
