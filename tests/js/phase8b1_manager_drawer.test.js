const assert = require('assert');
const {FOCUSABLE_SELECTOR, focusableControls, initialize} = require('../../app/static/js/manager-drawer.js');

class Classes {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}
class Element {
  constructor(document, {rects = 1, connected = true} = {}) { this.document = document; this.classList = new Classes(); this.attributes = {}; this.listeners = {}; this.dataset = {}; this.hidden = false; this.inert = false; this.rects = rects; this.isConnected = connected; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type, event = {}) { (this.listeners[type] || []).forEach(listener => listener(event)); }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  getClientRects() { return Array(this.rects).fill({}); }
  focus() { this.document.activeElement = this; }
  blur() { if (this.document.activeElement === this) this.document.activeElement = null; }
}

function fixture() {
  const listeners = {};
  const document = {activeElement:null, body:{classList:new Classes()}, addEventListener(type, listener) {(listeners[type] ||= []).push(listener);}};
  const openButton = new Element(document), closeButton = new Element(document), backdrop = new Element(document), link = new Element(document), logout = new Element(document), hiddenCsrf = new Element(document, {rects:0}), invisible = new Element(document, {rects:0});
  const drawer = new Element(document);
  drawer.controls = [closeButton, link, logout, hiddenCsrf, invisible];
  drawer.querySelectorAll = selector => selector === 'a[href]' ? [link] : drawer.controls;
  drawer.contains = element => drawer.controls.includes(element);
  document.getElementById = id => id === 'managerDrawer' ? drawer : null;
  document.querySelector = selector => ({'[data-manager-backdrop]':backdrop,'[data-open-manager-drawer]':openButton,'[data-close-manager-drawer]':closeButton})[selector] || null;
  const mediaListeners = [];
  const view = {matchMedia:() => ({addEventListener:(type, listener) => mediaListeners.push(listener)})};
  return {document, view, drawer, openButton, closeButton, backdrop, link, logout, hiddenCsrf, invisible, listeners, mediaListeners};
}

(() => {
  assert(FOCUSABLE_SELECTOR.includes('input:not([type="hidden"])'));
  const f = fixture();
  f.openButton.focus();
  const controller = initialize(f.document, f.view);
  assert.strictEqual(f.drawer.inert, true, 'closed drawer must be inert');
  controller.open();
  assert.strictEqual(f.drawer.inert, false);
  assert.strictEqual(f.drawer.getAttribute('aria-hidden'), 'false');
  assert.strictEqual(f.document.activeElement, f.closeButton);
  assert.deepStrictEqual(focusableControls(f.drawer), [f.closeButton, f.link, f.logout]);

  let prevented = false;
  f.logout.focus();
  f.listeners.keydown[0]({key:'Tab', shiftKey:false, preventDefault:() => { prevented = true; }});
  assert(prevented && f.document.activeElement === f.closeButton, 'Tab loops to first visible control');
  f.closeButton.focus();
  f.listeners.keydown[0]({key:'Tab', shiftKey:true, preventDefault() {}});
  assert.strictEqual(f.document.activeElement, f.logout, 'Shift+Tab loops to last visible control');

  f.listeners.keydown[0]({key:'Escape', preventDefault() {}});
  assert.strictEqual(f.drawer.inert, true);
  assert.strictEqual(f.drawer.getAttribute('aria-hidden'), 'true');
  assert.strictEqual(f.document.activeElement, f.openButton, 'Escape restores origin focus');

  controller.open();
  f.backdrop.dispatch('click');
  assert.strictEqual(f.drawer.inert, true, 'outside click closes drawer');
  controller.open();
  f.mediaListeners[0]({matches:true});
  assert.strictEqual(f.drawer.inert, true, 'desktop breakpoint closes drawer');
  assert(!f.drawer.contains(f.document.activeElement), 'focus does not remain in hidden drawer');
  process.stdout.write('phase8b1 manager drawer behavior: ok\n');
})();
