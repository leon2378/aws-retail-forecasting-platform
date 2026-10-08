import { createAuthClient } from './auth.js';

const $ = (id) => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';
const COLORS = { actual: '#91a49d', forecast: '#286b5d', baseline: '#d59460', band: '#dcebdc', grid: '#e9eee5', text: '#92a08c' };
const state = { catalog: null, forecast: null, mode: null, view: 'forecast', cutoff: null, forecastRequest: 0, simulationRequest: 0, simulation: null, releasesLoaded: false, releaseRequest: 0, chartIndex: null, operations: null, operationsRequest: 0, operationsLoading: false, drillRunning: false };
const number = (value, digits = 0) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : new Intl.NumberFormat('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(Number(value));
const percent = (value, digits = 1) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : `${number(Number(value) * 100, digits)}%`;
const money = (value) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : `$${number(value, 2)}`;
const validNumber = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
const parseDate = (value) => new Date(`${String(value).slice(0, 10)}T00:00:00Z`);
const isoDate = (date) => date.toISOString().slice(0, 10);
const formatDate = (value, year = false) => parseDate(value).toLocaleDateString('en-AU', { day: 'numeric', month: 'short', ...(year ? { year: 'numeric' } : {}), timeZone: 'UTC' });
const setText = (id, value) => { $(id).textContent = value; };
const announce = (value) => setText('announcement', value);
const initialMain = $('main').cloneNode(true);
let expiryTimer;
let resizeObserver;
let authBusy = false;
const auth = createAuthClient({ onExpired: () => showSignedOut('Your session has ended. Sign in again.') });
const signedIn = () => auth.getSession().authenticated === true;
const allowed = (permission) => signedIn() && auth.getSession().permissions?.[permission] === true;
const sessionChannel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('supplysight.access') : null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svgElement(tag, attributes = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, String(value)));
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  return auth.request(path, options);
}

function currentSelection() {
  return { store_id: $('store-select').value, item_id: $('product-select').value, cutoff: state.cutoff, model: $('model-select').value };
}

function cutoffDate(cutoff) {
  const date = parseDate(state.catalog.start_date);
  date.setUTCDate(date.getUTCDate() + cutoff - 1);
  return isoDate(date);
}

function replayBounds() {
  const fallbackMax = state.catalog?.mode === 'aws' ? state.catalog?.default_cutoff : (state.catalog?.total_days || 0) - 28;
  return {
    min: state.forecast?.replay?.min_cutoff ?? state.catalog?.min_cutoff ?? 196,
    max: state.forecast?.replay?.max_cutoff ?? fallbackMax,
  };
}

function setLoading(loading, message = 'Updating the forecast and backtests…') {
  $('loading-state').hidden = !loading;
  setText('loading-text', message);
  $('data-content').hidden = loading || !state.forecast;
  ['store-select', 'product-select', 'model-select', 'replay-date'].forEach((id) => { $(id).disabled = loading || !state.catalog; });
  const bounds = replayBounds();
  $('replay-prev').disabled = loading || !state.catalog || state.cutoff <= bounds.min;
  $('replay-next').disabled = loading || !state.catalog || state.cutoff >= bounds.max || state.forecast?.replay?.can_advance === false;
  $('simulate-button').disabled = loading || !state.forecast || !allowed('can_simulate');
  $('assumptions-form').querySelectorAll('input').forEach((input) => { input.disabled = loading || !state.forecast || !allowed('can_simulate'); });
  $('data-content').setAttribute('aria-busy', loading ? 'true' : 'false');
}

function showError(error) {
  if (!signedIn() || error.name === 'AbortError') return;
  setText('global-error-text', error.message || String(error));
  $('global-error').hidden = false;
  announce(`Unable to load forecast: ${error.message || error}`);
}

function populateSelect(id, options, selectedValue) {
  const select = $(id);
  select.replaceChildren(...options.map(({ value, label, disabled }) => {
    const option = element('option', null, label);
    option.value = value;
    option.disabled = !!disabled;
    return option;
  }));
  if (selectedValue && options.some((option) => option.value === selectedValue && !option.disabled)) select.value = selectedValue;
}

function populateProducts() {
  const previous = $('product-select').value;
  const products = state.catalog.products.filter((product) => product.store_id === $('store-select').value);
  if (!products.length) throw new Error('This store has no products available. Import or publish a product series before planning.');
  populateSelect('product-select', products.map((product) => ({ value: product.item_id, label: product.name || product.item_id })), previous);
}

async function boot() {
  if (!signedIn()) return;
  const sessionGeneration = auth.getGeneration();
  $('global-error').hidden = true;
  $('awaiting-publication').hidden = true;
  state.releasesLoaded = false;
  ++state.releaseRequest;
  setLoading(true, 'Preparing your planning workspace…');
  try {
    const health = await api('/api/health');
    if (sessionGeneration !== auth.getGeneration() || !signedIn()) return;
    updateRuntime(health.mode);
    const catalog = await api('/api/catalog');
    if (sessionGeneration !== auth.getGeneration() || !signedIn()) return;
    if (!catalog.stores?.length || !catalog.products?.length) throw new Error('There are no sales series available. Import the M5 dataset or start the local synthetic demo.');
    state.catalog = catalog;
    state.cutoff = catalog.default_cutoff;
    populateSelect('store-select', catalog.stores.map((store) => ({ value: store.id, label: store.name || store.id })));
    populateProducts();
    const available = catalog.models?.filter((model) => model.available) || [];
    if (!available.length) throw new Error('No forecast models are currently available. Check the application setup.');
    populateSelect('model-select', catalog.models.map((model) => ({ value: model.id, label: `${model.name}${model.available ? '' : ' (unavailable)'}`, disabled: !model.available })), available.some((model) => model.id === 'seasonal') ? 'seasonal' : available[0].id);
    $('replay-date').min = cutoffDate(catalog.min_cutoff || 196);
    $('replay-date').max = cutoffDate(catalog.mode === 'aws' ? catalog.default_cutoff : catalog.total_days - 28);
    updateSource(catalog);
    updateRuntime(catalog.mode);
    await loadForecast();
  } catch (error) {
    if (sessionGeneration !== auth.getGeneration() || !signedIn()) return;
    state.forecast = null;
    state.catalog = null;
    setLoading(false);
    if (state.mode === 'aws' && error.status === 404 && error.message === 'No published result exists yet for this selection') {
      $('awaiting-publication').hidden = false;
      ['store-select', 'product-select', 'model-select'].forEach((id) => populateSelect(id, [{ value: '', label: 'Awaiting first forecast' }]));
      const badge = $('source-badge');
      badge.replaceChildren(element('span'), document.createTextNode('Awaiting published data'));
      badge.classList.remove('real-data');
      badge.title = 'No forecast batch has been published to this AWS workspace yet.';
      setText('footer-source', 'AWS workspace · Awaiting first forecast');
      announce('AWS workspace connected. Awaiting the first published forecast.');
    } else showError(error);
    switchView(state.view, false);
  }
}

function updateRuntime(mode) {
  state.mode = mode;
  const aws = mode === 'aws';
  setText('runtime-label', aws ? 'AWS deployment' : mode === 'local' ? 'Local development' : 'Connection pending');
  const button = $('release-demo-button');
  button.disabled = aws || mode !== 'local' || !allowed('can_demo_release');
  button.textContent = aws ? 'Managed by SageMaker' : 'Run release demo ↗';
  button.title = aws ? 'Model releases on AWS are managed by the authenticated SageMaker workflow.' : !allowed('can_demo_release') ? 'Planner access is required to run the local release demonstration.' : '';
  $('release-permission-note').hidden = aws || allowed('can_demo_release');
  setText('release-heading', aws ? 'Model release history' : 'Release gate demonstration');
  setText('release-description', aws ? 'Release decisions recorded by the forecasting workflow.' : 'A visible audit trail from a rejected candidate to a passing candidate.');
  setText('release-disclaimer', aws ? 'Only completed workflow decisions appear here. An approved model has passed its release checks; approval alone does not establish improved accuracy.' : 'This illustrative workflow uses the current dataset with a deliberately degraded candidate and a baseline clone. It does not promote a trained model or establish forecast improvement.');
}

function updateSource(data) {
  const synthetic = data.source === 'synthetic';
  const m5 = data.source === 'm5';
  const badge = $('source-badge');
  badge.replaceChildren(element('span'), document.createTextNode(synthetic ? 'Synthetic demo data' : m5 ? 'M5 historical sales' : 'Data source pending'));
  badge.classList.toggle('real-data', m5);
  badge.title = data.source_label || (synthetic ? 'Generated demonstration data, not the M5 dataset.' : m5 ? 'Imported M5 historical sales data.' : 'The data source has not been verified.');
  setText('footer-source', synthetic ? 'Synthetic data · For demonstration only' : m5 ? 'M5 sales data · Historical replay' : 'Historical replay workspace');
}

async function loadForecast() {
  if (!signedIn() || !state.catalog) return;
  const request = ++state.forecastRequest;
  ++state.simulationRequest;
  state.simulation = null;
  $('global-error').hidden = true;
  $('replay-date').value = cutoffDate(state.cutoff);
  setLoading(true);
  try {
    const selection = currentSelection();
    const query = new URLSearchParams({ store: selection.store_id, item: selection.item_id, cutoff: String(selection.cutoff), model: selection.model });
    const data = await api(`/api/forecast?${query}`);
    if (request !== state.forecastRequest) return;
    if (!data.forecast?.length || !data.history?.length) throw new Error('No forecast is available for this selection. Try another product or replay date.');
    state.forecast = data;
    $('replay-date').min = cutoffDate(replayBounds().min);
    $('replay-date').max = cutoffDate(replayBounds().max);
    state.chartIndex = null;
    updateSource(data);
    renderForecast(data);
    renderModelLab(data);
    resetSimulation();
    setLoading(false);
    switchView(state.view, false);
    renderChart();
    announce(`Forecast loaded for ${$('product-select').selectedOptions[0]?.textContent}, through ${formatDate(data.as_of, true)}. Predicted demand: ${number(data.summary.total_demand)} units.`);
  } catch (error) {
    if (request !== state.forecastRequest) return;
    state.forecast = null;
    setLoading(false);
    showError(error);
  }
}

function renderForecast(data) {
  const product = state.catalog.products.find((row) => row.store_id === data.store_id && row.item_id === data.item_id);
  setText('selected-product-name', product?.name || data.item_id);
  setText('selected-category', (product?.category || '').replaceAll('_', ' '));
  $('selected-category').hidden = !product?.category;
  setText('as-of-label', `Observed through ${formatDate(data.as_of, true)}`);
  setText('kpi-total', number(data.summary.total_demand));
  setText('kpi-average', number(data.summary.average_daily, 1));
  setText('kpi-peak', `Peak day: ${number(data.summary.peak_demand, 1)} units`);
  setText('kpi-wape', percent(data.metrics.wape));
  setText('kpi-coverage', percent(data.metrics.coverage));
  const change = data.summary.change_percent;
  const foot = $('kpi-change');
  foot.replaceChildren();
  if (validNumber(change)) {
    foot.append(element('span', 'positive', `${change >= 0 ? '↗ +' : '↘ '}${percent(change)}`), document.createTextNode(' vs. prior 28 days'));
  } else foot.textContent = 'Across the next 28 days';
  setText('forecast-range', `${formatDate(data.forecast[0].date)} – ${formatDate(data.forecast.at(-1).date, true)}`);
  renderSparkline(data.forecast.map((row) => row.p50));
}

function renderSparkline(values) {
  const host = $('demand-spark');
  const svg = svgElement('svg', { viewBox: '0 0 80 30' });
  const min = Math.min(...values), max = Math.max(...values);
  const coordinates = values.map((value, index) => `${index * 80 / Math.max(values.length - 1, 1)},${26 - (value - min) / Math.max(max - min, 1) * 22}`).join(' ');
  svg.append(svgElement('polyline', { points: coordinates, fill: 'none', stroke: '#82aa83', 'stroke-width': 1.6, 'stroke-linejoin': 'round' }));
  host.replaceChildren(svg);
}

function niceMaximum(value) {
  if (value <= 0) return 4;
  const scale = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / scale * 2) / 2 * scale;
}

function renderChart() {
  const data = state.forecast;
  if (!data || state.view !== 'forecast' || $('data-content').hidden) return;
  const svg = $('forecast-chart');
  const width = Math.max($('forecast-chart-container').clientWidth, 300);
  const narrow = width < 600;
  const height = narrow ? 270 : 315;
  const margin = { top: 34, right: narrow ? 12 : 24, bottom: 43, left: narrow ? 36 : 45 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const history = data.history.slice(-28);
  const points = [...history.map((row) => ({ ...row, type: 'actual' })), ...data.forecast.map((row) => ({ ...row, type: 'forecast' }))];
  const max = niceMaximum(Math.max(...points.map((row) => Number(row.actual ?? row.p90) || 0), ...data.forecast.map((row) => Number(row.baseline) || 0)) * 1.13);
  const x = (index) => margin.left + index / (points.length - 1) * plotWidth;
  const y = (value) => margin.top + (1 - Math.max(Number(value) || 0, 0) / max) * plotHeight;
  const cutoffX = (x(history.length - 1) + x(history.length)) / 2;
  const linePath = (rows, key, offset = 0) => rows.map((row, index) => `${index ? 'L' : 'M'}${x(index + offset).toFixed(2)},${y(row[key]).toFixed(2)}`).join(' ');
  svg.replaceChildren();
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('tabindex', '0');
  svg.setAttribute('aria-label', `Demand chart: 28 observed days and 28 forecast days for ${data.item_id}. Forecast total ${number(data.summary.total_demand)} units. Use left and right arrow keys to inspect daily values.`);
  svg.append(svgElement('title', {}, 'Daily demand: observed sales and next 28 days of forecast'));
  svg.append(svgElement('rect', { x: cutoffX, y: margin.top - 4, width: width - margin.right - cutoffX, height: plotHeight + 4, fill: '#f6f9f1' }));
  for (let i = 0; i <= 4; i++) {
    const value = max * i / 4;
    const yy = y(value);
    svg.append(svgElement('line', { x1: margin.left, y1: yy, x2: width - margin.right, y2: yy, stroke: COLORS.grid, 'stroke-width': 1, 'stroke-dasharray': i ? '3 5' : 'none' }));
    svg.append(svgElement('text', { x: margin.left - 12, y: yy + 3.5, 'text-anchor': 'end', fill: COLORS.text, 'font-size': 9 }, number(value, max < 8 ? 1 : 0)));
  }
  const areaTop = data.forecast.map((row, index) => `${x(index + history.length)},${y(row.p90)}`);
  const areaBottom = [...data.forecast].reverse().map((row, index) => `${x(points.length - 1 - index)},${y(row.p10)}`);
  svg.append(svgElement('polygon', { points: [...areaTop, ...areaBottom].join(' '), fill: COLORS.band, opacity: .8 }));
  svg.append(svgElement('path', { d: linePath(history, 'actual'), fill: 'none', stroke: COLORS.actual, 'stroke-width': 1.8, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  if ($('baseline-toggle').checked) svg.append(svgElement('path', { d: linePath(data.forecast, 'baseline', history.length), fill: 'none', stroke: COLORS.baseline, 'stroke-width': 1.7, 'stroke-dasharray': '5 4', 'stroke-linejoin': 'round' }));
  const forecastPath = `M${x(history.length - 1)},${y(history.at(-1).actual)} L${x(history.length)},${y(data.forecast[0].p50)} ` + linePath(data.forecast, 'p50', history.length).replace(/^M/, 'L');
  svg.append(svgElement('path', { d: forecastPath, fill: 'none', stroke: COLORS.forecast, 'stroke-width': 2.1, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  svg.append(svgElement('line', { x1: cutoffX, y1: margin.top - 4, x2: cutoffX, y2: height - margin.bottom + 2, stroke: '#9baf8f', 'stroke-width': 1, 'stroke-dasharray': '4 4' }));
  svg.append(svgElement('rect', { x: cutoffX - 26, y: 9, width: 52, height: 18, rx: 4, fill: '#edf3e4', stroke: '#dce7d1' }));
  svg.append(svgElement('text', { x: cutoffX, y: 21, 'text-anchor': 'middle', fill: '#738867', 'font-size': 8, 'font-weight': 500 }, 'AS OF'));
  if (!narrow) svg.append(svgElement('text', { x: width - margin.right - 4, y: 22, 'text-anchor': 'end', fill: '#8c9d81', 'font-size': 8, 'letter-spacing': 1.2 }, '28-DAY FORECAST'));
  const ticks = narrow ? 4 : 7;
  for (let i = 0; i < ticks; i++) {
    const index = Math.round(i * (points.length - 1) / (ticks - 1));
    svg.append(svgElement('text', { x: x(index), y: height - 17, 'text-anchor': i === 0 ? 'start' : i === ticks - 1 ? 'end' : 'middle', fill: COLORS.text, 'font-size': 9 }, formatDate(points[index].date)));
  }
  const hover = svgElement('g', { visibility: 'hidden', 'pointer-events': 'none' });
  const hoverLine = svgElement('line', { y1: margin.top, y2: height - margin.bottom, stroke: '#92a68b', 'stroke-dasharray': '3 3' });
  const hoverDot = svgElement('circle', { r: 4, stroke: 'white', 'stroke-width': 2, fill: COLORS.forecast });
  hover.append(hoverLine, hoverDot);
  svg.append(hover);
  const tooltip = $('forecast-tooltip');
  const showPoint = (index, keyboard = false) => {
    state.chartIndex = Math.max(0, Math.min(points.length - 1, index));
    const point = points[state.chartIndex];
    const xx = x(state.chartIndex), yy = y(point.actual ?? point.p50);
    hover.setAttribute('visibility', 'visible');
    hoverLine.setAttribute('x1', xx); hoverLine.setAttribute('x2', xx);
    hoverDot.setAttribute('cx', xx); hoverDot.setAttribute('cy', yy);
    tooltip.replaceChildren(element('strong', null, formatDate(point.date, true)));
    const values = point.type === 'actual' ? [['Actual sales', `${number(point.actual)} units`]] : [['Expected demand', `${number(point.p50, 1)} units`], ['Approx. 80% band', `${number(point.p10, 1)} – ${number(point.p90, 1)}`], ['Seasonal baseline', `${number(point.baseline, 1)} units`]];
    values.forEach(([label, value]) => { const row = element('div', 'tooltip-row'); row.append(element('span', null, label), element('span', null, value)); tooltip.append(row); });
    tooltip.hidden = false;
    const host = $('forecast-chart-container');
    tooltip.style.left = `${Math.max(4, Math.min(xx + 12, host.clientWidth - tooltip.offsetWidth - 4))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(yy - 20, host.clientHeight - tooltip.offsetHeight - 5))}px`;
    if (keyboard) announce(`${formatDate(point.date, true)}. ${values.map(([label, value]) => `${label}: ${value}`).join('. ')}`);
  };
  const hidePoint = () => { hover.setAttribute('visibility', 'hidden'); tooltip.hidden = true; };
  svg.onpointermove = (event) => {
    const bounds = svg.getBoundingClientRect();
    const xx = (event.clientX - bounds.left) * width / bounds.width;
    showPoint(Math.round((xx - margin.left) / plotWidth * (points.length - 1)));
  };
  svg.onpointerleave = hidePoint;
  svg.onfocus = () => showPoint(state.chartIndex ?? history.length, true);
  svg.onblur = hidePoint;
  svg.onkeydown = (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Escape') { hidePoint(); return; }
    const index = state.chartIndex ?? history.length;
    showPoint(event.key === 'Home' ? 0 : event.key === 'End' ? points.length - 1 : index + (event.key === 'ArrowRight' ? 1 : -1), true);
  };
}

function resetSimulation() {
  $('simulation-content').hidden = true;
  $('policy-table-panel').hidden = true;
  $('simulation-error').hidden = true;
  $('simulation-loading').hidden = false;
  setText('simulation-loading', 'Choose your assumptions and run a comparison.');
  $('simulate-button').disabled = !state.forecast || !allowed('can_simulate');
  if (!allowed('can_simulate')) setText('simulation-loading', 'Viewer access lets you review forecasts. Planner access is required to run policy comparisons.');
}

async function simulate(event) {
  event?.preventDefault();
  if (!allowed('can_simulate') || !state.forecast || $('data-content').hidden || !$('assumptions-form').reportValidity()) return;
  const request = ++state.simulationRequest;
  const assumptions = Object.fromEntries([...new FormData($('assumptions-form'))].map(([key, value]) => [key, Number(value)]));
  $('simulation-error').hidden = true;
  $('simulation-content').hidden = true;
  $('policy-table-panel').hidden = true;
  $('simulation-loading').hidden = false;
  setText('simulation-loading', 'Comparing ordering policies against historical demand…');
  $('simulate-button').disabled = true;
  try {
    const data = await api('/api/simulate', { method: 'POST', body: JSON.stringify({ ...currentSelection(), assumptions }) });
    if (request !== state.simulationRequest) return;
    state.simulation = data;
    renderSimulation(data);
    $('simulation-content').hidden = false;
    $('policy-table-panel').hidden = false;
    $('simulation-loading').hidden = true;
    announce('Replenishment policy simulation complete. Results are simulated, not realized savings.');
  } catch (error) {
    if (request !== state.simulationRequest) return;
    setText('simulation-error', error.message);
    $('simulation-error').hidden = false;
    $('simulation-loading').hidden = true;
  } finally {
    if (request === state.simulationRequest) $('simulate-button').disabled = !allowed('can_simulate');
  }
}

function renderSimulation(data) {
  const policies = data.policies || [];
  if (!policies.length) throw new Error('The simulation did not return any policy results.');
  const best = policies.reduce((winner, policy) => policy.total_cost < winner.total_cost ? policy : winner);
  const comparedPolicy = policies.find((policy) => policy.id === data.savings?.policy_id);
  const summary = $('simulation-summary');
  const description = element('div');
  description.append(element('h3', null, 'Simulated cost difference'), element('p', null, `${comparedPolicy?.name || 'Lowest-cost forecast policy'} vs. fixed ordering. Selected after scoring; a negative value means higher cost.`));
  const saving = element('div', 'simulation-saving', money(data.savings?.amount));
  saving.append(element('small', null, `${percent(data.savings?.percent)} of fixed-policy cost`));
  summary.replaceChildren(description, saving);
  const maxCost = Math.max(...policies.map((policy) => policy.total_cost), 1);
  const bars = $('cost-bars');
  bars.replaceChildren();
  policies.forEach((policy) => {
    const row = element('div', 'cost-row');
    const heading = element('div', 'cost-row-heading');
    heading.append(element('span', null, policy.name), element('strong', null, money(policy.total_cost)));
    const track = element('div', 'bar-track');
    track.setAttribute('role', 'img');
    track.setAttribute('aria-label', `${policy.name}: holding ${money(policy.holding_cost)}, lost sales ${money(policy.stockout_cost)}, ordering ${money(policy.ordering_cost)}.`);
    [['holding_cost', 'var(--teal)'], ['stockout_cost', 'var(--orange)'], ['ordering_cost', '#a9bdc0']].forEach(([key, color]) => {
      const segment = element('div');
      segment.style.width = `${Math.max(0, Number(policy[key]) || 0) / maxCost * 100}%`;
      segment.style.background = color;
      track.append(segment);
    });
    row.append(heading, track); bars.append(row);
  });
  $('policy-table-body').replaceChildren(...policies.map((policy) => {
    const row = element('tr');
    const name = element('td', null, policy.name);
    if (policy.id === best.id) name.append(element('span', 'policy-name-tag', 'LOWEST COST'));
    row.append(name, ...[percent(policy.fill_rate), money(policy.total_cost), number(policy.units_ordered), money(policy.procurement_cost), number(policy.ending_inventory), number(policy.stockout_days)].map((value) => element('td', null, value)));
    return row;
  }));
  const note = $('simulation-label');
  note.replaceChildren(element('strong', null, data.label || 'Simulation results only, not realized savings.'));
  if (data.cost_basis) note.append(element('span', 'simulation-note-line', data.cost_basis));
  (data.limitations || []).forEach((limitation) => note.append(element('span', 'simulation-note-line', limitation)));
}

function renderModelLab(data) {
  const model = state.catalog.models.find((row) => row.id === data.model);
  setText('model-name', model?.name || data.model);
  setText('model-description', data.model === 'seasonal' ? 'A weekly seasonal benchmark. Recent matching weekdays provide a transparent starting point for demand planning.' : 'A boosted-tree demand model with historical lag and calendar features, evaluated against the seasonal benchmark.');
  setText('training-through', formatDate(data.as_of, true));
  const metrics = data.metrics;
  const maxError = Math.max(Number(metrics.wape) || 0, Number(metrics.baseline_wape) || 0, .01) * 1.15;
  $('model-error-bars').replaceChildren(...[[model?.name || data.model, metrics.wape, COLORS.forecast], ['Seasonal baseline', metrics.baseline_wape, COLORS.baseline]].map(([name, value, color]) => {
    const row = element('div');
    const heading = element('div', 'cost-row-heading');
    heading.append(element('span', null, name), element('strong', null, percent(value)));
    const track = element('div', 'bar-track');
    const fill = element('div');
    fill.style.width = `${Math.min(100, (Number(value) || 0) / maxError * 100)}%`;
    fill.style.background = color;
    track.append(fill); row.append(heading, track); return row;
  }));
  setText('model-comparison-note', `Mean absolute error: ${number(metrics.mae, 1)} units. Forecast bias: ${percent(metrics.bias)}. These metrics describe historical backtests, not guaranteed future performance.`);
  setText('uncertainty-note', data.training?.uncertainty_limitations || 'Uncertainty bands are approximate, based on earlier held-out errors. Their coverage is not guaranteed.');
  const backtests = data.backtests || [];
  setText('backtest-count', `${backtests.length} CHRONOLOGICAL FOLD${backtests.length === 1 ? '' : 'S'}`);
  $('backtest-table-body').replaceChildren(...backtests.map((fold) => {
    const row = element('tr');
    row.append(...[formatDate(fold.cutoff_date, true), percent(fold.wape), percent(fold.baseline_wape), number(fold.mae, 1), percent(fold.coverage)].map((value) => element('td', null, value)));
    return row;
  }));
  if (!backtests.length) {
    const row = element('tr'); const cell = element('td', null, 'No backtest folds are available for this replay date.'); cell.colSpan = 5; row.append(cell); $('backtest-table-body').append(row);
  }
}

async function loadReleases() {
  if (!signedIn()) return;
  const request = ++state.releaseRequest;
  $('release-error').hidden = true;
  try {
    const data = await api('/api/releases');
    if (request !== state.releaseRequest) return;
    renderReleases(data.releases || []);
    state.releasesLoaded = true;
  } catch (error) {
    if (request !== state.releaseRequest) return;
    setText('release-error', error.message);
    $('release-error').hidden = false;
  }
}

function renderReleases(releases) {
  const list = $('release-list');
  if (!releases.length) {
    list.replaceChildren(element('div', 'empty-state', state.mode === 'aws' ? 'No model release decisions have been published yet.' : 'No releases yet. Run the demonstration to see a failed check followed by a passing check.'));
    return;
  }
  list.replaceChildren(...[...releases].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).map((release) => {
    const row = element('article', 'release-row');
    const rejected = String(release.status).toLowerCase() === 'rejected';
    const status = element('span', `release-status${rejected ? ' rejected' : ''}`, String(release.status).toUpperCase());
    const details = element('div', 'release-details');
    details.append(element('div', 'release-title', `${release.model || 'Model candidate'} · ${release.id}`), element('p', null, release.reason || 'Release checks completed.'));
    let timestamp = String(release.created_at || '');
    const date = new Date(timestamp);
    if (!Number.isNaN(date.valueOf())) timestamp = date.toLocaleString('en-AU', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    details.append(element('div', 'release-meta', `${timestamp}${release.synthetic_demo || release.workflow_demo ? ' · Illustrative workflow demonstration' : ''}`));
    row.append(status, details); return row;
  }));
}

async function runReleaseDemo() {
  if (!allowed('can_demo_release') || state.mode !== 'local' || $('release-demo-button').disabled) return;
  const request = ++state.releaseRequest;
  const button = $('release-demo-button');
  button.disabled = true;
  button.textContent = 'Running release checks…';
  $('release-error').hidden = true;
  try {
    const data = await api('/api/releases/demo', { method: 'POST', body: '{}' });
    if (request !== state.releaseRequest) return;
    renderReleases(data.releases || []);
    state.releasesLoaded = true;
    announce('Release demonstration complete. The audit trail shows rejected and approved illustrative candidates.');
  } catch (error) {
    if (request !== state.releaseRequest) return;
    setText('release-error', error.message);
    $('release-error').hidden = false;
  } finally {
    if (request === state.releaseRequest) {
      button.disabled = !allowed('can_demo_release');
      button.textContent = 'Run release demo ↗';
    }
  }
}

function operationsTimestamp(value) {
  if (!value) return 'Not recorded';
  const date = new Date(value);
  if (!Number.isFinite(date.valueOf())) return 'Not recorded';
  return date.toLocaleString('en-AU', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Australia/Sydney' });
}

function operationsDate(value) {
  if (!value || !Number.isFinite(parseDate(value).valueOf())) return 'Not recorded';
  return formatDate(value, true);
}

function operationsStatus(value) {
  const labels = { healthy: 'Healthy', attention: 'Needs attention', awaiting_publication: 'Awaiting publication', passed: 'Passed', failed: 'Failed', warning: 'Review', completed: 'Completed', succeeded: 'Succeeded', running: 'Running', pending: 'Pending', unknown: 'Not recorded', rejected: 'Rejected', quarantined: 'Quarantined', interrupted: 'Interrupted' };
  return { className: Object.hasOwn(labels, value) ? value : 'pending', label: labels[value] || 'Pending' };
}

function statusBadge(value) {
  const status = operationsStatus(value);
  return element('span', `operations-status ${status.className}`, status.label);
}

function setOperationsStatus(id, value) {
  const status = operationsStatus(value);
  $(id).className = `operations-status ${status.className}`;
  $(id).textContent = status.label;
}

function setDrillAvailability() {
  const data = state.operations;
  const local = data?.mode === 'local' && data?.capabilities?.can_run_drill === true;
  $('operations-drill-button').disabled = !local || !allowed('can_run_drill') || state.drillRunning || state.operationsLoading;
  $('operations-drill-button').textContent = state.drillRunning ? 'Running recovery drill…' : local ? 'Run recovery drill →' : data?.mode === 'aws' ? 'Local exercise only' : 'Recovery unavailable';
  $('operations-drill-button').title = local && !allowed('can_run_drill') ? 'Planner access is required to run recovery drills.' : '';
  if ($('drill-permission-note')) $('drill-permission-note').hidden = !local || allowed('can_run_drill');
  $('operations-refresh').disabled = state.drillRunning || state.operationsLoading;
  $('operations-retry').disabled = state.drillRunning || state.operationsLoading;
}

async function loadOperations() {
  if (!signedIn() || state.drillRunning) return;
  const request = ++state.operationsRequest;
  state.operationsLoading = true;
  state.operations = null;
  $('view-operations').setAttribute('aria-busy', 'true');
  $('operations-loading').hidden = false;
  $('operations-content').hidden = true;
  $('operations-error').hidden = true;
  $('operations-drill-error').hidden = true;
  $('operations-drill-summary').hidden = true;
  $('operations-drill-steps').hidden = true;
  $('operations-drill-proof').hidden = true;
  setDrillAvailability();
  try {
    const data = await api('/api/operations');
    if (request !== state.operationsRequest) return;
    if (!data.summary || !data.quality || !['local', 'aws'].includes(data.mode)) throw new Error('Operational status is incomplete. Please refresh when the workspace is available.');
    state.operations = data;
    renderOperations(data);
    $('operations-content').hidden = false;
    setText('operations-announcement', `Operations updated. ${operationsStatus(data.summary.status).label}. ${number(data.summary.series_count)} published series.`);
  } catch (error) {
    if (request !== state.operationsRequest) return;
    setText('operations-error-text', error.message || 'Unable to load operational status.');
    $('operations-error').hidden = false;
    setText('operations-announcement', 'Unable to load operational status. Use Try again to retry.');
  } finally {
    if (request === state.operationsRequest) {
      state.operationsLoading = false;
      $('operations-loading').hidden = true;
      $('view-operations').setAttribute('aria-busy', 'false');
      setDrillAvailability();
    }
  }
}

function renderOperations(data) {
  const summary = data.summary;
  const local = data.mode === 'local';
  setText('operations-scope', data.scope || (local ? 'LOCAL REHEARSAL' : 'AWS PUBLISHED METADATA'));
  setText('operations-source', data.source === 'm5' ? 'M5 historical sales' : data.source === 'synthetic' ? 'Synthetic demonstration data' : 'No accepted data source yet');
  $('operations-source').title = data.source_label || '';
  if (state.view === 'operations') {
    updateRuntime(data.mode);
    updateSource(data);
  }
  const awaiting = summary.status === 'awaiting_publication';
  setText('operations-health-title', awaiting ? 'Waiting for a valid publication.' : summary.status === 'healthy' ? 'A valid forecast is available.' : 'Review the latest workflow evidence.');
  setText('operations-health-description', local ? 'Evidence from the local publication and recovery workspace. AWS infrastructure and job health are not monitored here.' : 'Evidence from recorded AWS publication metadata. Unrecorded jobs and infrastructure state are outside this view.');
  setOperationsStatus('operations-status', summary.status);
  $('operations-health-title').closest('.operations-health').classList.toggle('needs-attention', summary.status === 'attention');
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  $('operations-alerts').replaceChildren(...alerts.map((alert) => {
    const node = element('div', `operations-alert ${alert.severity === 'error' ? 'error' : 'warning'}`);
    node.append(element('span', 'operations-alert-symbol', alert.severity === 'error' ? '!' : 'i'), element('p', null, alert.message || 'Review the latest recorded event.'));
    return node;
  }));
  setText('operations-last-success', operationsTimestamp(summary.last_success_at));
  setText('operations-last-success-note', summary.last_success_at ? `${local ? 'Local checkpoint' : 'Published batch'} · Sydney time` : 'No recorded publication yet');
  setText('operations-replay-progress', validNumber(summary.active_cutoff) ? `Day ${number(summary.active_cutoff)}` : 'Not published');
  const expected = validNumber(summary.expected_cutoff) ? `Expected day ${number(summary.expected_cutoff)}` : 'Expected day not recorded';
  const lag = validNumber(summary.replay_lag_days) ? Number(summary.replay_lag_days) === 0 ? 'Replay caught up' : `${number(summary.replay_lag_days)} day${Number(summary.replay_lag_days) === 1 ? '' : 's'} behind` : 'Lag not recorded';
  setText('operations-replay-note', `${expected} · ${lag}`);
  setText('operations-replay-observed', summary.active_as_of ? `Observed through ${operationsDate(summary.active_as_of)}` : 'Observation cutoff not recorded');
  $('operations-replay-progress').title = summary.active_as_of ? `Observed through ${operationsDate(summary.active_as_of)}` : '';
  setText('operations-model', summary.active_model === 'seasonal' ? 'Seasonal baseline' : summary.active_model === 'xgboost' ? 'XGBoost' : summary.active_model || 'Not published');
  setText('operations-version', summary.active_version ? `Version ${summary.active_version}` : 'No active model version');
  setText('operations-series', number(summary.series_count));
  const quality = data.quality;
  setOperationsStatus('operations-quality-status', quality.passed === true ? 'passed' : quality.passed === false ? 'failed' : 'pending');
  setText('operations-quality-meta', quality.checked_at ? `Latest snapshot check · ${operationsTimestamp(quality.checked_at)} Sydney` : 'No snapshot check recorded yet.');
  const checks = Array.isArray(quality.checks) ? quality.checks : [];
  $('operations-quality-checks').replaceChildren(...checks.map((check) => {
    const row = element('div', 'operations-check-row');
    const copy = element('div', 'operations-check-copy');
    copy.append(element('strong', null, check.label || 'Snapshot check'), element('p', null, check.detail || 'No additional detail recorded.'));
    row.append(copy, statusBadge(check.status));
    return row;
  }));
  if (!checks.length) $('operations-quality-checks').append(element('div', 'empty-state', 'No data quality checks are recorded yet.'));
  const issues = Array.isArray(quality.issues) ? quality.issues : [];
  $('operations-quality-issues').hidden = !issues.length;
  $('operations-quality-issues').replaceChildren(...issues.map((issue) => {
    const node = element('div', 'operations-quality-issue');
    node.append(element('strong', null, issue.severity === 'warning' ? 'Review' : 'Blocked'), element('p', null, issue.message || 'A snapshot issue needs review.'));
    return node;
  }));
  const hash = quality.snapshot_sha256;
  $('operations-lineage').hidden = !hash;
  setText('operations-snapshot-hash', hash || '');
  const runs = Array.isArray(data.runs) ? data.runs : [];
  const active = runs.filter((run) => ['running', 'pending'].includes(run.status)).length;
  const failed = runs.filter((run) => ['failed', 'quarantined', 'interrupted', 'rejected'].includes(run.status)).length;
  setText('operations-run-count', `${number(runs.length)} RECORDED`);
  setText('operations-runs-note', `${number(active)} active or pending · ${number(failed)} blocked or interrupted in this history`);
  $('operations-run-list').replaceChildren(...runs.map((run) => {
    const row = element('article', 'operations-run-row');
    const heading = element('div', 'operations-run-heading');
    const label = String(run.type || 'Workflow run').replaceAll('_', ' ');
    heading.append(element('strong', null, label), statusBadge(run.status));
    const stage = String(run.stage || 'Stage not recorded').replaceAll('_', ' ');
    row.append(heading, element('p', null, `${stage}${validNumber(run.cutoff) ? ` · Replay day ${number(run.cutoff)}` : ''}`));
    if (run.reason) row.append(element('p', 'operations-run-reason', run.reason));
    row.append(element('div', 'operations-run-meta', `${operationsTimestamp(run.finished_at || run.started_at)} · ${run.scope || data.scope || 'Recorded workflow'}`));
    return row;
  }));
  if (!runs.length) $('operations-run-list').append(element('div', 'empty-state', 'No workflow runs have been recorded. The last valid publication, when available, remains the planning source.'));
  setText('operations-drill-description', local ? 'This isolated local exercise blocks a bad snapshot, interrupts a candidate batch, retries it, and restores the previous valid forecast. It preserves the working dataset and does not execute AWS jobs.' : 'Recovery drills run in the local rehearsal workspace. This AWS view shows recorded publication evidence and cannot start an isolated local exercise.');
  renderDrill(data.latest_drill);
  const events = Array.isArray(data.events) ? data.events : [];
  setText('operations-event-count', `${number(events.length)} EVENT${events.length === 1 ? '' : 'S'}`);
  $('operations-event-list').replaceChildren(...events.map((event) => {
    const row = element('article', 'operations-event-row');
    const copy = element('div', 'operations-event-copy');
    copy.append(element('strong', null, String(event.type || 'Workflow event').replaceAll('_', ' ')), element('p', null, event.message || 'Operational event recorded.'));
    row.append(element('span', 'operations-event-dot'), copy, element('time', 'operations-event-time', operationsTimestamp(event.at)));
    return row;
  }));
  if (!events.length) $('operations-event-list').append(element('div', 'empty-state', 'No operational events are recorded yet.'));
  const limitations = Array.isArray(data.limitations) ? data.limitations : [];
  $('operations-limitations').hidden = !limitations.length;
  $('operations-limitations').replaceChildren(...limitations.map((limitation) => element('p', null, limitation)));
  setDrillAvailability();
}

function renderDrill(drill) {
  const recorded = !!drill;
  $('operations-drill-empty').hidden = recorded;
  $('operations-drill-summary').hidden = !recorded;
  $('operations-drill-steps').hidden = !recorded;
  $('operations-drill-proof').hidden = !recorded;
  $('operations-drill-steps').replaceChildren();
  $('operations-drill-proof').replaceChildren();
  $('operations-drill-empty').querySelector('strong').textContent = 'No recovery drill recorded yet.';
  $('operations-drill-empty').querySelector('p').textContent = 'Run the isolated exercise to collect its step-by-step audit trail and checks.';
  if (!drill) return;
  setOperationsStatus('operations-drill-status', drill.status);
  setText('operations-recovery-time', validNumber(drill.recovery_seconds) ? `${number(drill.recovery_seconds, 2)}s recorded recovery` : 'Recovery time not recorded');
  setText('operations-drill-time', `${operationsTimestamp(drill.finished_at || drill.started_at)} Sydney · ${drill.scope || 'Local rehearsal'}`);
  const steps = Array.isArray(drill.steps) ? drill.steps : [];
  $('operations-drill-steps').replaceChildren(...steps.map((step, index) => {
    const row = element('li', 'operations-drill-step');
    const copy = element('div', 'operations-drill-step-copy');
    const title = element('div', 'operations-drill-step-title');
    title.append(element('strong', null, step.label || `Step ${index + 1}`), statusBadge(step.status));
    copy.append(title, element('p', null, step.detail || 'No step detail recorded.'));
    row.append(element('span', 'operations-step-number', String(index + 1)), copy);
    return row;
  }));
  const proof = drill.proof || {};
  const proofs = [['invalid_snapshot_blocked', 'Invalid snapshot blocked'], ['interrupted_batch_hidden', 'Incomplete batch hidden'], ['retry_idempotent', 'Retry kept one publication'], ['rollback_restored', 'Previous forecast restored']];
  $('operations-drill-proof').replaceChildren(...proofs.map(([key, label]) => {
    const node = element('div', 'operations-proof');
    node.append(element('span', null, label), statusBadge(proof[key] === true ? 'passed' : proof[key] === false ? 'failed' : 'pending'));
    return node;
  }));
}

async function runRecoveryDrill() {
  if (!allowed('can_run_drill') || state.operations?.mode !== 'local' || state.operations?.capabilities?.can_run_drill !== true || $('operations-drill-button').disabled) return;
  const request = ++state.operationsRequest;
  state.drillRunning = true;
  $('operations-drill-error').hidden = true;
  $('operations-drill-progress').hidden = false;
  setText('operations-drill-progress', 'The isolated exercise is running. Recovery evidence will appear after the recorded checks finish.');
  renderDrill(null);
  $('operations-drill-empty').querySelector('strong').textContent = 'Recovery exercise in progress.';
  $('operations-drill-empty').querySelector('p').textContent = 'The recorded results will appear when the isolated checks finish.';
  $('operations-drill-button').closest('.operations-recovery').setAttribute('aria-busy', 'true');
  setDrillAvailability();
  try {
    const data = await api('/api/operations/drill', { method: 'POST', body: '{}' });
    if (request !== state.operationsRequest) return;
    if (!data.summary || !data.latest_drill || data.mode !== 'local') throw new Error('The recovery result is incomplete. Refresh operational status to check the recorded outcome.');
    state.operations = data;
    renderOperations(data);
    setText('operations-announcement', `Recovery drill ${operationsStatus(data.latest_drill.status).label.toLowerCase()}. Review the recorded steps and recovery evidence.`);
  } catch (error) {
    if (request !== state.operationsRequest) return;
    state.operations = null;
    setText('operations-drill-error', `${error.message || 'Unable to complete the drill.'} Refresh status to check whether a result was recorded.`);
    $('operations-drill-error').hidden = false;
    $('operations-drill-empty').hidden = false;
    $('operations-drill-empty').querySelector('strong').textContent = 'Recovery result not confirmed.';
    $('operations-drill-empty').querySelector('p').textContent = 'Refresh status to retrieve the latest recorded outcome before running another exercise.';
    setText('operations-announcement', 'The recovery response could not be confirmed. Refresh status before running another drill.');
  } finally {
    if (request === state.operationsRequest) {
      state.drillRunning = false;
      $('operations-drill-progress').hidden = true;
      $('operations-drill-button').closest('.operations-recovery').setAttribute('aria-busy', 'false');
      setDrillAvailability();
    }
  }
}

function switchView(view, updateHash = true) {
  if (!signedIn()) return;
  const views = {
    forecast: ['Demand forecast', 'See what’s ahead. Keep the right products on your shelves.'],
    replenishment: ['Replenishment planner', 'Turn demand into decisions. Find the policy that fits your store.'],
    models: ['Model lab', 'Understand the evidence behind every forecast and release.'],
    operations: ['Operations', 'Know what is published. Catch bad data. Practice the recovery.'],
  };
  if (!views[view]) view = 'forecast';
  const enteredOperations = view === 'operations' && state.view !== 'operations';
  state.view = view;
  $('main').classList.toggle('operations-active', view === 'operations');
  document.querySelectorAll('.view-panel').forEach((panel) => { panel.hidden = panel.id !== `view-${view}`; });
  document.querySelectorAll('.nav-button').forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle('active', selected);
    if (selected) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  setText('page-title', views[view][0]);
  setText('page-description', views[view][1]);
  setText('breadcrumb-current', view === 'replenishment' ? 'Replenishment' : views[view][0]);
  document.title = `SupplySight · ${views[view][0]}`;
  if (updateHash) history.replaceState(null, '', `#${view}`);
  if (view === 'forecast') renderChart();
  if (view === 'replenishment' && state.forecast && !state.simulation && !$('simulate-button').disabled) simulate();
  const canLoadReleases = state.mode === 'aws' || (state.catalog && state.mode === 'local');
  $('release-panel').hidden = view !== 'models' || !canLoadReleases;
  if (view === 'models' && !state.releasesLoaded && canLoadReleases) loadReleases();
  if (enteredOperations) loadOperations();
}

function bindWorkspaceEvents() {
$('open-replenishment').addEventListener('click', () => { switchView('replenishment'); $('page-title').scrollIntoView({ block: 'start', behavior: 'smooth' }); });
$('open-models').addEventListener('click', () => { switchView('models'); $('page-title').scrollIntoView({ block: 'start', behavior: 'smooth' }); });
$('store-select').addEventListener('change', () => { try { populateProducts(); loadForecast(); } catch (error) { showError(error); } });
$('product-select').addEventListener('change', loadForecast);
$('model-select').addEventListener('change', loadForecast);
$('baseline-toggle').addEventListener('change', renderChart);
$('replay-prev').addEventListener('click', () => { if (state.cutoff > replayBounds().min) { state.cutoff--; loadForecast(); } });
$('replay-next').addEventListener('click', () => { if (state.cutoff < replayBounds().max && state.forecast?.replay?.can_advance !== false) { state.cutoff = state.forecast?.replay?.next_cutoff ?? state.cutoff + 1; loadForecast(); } });
$('replay-date').addEventListener('change', () => {
  if (!$('replay-date').value) { $('replay-date').value = cutoffDate(state.cutoff); return; }
  const cutoff = Math.round((parseDate($('replay-date').value) - parseDate(state.catalog.start_date)) / 86400000) + 1;
  const { min, max } = replayBounds();
  if (!Number.isFinite(cutoff) || cutoff < min || cutoff > max) {
    showError(new Error(`Choose a replay date between ${formatDate(cutoffDate(min), true)} and ${formatDate(cutoffDate(max), true)}. A complete 28-day historical holdout is required.`));
    $('replay-date').value = cutoffDate(state.cutoff);
    return;
  }
  state.cutoff = cutoff;
  loadForecast();
});
$('retry-button').addEventListener('click', () => state.catalog ? loadForecast() : boot());
$('check-publication-button').addEventListener('click', boot);
$('assumptions-form').addEventListener('submit', simulate);
$('assumptions-form').addEventListener('input', () => {
  ++state.simulationRequest;
  state.simulation = null;
  resetSimulation();
  setText('simulation-loading', 'Assumptions changed. Run the comparison to update your results.');
});
$('release-demo-button').addEventListener('click', runReleaseDemo);
$('operations-refresh').addEventListener('click', loadOperations);
$('operations-retry').addEventListener('click', loadOperations);
$('operations-drill-button').addEventListener('click', runRecoveryDrill);
  let resizeFrame;
  resizeObserver = new ResizeObserver(() => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(renderChart); });
  resizeObserver.observe($('forecast-chart-container'));
}

function resetWorkspace() {
  clearTimeout(expiryTimer);
  $('workspace').hidden = true;
  ++state.forecastRequest;
  ++state.simulationRequest;
  ++state.releaseRequest;
  ++state.operationsRequest;
  Object.assign(state, { catalog: null, forecast: null, mode: null, cutoff: null, simulation: null, releasesLoaded: false, chartIndex: null, operations: null, operationsLoading: false, drillRunning: false, view: 'forecast' });
  resizeObserver?.disconnect();
  $('main').replaceWith(initialMain.cloneNode(true));
  bindWorkspaceEvents();
  $('source-badge').replaceChildren(element('span'), document.createTextNode('Access pending'));
  $('source-badge').classList.remove('real-data');
  $('source-badge').removeAttribute('title');
  setText('profile-name', 'Signed out');
  setText('profile-role', 'Access pending');
  setText('profile-avatar', '—');
  setText('top-avatar', '—');
  $('top-avatar').setAttribute('aria-label', 'Signed out');
  setText('access-role', 'Access pending');
  setText('runtime-label', 'Connecting…');
  setText('breadcrumb-current', 'Demand forecast');
  announce('');
  document.title = 'SupplySight · Sign in';
}

function updateSigninScreen(message = '') {
  const config = auth.getConfig();
  const local = config?.mode === 'local_demo';
  $('signin-screen').hidden = false;
  $('local-signin').hidden = !local;
  $('signin-cloud').hidden = !config || local;
  $('signin-retry').hidden = !!config;
  $('signin-clear').hidden = !config || local || !message.includes('not assigned');
  $('signin-error').hidden = !message;
  setText('signin-error', message);
  setText('signin-mode', local ? 'LOCAL ACCESS PREVIEW' : config ? 'ACCOUNT SIGN-IN' : 'CONNECTION PENDING');
  setText('signin-description', local ? 'Choose a role to explore how access works. No account or password is required for this local preview.' : config ? 'Sign in with your account to open the planning workspace. Access is assigned by your workspace administrator.' : 'Connect to your workspace to check how to sign in.');
  $('signin-progress').hidden = !authBusy;
  ['signin-viewer', 'signin-planner', 'signin-cloud', 'signin-retry', 'signin-clear'].forEach((id) => { $(id).disabled = authBusy; });
}

function showSignedOut(message = '') {
  auth.clear();
  resetWorkspace();
  updateSigninScreen(message);
}

function scheduleExpiry() {
  clearTimeout(expiryTimer);
  const expires = auth.getExpiresAt();
  if (expires > 0) expiryTimer = setTimeout(() => showSignedOut('Your session has ended. Sign in again.'), Math.max(1, expires - Date.now()));
}

function showWorkspace() {
  const session = auth.getSession();
  if (!session.authenticated) { showSignedOut(); return; }
  resetWorkspace();
  const role = session.role === 'planner' ? 'Planner' : 'Viewer';
  const local = auth.getConfig()?.mode === 'local_demo';
  const name = session.display_name;
  const initials = name.split(/\s+/).filter(Boolean).map((part) => part[0]).slice(0, 2).join('').toUpperCase() || role[0];
  setText('profile-name', name);
  setText('profile-role', `${role}${local ? ' · Local preview' : ''}`);
  setText('profile-avatar', initials);
  setText('top-avatar', initials);
  $('top-avatar').setAttribute('aria-label', `${name}, ${role}`);
  setText('access-role', `${role}${local ? ' · Preview' : ''}`);
  $('simulation-permission-note').hidden = allowed('can_simulate');
  const note = element('p', 'permission-note', 'Planner access is required to run recovery drills.');
  note.id = 'drill-permission-note';
  note.hidden = allowed('can_run_drill');
  $('operations-drill-button').insertAdjacentElement('afterend', note);
  $('signin-screen').hidden = true;
  $('workspace').hidden = false;
  scheduleExpiry();
  switchView(location.hash.slice(1) || 'forecast', false);
  boot();
}

async function initializeAccess() {
  if (authBusy) return;
  authBusy = true;
  resetWorkspace();
  updateSigninScreen();
  try {
    const session = await auth.initialize();
    if (session.authenticated) showWorkspace();
    else updateSigninScreen();
  } catch (error) {
    if (error.name !== 'AbortError') updateSigninScreen(error.status === 403 ? 'Access is not assigned. Ask your workspace administrator to add you as a Viewer or Planner.' : error.message);
  } finally { authBusy = false; updateSigninControls(); }
}

function updateSigninControls() {
  $('signin-progress').hidden = !authBusy;
  ['signin-viewer', 'signin-planner', 'signin-cloud', 'signin-retry', 'signin-clear'].forEach((id) => { $(id).disabled = authBusy; });
}

async function signIn(role) {
  if (authBusy) return;
  authBusy = true;
  resetWorkspace();
  updateSigninScreen();
  try {
    const session = await auth.signIn(role);
    if (session.authenticated) {
      showWorkspace();
      sessionChannel?.postMessage({ type: 'local-role-change' });
    }
  } catch (error) { if (error.name !== 'AbortError') updateSigninScreen(error.message); }
  finally { authBusy = false; updateSigninControls(); }
}

async function signOut() {
  if (authBusy) return;
  authBusy = true;
  resetWorkspace();
  updateSigninScreen();
  let message = '';
  try { await auth.signOut(); }
  catch { message = 'The server could not confirm sign-out. Your displayed data has been cleared. Reconnect and try signing out again.'; }
  finally {
    authBusy = false;
    sessionChannel?.postMessage({ type: 'signout' });
    showSignedOut(message);
  }
}

document.querySelectorAll('.nav-button').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));
$('signin-viewer').addEventListener('click', () => signIn('viewer'));
$('signin-planner').addEventListener('click', () => signIn('planner'));
$('signin-cloud').addEventListener('click', () => signIn());
$('signin-retry').addEventListener('click', initializeAccess);
$('signin-clear').addEventListener('click', signOut);
$('signout-button').addEventListener('click', signOut);
window.addEventListener('hashchange', () => switchView(location.hash.slice(1), false));
window.addEventListener('pagehide', () => { auth.clear(); resetWorkspace(); });
window.addEventListener('pageshow', (event) => { if (event.persisted) { showSignedOut('Sign in again to reopen your workspace.'); } });
window.addEventListener('focus', async () => {
  if (!signedIn() || authBusy) return;
  const previous = JSON.stringify([auth.getSession().role, auth.getSession().permissions]);
  try {
    const session = await auth.checkSession();
    if (!session.authenticated) return;
    if (previous !== JSON.stringify([session.role, session.permissions])) showWorkspace();
    else scheduleExpiry();
  } catch (error) {
    if (error.status === 403) { showSignedOut('Access is not assigned. Ask your workspace administrator to add you as a Viewer or Planner.'); }
    else if (error.status !== 401 && error.name !== 'AbortError' && signedIn()) announce('Access could not be refreshed. Your current session will still end at its scheduled expiry.');
  }
});
if (sessionChannel) sessionChannel.onmessage = (event) => {
  if (!['signout', 'local-role-change'].includes(event.data?.type)) return;
  showSignedOut(event.data.type === 'signout' ? 'You signed out in another tab.' : 'Access changed in another tab. Confirming the current role…');
  if (event.data.type === 'local-role-change' && auth.getConfig()?.mode === 'local_demo') initializeAccess();
};
initializeAccess();
