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
const panel = element();
const elements = {
  display: element(), status: element(), 'cache-state': element(),
  'default-menu-rows': element(), 'equipment-state': element(),
  'control-lock': element(),
};

const documentMock = {
  documentElement: {style: {setProperty() {}}},
  getElementById(id) { return elements[id]; },
  querySelector(selector) {
    if (selector === '.panel') return panel;
    if (selector === 'button[data-speed-clear]') return releaseButton;
    throw new Error(`unexpected selector: ${selector}`);
  },
  querySelectorAll(selector) {
    if (selector === 'button[data-mode]') return modeButtons;
    if (selector === 'button[data-speed]') return speedButtons;
    if (selector === 'button[data-switch]') return switchButtons;
    if (selector === 'button[data-k]') return navButtons;
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

setPending({kind: 'mode', target: 'spa', accepted: true, startedAt: Date.now()});
render(state());
expectButton(modeButtons[0], {active: false, pending: false, disabled: true});
expectButton(modeButtons[1], {active: false, pending: true, disabled: true});
assert.strictEqual(elements['control-lock'].hidden, false);
render(state({mode: 'spa'}));
expectButton(modeButtons[1], {active: true, pending: false, disabled: false});

setPending({kind: 'speed', target: 'speed2', accepted: true, startedAt: Date.now()});
render(state({mode: 'spa'}));
expectButton(speedButtons[0], {active: false, pending: false, disabled: true});
expectButton(speedButtons[1], {active: false, pending: true, disabled: true});
render(state({mode: 'spa', vsp: {enabled: true, busy: true, phase: 'holding', target_name: 'speed2'}}));
expectButton(speedButtons[1], {active: true, pending: false, disabled: false});

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
setPending({kind: 'speed', target: null, accepted: true, startedAt: Date.now()});
render(manualSpeedState);
expectButton(speedButtons[1], {active: false, pending: false, disabled: true});
expectButton(releaseButton, {active: false, pending: true, disabled: true});
render(state({mode: 'spa'}));
expectButton(speedButtons[0], {active: true, pending: false, disabled: false});
expectButton(releaseButton, {active: true, pending: false, disabled: false});

setPending({kind: 'switch', control: 'lights', target: true, accepted: true, startedAt: Date.now()});
render(state({mode: 'spa'}));
expectButton(switchButtons[2], {active: false, pending: true, disabled: true});
render(state({mode: 'spa', lights: true}));
expectButton(switchButtons[2], {active: true, pending: false, disabled: false});

console.log('WebUI pending-state tests passed.');
