#!/usr/bin/env node

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class ClassList {
  constructor() { this.values = new Set(); }
  toggle(name, enabled) {
    if (enabled) this.values.add(name);
    else this.values.delete(name);
  }
  contains(name) { return this.values.has(name); }
}

class Button {
  constructor(dataset) {
    this.dataset = dataset;
    this.classList = new ClassList();
    this.disabled = false;
    this.attributes = {};
    this.listeners = {};
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
}

function element() {
  return {
    textContent: '', className: '', hidden: false, attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    replaceChildren() {}, appendChild() {}, append() {},
  };
}

const modeButtons = ['pool', 'spa', 'spillover'].map(mode => new Button({mode}));
const speedButtons = ['speed1', 'speed2', 'speed3', 'speed4'].map(speed => new Button({speed}));
const switchButtons = ['auto_heat', 'heater_relay', 'lights', 'blower']
  .map(control => new Button({switch: control}));
const navButtons = ['menu', 'plus', 'minus', 'left', 'right'].map(k => new Button({k}));
const releaseButton = new Button({speedClear: 'true'});
const pumpOnButton = new Button({pumpOn: 'true'});
const temperatureButtons = ['pool', 'spa'].map(body => new Button({temperatureBody: body}));
const temperatureAdjustButtons = ['minus', 'plus'].map(value => new Button({temperatureAdjust: value}));
const temperatureConfirmButton = new Button({temperatureConfirm: 'true'});
const temperatureCancelButton = new Button({temperatureCancel: 'true'});
const panel = element();
const elements = {
  display: element(), status: element(), 'cache-state': element(),
  'default-menu-rows': element(), 'equipment-state': element(),
  'control-lock': element(),
  'temperature-editor': element(), 'temperature-value': element(),
};

const documentMock = {
  documentElement: {style: {setProperty() {}}},
  getElementById(id) { return elements[id]; },
  querySelector(selector) {
    if (selector === '.panel') return panel;
    if (selector === 'button[data-speed-clear]') return releaseButton;
    if (selector === 'button[data-pump-on]') return pumpOnButton;
    if (selector === 'button[data-temperature-confirm]') return temperatureConfirmButton;
    if (selector === 'button[data-temperature-cancel]') return temperatureCancelButton;
    throw new Error(`unexpected selector: ${selector}`);
  },
  querySelectorAll(selector) {
    if (selector === 'button[data-mode]') return modeButtons;
    if (selector === 'button[data-speed]') return speedButtons;
    if (selector === 'button[data-switch]') return switchButtons;
    if (selector === 'button[data-k]') return navButtons;
    if (selector === 'button[data-temperature-body]') return temperatureButtons;
    if (selector === 'button[data-temperature-adjust]') return temperatureAdjustButtons;
    throw new Error(`unexpected selector: ${selector}`);
  },
  createElement() { return element(); },
  createDocumentFragment() { return element(); },
};

const context = vm.createContext({
  console,
  Date,
  Math,
  document: documentMock,
  window: {innerHeight: 720, addEventListener() {}},
  fetch: () => new Promise(() => {}),
  setTimeout() {},
  clearTimeout() {},
});

const html = fs.readFileSync(path.join(__dirname, 'aqualogic_mqtt/static/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
vm.runInContext(script, context);

function state(overrides = {}) {
  return {
    available: true,
    mode: 'pool',
    target_mode: null,
    busy: false,
    phase: 'complete',
    service_mode: false,
    auto_heat: false,
    heater_relay: true,
    lights: false,
    blower: false,
    controls_locked: false,
    filter_on: true,
    heater_targets: {
      available: true, busy: false, phase: 'complete', minimum_f: 65, maximum_f: 104,
      targets: {pool: 85, spa: 102}, known: {pool: true, spa: true},
      observed_at_utc_by_body: {pool: '2026-06-29T12:00:00Z', spa: '2026-06-29T12:00:00Z'},
    },
    vsp: {enabled: true, busy: true, phase: 'holding', target_name: 'speed1'},
    automation: {
      enabled: true,
      pool_heat_enabled: false,
      manual_override: null,
      desired: {pump_preset: 'speed1'},
    },
    ...overrides,
  };
}

function render(value) {
  context.__state = value;
  vm.runInContext('renderEquipment(__state)', context);
}

function expectButton(button, {active, pending, disabled}) {
  assert.strictEqual(button.classList.contains('active'), active);
  assert.strictEqual(button.classList.contains('pending'), pending);
  assert.strictEqual(button.disabled, disabled);
}

function setPending(value) {
  context.__pending = value;
  vm.runInContext('pendingControl = __pending', context);
}

render(state());
expectButton(modeButtons[0], {active: true, pending: false, disabled: false});
expectButton(speedButtons[0], {active: true, pending: false, disabled: false});
expectButton(pumpOnButton, {active: true, pending: false, disabled: false});
assert.strictEqual(temperatureButtons[0].textContent, 'Pool 85°F');
assert.strictEqual(temperatureButtons[1].textContent, 'Spa 102°F');

setPending({kind: 'mode', target: 'spa', accepted: true, startedAt: Date.now()});
render(state());
expectButton(modeButtons[0], {active: false, pending: false, disabled: false});
expectButton(modeButtons[1], {active: false, pending: true, disabled: true});
expectButton(navButtons[0], {active: false, pending: false, disabled: false});
assert.strictEqual(elements['control-lock'].hidden, true);
render(state({mode: 'spa'}));
expectButton(modeButtons[1], {active: true, pending: false, disabled: false});

setPending({kind: 'speed', target: 'speed2', accepted: true, startedAt: Date.now()});
render(state({mode: 'spa'}));
expectButton(speedButtons[0], {active: false, pending: false, disabled: true});
expectButton(speedButtons[1], {active: false, pending: true, disabled: true});
expectButton(navButtons[0], {active: false, pending: false, disabled: true});
assert.strictEqual(elements['control-lock'].hidden, false);
render(state({mode: 'spa', vsp: {enabled: true, busy: true, phase: 'holding', target_name: 'speed2'}}));
expectButton(speedButtons[1], {active: true, pending: false, disabled: false});

setPending({kind: 'speed', target: 'speed1', accepted: true, startedAt: Date.now()});
render(state({
  vsp: {
    enabled: true, busy: false, phase: 'observed', target_name: 'speed1',
    requested_speed_pct: 70, verified: true,
  }
}));
expectButton(speedButtons[0], {active: true, pending: false, disabled: false});

const manualSpeedState = state({
  mode: 'spa',
  vsp: {enabled: true, busy: true, phase: 'holding', target_name: 'speed2'},
  automation: {
    enabled: true,
    pool_heat_enabled: false,
    manual_override: {pump_preset: 'speed2'},
    desired: {pump_preset: 'speed2'},
  },
});
setPending({kind: 'resume-schedule', accepted: false, startedAt: Date.now()});
render(manualSpeedState);
expectButton(speedButtons[1], {active: true, pending: false, disabled: false});
expectButton(releaseButton, {active: false, pending: true, disabled: true});
setPending({kind: 'resume-schedule', accepted: true, startedAt: Date.now()});
render(state({mode: 'spa'}));
expectButton(speedButtons[0], {active: true, pending: false, disabled: false});
expectButton(releaseButton, {active: false, pending: false, disabled: true});
assert.strictEqual(releaseButton.attributes['aria-pressed'], 'false');

setPending({kind: 'switch', control: 'lights', target: true, accepted: true, startedAt: Date.now()});
render(state({mode: 'spa'}));
expectButton(switchButtons[2], {active: false, pending: true, disabled: true});
expectButton(switchButtons[3], {active: false, pending: false, disabled: false});
expectButton(navButtons[0], {active: false, pending: false, disabled: false});
assert.strictEqual(elements['control-lock'].hidden, true);
render(state({mode: 'spa', lights: true}));
expectButton(switchButtons[2], {active: true, pending: false, disabled: false});

setPending({kind: 'switch', control: 'filter', target: false, accepted: true, startedAt: Date.now()});
render(state({mode: 'spa'}));
expectButton(pumpOnButton, {active: false, pending: true, disabled: true});
render(state({mode: 'spa', filter_on: false}));
expectButton(pumpOnButton, {active: false, pending: false, disabled: false});

setPending({kind: 'temperature', body: 'spa', target_f: 103, accepted: true, startedAt: Date.now()});
render(state());
expectButton(temperatureButtons[1], {active: false, pending: true, disabled: true});
expectButton(navButtons[0], {active: false, pending: false, disabled: true});
render(state({heater_targets: {
  available: true, busy: false, phase: 'complete', minimum_f: 65, maximum_f: 104,
  targets: {pool: 85, spa: 103}, known: {pool: true, spa: true},
  observed_at_utc_by_body: {pool: '2026-06-29T12:00:00Z', spa: '2026-06-29T12:00:00Z'},
}}));
expectButton(temperatureButtons[1], {active: false, pending: false, disabled: false});

render(state({heater_targets: {
  available: true, busy: false, phase: 'complete', minimum_f: 65, maximum_f: 104,
  targets: {pool: null, spa: null}, known: {pool: true, spa: true},
  observed_at_utc_by_body: {pool: '2026-06-29T12:00:00Z', spa: '2026-06-29T12:00:00Z'},
}}));
assert.strictEqual(temperatureButtons[0].textContent, 'Pool Off');
assert.strictEqual(temperatureButtons[1].textContent, 'Spa Off');
temperatureButtons[0].listeners.click();
expectButton(temperatureButtons[0], {active: false, pending: true, disabled: true});
const scanStartedAt = Date.now();
setPending({
  kind: 'temperature-scan', body: 'pool', accepted: true,
  startedAt: scanStartedAt, operationId: 'scan-pool-1',
});
render(state({heater_targets: {
  available: true, busy: false, phase: 'complete', operation_id: 'scan-pool-1',
  minimum_f: 65, maximum_f: 104,
  targets: {pool: null, spa: null}, known: {pool: true, spa: true},
  observed_at_utc_by_body: {
    pool: new Date(scanStartedAt + 1000).toISOString(),
    spa: '2026-06-29T12:00:00Z',
  },
}}));
assert.strictEqual(elements['temperature-editor'].hidden, false);
assert.strictEqual(elements['temperature-value'].textContent, '65°F');

console.log('WebUI pending-state tests passed.');
