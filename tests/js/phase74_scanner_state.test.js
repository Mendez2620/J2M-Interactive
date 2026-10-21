'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {ScannerState} = require('../../app/static/js/web-scanner-state.js');

function selection(name = 'Cliente A') {
  return {token:'token-a', customer:{id:1, full_name:name, current_stamps:0}, business:{id:1, loyalty_type:'stamps', stamps_reward_limit:8}};
}

const invalidAfterA = new ScannerState();
const firstLookup = invalidAfterA.beginLookup();
assert.equal(invalidAfterA.finishLookup(firstLookup, selection()), true);
const invalidLookup = invalidAfterA.beginLookup();
assert.equal(invalidAfterA.selection, null, 'a new lookup must immediately invalidate A');
assert.equal(invalidAfterA.beginOperation({signature:'earn-a', id:'wrong'}), null, 'actions stay disabled while lookup is pending');
invalidAfterA.finishLookup(invalidLookup, null);
assert.equal(invalidAfterA.beginOperation({signature:'earn-a', id:'wrong'}), null, 'invalid B must never operate on A');

const ordering = new ScannerState();
const oldLookup = ordering.beginLookup();
const newLookup = ordering.beginLookup();
assert.equal(ordering.finishLookup(oldLookup, selection('Respuesta antigua')), false);
assert.equal(ordering.finishLookup(newLookup, selection('Respuesta vigente')), true);
assert.equal(ordering.selection.customer.full_name, 'Respuesta vigente');

const retry = new ScannerState();
retry.finishLookup(retry.beginLookup(), selection());
const original = retry.beginOperation({action:'earn_stamp', signature:'earn-a', id:'same-key', before:'0 sellos'});
assert.equal(retry.beginOperation({action:'earn_stamp', signature:'earn-a', id:'other-key'}), null, 'double click is blocked');
retry.finishOperation('ambiguous');
assert.equal(retry.reset(), false, 'uncertain operations lock the selected customer');
assert.equal(retry.beginOperation({action:'redeem_stamp', signature:'redeem-a', id:'new-key'}), null, 'operation changes are blocked');
const replay = retry.beginOperation({action:'earn_stamp', signature:'earn-a', id:'new-key'});
assert.equal(replay.id, original.id, 'retry must reuse the original idempotency key');
retry.finishOperation('success', {id:1, current_stamps:1});
assert.equal(retry.selection.customer.full_name, 'Cliente A', 'partial/idempotent responses preserve the name');
assert.equal(retry.selection.customer.current_stamps, 1);

async function testServiceWorkerUpgrade() {
  const source = fs.readFileSync(path.join(__dirname, '../../app/static/service-worker.js'), 'utf8');
  const listeners = {};
  const deleted = [];
  const context = {
    URL,
    Response,
    fetch: async () => { throw new Error('unused'); },
    caches: {
      open: async () => ({addAll:async () => {}, put:async () => {}}),
      keys: async () => ['j2m-staff-static-v1', 'unrelated-cache', 'j2m-staff-static-v2-20261020'],
      delete: async key => { deleted.push(key); return true; },
      match: async () => null,
    },
    self: {
      location:{origin:'https://staff.example'},
      clients:{claim:async () => {}},
      skipWaiting:async () => {},
      addEventListener:(name, handler) => { listeners[name] = handler; },
    },
  };
  vm.runInNewContext(source, context);
  let activation;
  listeners.activate({waitUntil:promise => { activation = promise; }});
  await activation;
  assert.deepEqual(deleted, ['j2m-staff-static-v1']);
  assert.match(source, /no-store/);
  assert.doesNotMatch(source, /localStorage|\/api\//);
}

function element(dataset = {}) {
  const listeners = {};
  const classes = new Set(['hidden']);
  return {
    dataset, disabled:false, value:'', textContent:'', id:'', hidden:false,
    className:'',
    classList:{add:name => classes.add(name), remove:name => classes.delete(name), toggle:(name, force) => force ? classes.add(name) : classes.delete(name), contains:name => classes.has(name)},
    addEventListener:(name, handler) => { listeners[name] = handler; },
    dispatch:async name => listeners[name]?.({target:this}),
    replaceChildren() {}, appendChild(option) { if (!this.value) this.value = option.value; },
  };
}

function scannerHarness(fetchImpl) {
  const nodes = {
    status:element(), cameraPanel:element(), cameraSelect:element(), start:element(), stop:element(), reader:element(),
    customerPanel:element(), customerName:element(), customerBalance:element(), stamps:element(), points:element(),
    manual:element(), lookup:element(), purchase:element(), redeem:element(), reset:element(),
    earn:element({action:'earn_stamp'}), redeemAction:element({action:'redeem_stamp'}),
  };
  nodes.reader.id = 'test-reader';
  const one = {
    '[data-scanner-status]':nodes.status, '[data-camera-panel]':nodes.cameraPanel, '[data-camera-select]':nodes.cameraSelect,
    '[data-start-camera]':nodes.start, '[data-stop-camera]':nodes.stop, '[data-qr-reader]':nodes.reader,
    '[data-customer-panel]':nodes.customerPanel, '[data-customer-name]':nodes.customerName,
    '[data-customer-balance]':nodes.customerBalance, '[data-stamps-actions]':nodes.stamps, '[data-points-actions]':nodes.points,
    '[data-manual-token]':nodes.manual, '[data-lookup-manual]':nodes.lookup, '[data-purchase-amount]':nodes.purchase,
    '[data-redeem-amount]':nodes.redeem, '[data-reset-scanner]':nodes.reset,
  };
  const root = {querySelector:selector => one[selector], querySelectorAll:selector => selector === '[data-action]' ? [nodes.earn, nodes.redeemAction] : [nodes.purchase, nodes.redeem]};
  const documentListeners = {};
  const document = {
    hidden:false,
    querySelector:selector => selector === '[data-web-scanner]' ? root : selector === 'meta[name="csrf-token"]' ? {content:'csrf'} : null,
    querySelectorAll:() => [], createElement:() => element(), addEventListener:(name, handler) => { documentListeners[name] = handler; },
  };
  const windowListeners = {};
  const instances = [];
  class Html5Qrcode {
    static async getCameras() { return [{id:'camera-1', label:'Principal'}, {id:'camera-2', label:'Trasera'}]; }
    constructor() { this.paused = false; this.stopped = false; instances.push(this); }
    async start(_camera, _config, callback) { this.callback = callback; }
    pause() { this.paused = true; }
    resume() { this.paused = false; }
    async stop() { this.stopped = true; }
  }
  const window = {J2MScannerState:{ScannerState}, Html5Qrcode, isSecureContext:true, addEventListener:(name, handler) => { windowListeners[name] = handler; }};
  const context = {window, document, navigator:{mediaDevices:{getUserMedia(){}}}, Html5Qrcode, crypto:globalThis.crypto, AbortController, setTimeout, clearTimeout, fetch:fetchImpl};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../app/static/js/web-scanner.js'), 'utf8'), context);
  return {nodes, instances, windowListeners, documentListeners};
}

const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const lookupSuccess = name => new Response(JSON.stringify({customer:{id:1, full_name:name, current_stamps:0}, business:{id:1, loyalty_type:'stamps', stamps_reward_limit:8}}), {status:200, headers:{'Content-Type':'application/json'}});

async function testCameraIntegration() {
  let lookupCalls = 0;
  const slow = scannerHarness(async url => {
    assert.equal(url, '/api/web-scanner/lookup'); lookupCalls += 1;
    await sleep(500); return lookupSuccess('Cliente correcto');
  });
  await slow.nodes.start.dispatch('click');
  for (let index = 0; index < 10; index += 1) slow.instances[0].callback('qr-repetido');
  assert.equal(slow.instances[0].paused, true, 'camera pauses before awaiting the lookup');
  await sleep(550);
  assert.equal(lookupCalls, 1, 'ten detections produce one effective lookup');
  assert.equal(slow.nodes.customerName.textContent, 'Cliente correcto');
  assert.equal(slow.nodes.customerPanel.classList.contains('hidden'), false);

  const responses = [
    async () => new Response(JSON.stringify({message:'Cliente no encontrado.'}), {status:404, headers:{'Content-Type':'application/json'}}),
    async () => lookupSuccess('Cliente después del inválido'),
  ];
  let sequentialCalls = 0;
  const sequential = scannerHarness(async () => { sequentialCalls += 1; return responses.shift()(); });
  await sequential.nodes.start.dispatch('click');
  sequential.instances[0].callback('qr-invalido'); await sleep(10);
  assert.equal(sequential.instances[0].paused, false, 'invalid lookup resumes the same camera');
  sequential.instances[0].callback('qr-valido'); await sleep(10);
  assert.equal(sequentialCalls, 2);
  assert.equal(sequential.nodes.customerName.textContent, 'Cliente después del inválido');

  let changedCalls = 0;
  const changed = scannerHarness(async () => { changedCalls += 1; await sleep(80); return lookupSuccess('Cliente tras cambio'); });
  await changed.nodes.start.dispatch('click');
  changed.instances[0].callback('qr-cambio-camara');
  await changed.nodes.cameraSelect.dispatch('change');
  await sleep(100);
  assert.equal(changedCalls, 1, 'camera changes do not restart an active lookup');
  assert.equal(changed.nodes.customerName.textContent, 'Cliente tras cambio');

  const operationKeys = [];
  let processCalls = 0;
  const uncertain = scannerHarness(async (url, options) => {
    if (url.endsWith('/lookup')) return lookupSuccess('Cliente operación');
    processCalls += 1; operationKeys.push(options.headers['Idempotency-Key']);
    if (processCalls === 1) throw new Error('lost response after commit');
    return new Response(JSON.stringify({customer:{id:1,current_stamps:1},wallet_sync:{google_status:'pending'}}), {status:200, headers:{'Content-Type':'application/json'}});
  });
  await uncertain.nodes.start.dispatch('click');
  const oldCallback = uncertain.instances[0].callback;
  oldCallback('qr-operacion'); await sleep(10);
  await uncertain.nodes.earn.dispatch('click');
  oldCallback('qr-repetido-durante-operacion');
  assert.equal(processCalls, 1, 'QR reads never create automatic operations');
  await uncertain.nodes.earn.dispatch('click');
  assert.equal(processCalls, 2);
  assert.equal(operationKeys[0], operationKeys[1], 'uncertain retry keeps the same idempotency key');
  assert.equal(uncertain.nodes.customerName.textContent, 'Cliente operación');

  let disconnectedCalls = 0;
  const disconnected = scannerHarness(async () => { disconnectedCalls += 1; throw new Error('offline'); });
  await disconnected.nodes.start.dispatch('click');
  disconnected.instances[0].callback('qr-sin-red'); await sleep(10);
  assert.equal(disconnectedCalls, 1);
  assert.equal(disconnected.instances[0].paused, false, 'network loss safely permits scanning again');
}

Promise.all([testServiceWorkerUpgrade(), testCameraIntegration()]).then(() => process.stdout.write('phase74 javascript behavior: ok\n'));
