const $ = (id) => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';
const COLORS = { actual: '#91a49d', forecast: '#286b5d', baseline: '#d59460', band: '#dcebdc', grid: '#e9eee5', text: '#92a08c' };
const state = { catalog: null, forecast: null, mode: null, view: 'forecast', cutoff: null, forecastRequest: 0, simulationRequest: 0, simulation: null, releasesLoaded: false, releaseRequest: 0, chartIndex: null };
const number = (value, digits = 0) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : new Intl.NumberFormat('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(Number(value));
const percent = (value, digits = 1) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : `${number(Number(value) * 100, digits)}%`;
const money = (value) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : `$${number(value, 2)}`;
const validNumber = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
const parseDate = (value) => new Date(`${String(value).slice(0, 10)}T00:00:00Z`);
const isoDate = (date) => date.toISOString().slice(0, 10);
const formatDate = (value, year = false) => parseDate(value).toLocaleDateString('en-AU', { day: 'numeric', month: 'short', ...(year ? { year: 'numeric' } : {}), timeZone: 'UTC' });
const setText = (id, value) => { $(id).textContent = value; };
const announce = (value) => setText('announcement', value);

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
  const response = await fetch(`${String(window.API_BASE || '').replace(/\/$/, '')}${path}`, {
    ...options,
    headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  });
  let data;
  try { data = await response.json(); } catch { throw new Error(`The server returned an unreadable response (${response.status}). Check that the API is running.`); }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status}). Please try again.`);
    error.status = response.status;
    throw error;
  }
  return data;
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
  $('simulate-button').disabled = loading || !state.forecast;
  $('assumptions-form').querySelectorAll('input').forEach((input) => { input.disabled = loading || !state.forecast; });
  $('main').setAttribute('aria-busy', loading ? 'true' : 'false');
}

function showError(error) {
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
  $('global-error').hidden = true;
  $('awaiting-publication').hidden = true;
  state.releasesLoaded = false;
  ++state.releaseRequest;
  setLoading(true, 'Preparing your planning workspace…');
  try {
    const health = await api('/api/health');
    updateRuntime(health.mode);
    const catalog = await api('/api/catalog');
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
  setText('runtime-label', aws ? 'AWS deployment' : 'Local development');
  const button = $('release-demo-button');
  button.disabled = aws || mode !== 'local';
  button.textContent = aws ? 'Managed by SageMaker' : 'Run release demo ↗';
  button.title = aws ? 'Model releases on AWS are managed by the authenticated SageMaker workflow.' : '';
  setText('release-heading', aws ? 'Model release history' : 'Release gate demonstration');
  setText('release-description', aws ? 'Release decisions recorded by the forecasting workflow.' : 'A visible audit trail from a rejected candidate to a passing candidate.');
  setText('release-disclaimer', aws ? 'Only completed workflow decisions appear here. An approved model has passed its release checks; approval alone does not establish improved accuracy.' : 'This illustrative workflow uses the current dataset with a deliberately degraded candidate and a baseline clone. It does not promote a trained model or establish forecast improvement.');
}

function updateSource(data) {
  const synthetic = data.source === 'synthetic';
  const badge = $('source-badge');
  badge.replaceChildren(element('span'), document.createTextNode(synthetic ? 'Synthetic demo data' : 'M5 historical sales'));
  badge.classList.toggle('real-data', !synthetic);
  badge.title = data.source_label || (synthetic ? 'Generated demonstration data, not the M5 dataset.' : 'Imported M5 historical sales data.');
  setText('footer-source', synthetic ? 'Synthetic data · For demonstration only' : 'M5 sales data · Historical replay');
}

async function loadForecast() {
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
  $('simulate-button').disabled = !state.forecast;
}

async function simulate(event) {
  event?.preventDefault();
  if (!state.forecast || $('data-content').hidden || !$('assumptions-form').reportValidity()) return;
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
    if (request === state.simulationRequest) $('simulate-button').disabled = false;
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
  if (state.mode !== 'local' || $('release-demo-button').disabled) return;
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
    button.disabled = false;
    button.textContent = 'Run release demo ↗';
  }
}

function switchView(view, updateHash = true) {
  const views = {
    forecast: ['Demand forecast', 'See what’s ahead. Keep the right products on your shelves.'],
    replenishment: ['Replenishment planner', 'Turn demand into decisions. Find the policy that fits your store.'],
    models: ['Model lab', 'Understand the evidence behind every forecast and release.'],
  };
  if (!views[view]) view = 'forecast';
  state.view = view;
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
  const canLoadReleases = state.mode === 'aws' || (state.catalog && !$('release-demo-button').disabled);
  $('release-panel').hidden = view !== 'models' || !canLoadReleases;
  if (view === 'models' && !state.releasesLoaded && canLoadReleases) loadReleases();
}

document.querySelectorAll('.nav-button').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));
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
window.addEventListener('hashchange', () => switchView(location.hash.slice(1), false));
let resizeFrame;
new ResizeObserver(() => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(renderChart); }).observe($('forecast-chart-container'));
switchView(location.hash.slice(1) || 'forecast', false);
boot();
