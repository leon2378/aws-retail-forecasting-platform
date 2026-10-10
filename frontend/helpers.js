export const views = ['overview', 'orders', 'inventory', 'recovery'];
export const scenarios = { happy_path: 'Successful order', payment_declined: 'Payment declined', fulfillment_retry: 'Temporary fulfillment failure', fulfillment_dead_letter: 'Persistent fulfillment failure' };
export function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]); }
export function money(cents, currency = 'USD') { return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format((Number(cents) || 0) / 100); }
export function dateLabel(value, time = false) { const date = new Date(value); if (Number.isNaN(date.getTime())) return 'Not recorded'; return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', ...(time ? { hour: 'numeric', minute: '2-digit' } : {}) }).format(date); }
export function statusLabel(status) { return ({ queued: 'Queued', processing: 'Processing', completed: 'Completed', cancelled: 'Cancelled', failed: 'Needs attention', pending: 'Pending', dead_letter: 'Blocked', done: 'Done', paid: 'Paid', captured: 'Captured', refunded: 'Refunded', declined: 'Declined', shipped: 'Shipped', not_started: 'Not started' })[status] || String(status || 'Pending').replaceAll('_', ' '); }
export function stockState(product) { const available = Number(product.available) || 0; if (available <= 0) return { label: 'Out of stock', tone: 'danger' }; if (available <= Number(product.reorder_level || 0)) return { label: 'Running low', tone: 'warning' }; return { label: 'Healthy stock', tone: 'success' }; }
export function canCancel(order, permissions) { return permissions.can_create === true && ['queued', 'processing', 'failed'].includes(order.status) && order.shipment_status !== 'shipped'; }
export function canRetry(order, permissions) { return permissions.can_retry === true && order.status === 'failed' && order.stage === 'fulfillment'; }
export function filterOrders(orders, status = 'all', search = '') { const term = search.trim().toLowerCase(); return orders.filter((order) => (status === 'all' || order.status === status) && `${order.reference || ''} ${order.customer || ''} ${order.id || ''}`.toLowerCase().includes(term)); }
export function shouldAutoRefreshWorkspace({ loaded = false, dashboard, operations, orders } = {}) {
  if (!loaded || dashboard?.mode !== 'aws' || operations?.mode !== 'aws') return true;
  const counts = [dashboard.metrics?.pending_orders, dashboard.metrics?.queue_depth, operations.metrics?.pending_orders, operations.metrics?.queue_depth, operations.outbox_pending];
  if (counts.some((count) => !Number.isSafeInteger(count) || count < 0) || !Array.isArray(orders) || !Array.isArray(operations.queue) || !Array.isArray(operations.drills)) return true;
  return counts.some((count) => count > 0) || operations.queue.length > 0
    || orders.some((order) => !['completed', 'cancelled', 'failed'].includes(order?.status))
    || operations.drills.some((drill) => !['passed', 'failed'].includes(drill?.status));
}
export function orderFingerprint(input) { return JSON.stringify({ customer: String(input.customer || '').trim(), items: input.items.map(({ sku, quantity }) => ({ sku, quantity: Number(quantity) })).sort((a, b) => a.sku.localeCompare(b.sku)), scenario: input.scenario }); }
export function createSubmissionTracker(makeKey = () => crypto.randomUUID()) {
  let key = null, fingerprint = null, uncertain = false;
  return { prepare(input) { const next = orderFingerprint(input); if (uncertain && fingerprint !== next) throw new Error('An earlier submission is uncertain. Restore its details or choose Start fresh before changing the order.'); if (!key || fingerprint !== next) { key = makeKey(); fingerprint = next; } return { ...input, idempotency_key: key }; }, markUncertain() { uncertain = true; }, reset() { key = null; fingerprint = null; uncertain = false; }, isUncertain() { return uncertain; } };
}
