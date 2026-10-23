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
  let skipWaitingCalls = 0;
  const context = {
    URL,
    Response,
    fetch: async () => { throw new Error('unused'); },
    caches: {
      open: async () => ({addAll:async () => {}, put:async () => {}}),
      keys: async () => ['j2m-staff-static-v1', 'unrelated-cache', 'j2m-staff-static-v3-phase76'],
      delete: async key => { deleted.push(key); return true; },
      match: async () => null,
    },
    self: {
      location:{origin:'https://staff.example'},
      clients:{claim:async () => {}},
      skipWaiting:async () => { skipWaitingCalls += 1; },
      addEventListener:(name, handler) => { listeners[name] = handler; },
    },
  };
  vm.runInNewContext(source, context);
  let installation;
  listeners.install({waitUntil:promise => { installation = promise; }});
  await installation;
  assert.equal(skipWaitingCalls, 0, 'an update must not activate or reload automatically');
  listeners.message({data:{type:'SKIP_WAITING'}});
  assert.equal(skipWaitingCalls, 1, 'the waiting worker activates only after an explicit safe request');
  let activation;
  listeners.activate({waitUntil:promise => { activation = promise; }});
  await activation;
  assert.deepEqual(deleted, ['j2m-staff-static-v1']);
  assert.match(source, /no-store/);
  assert.doesNotMatch(source, /localStorage|\/api\//);
}

async function testPwaUpdateWaitsForSafeScannerState() {
  const source = fs.readFileSync(path.join(__dirname, '../../app/static/js/pwa-install.js'), 'utf8');
  const windowListeners = {};
  const serviceWorkerListeners = {};
  const posted = [];
  let reloads = 0;
  let updateNotice = null;
  const waitingWorker = {postMessage:message => posted.push(message)};
  const registration = {waiting:waitingWorker, installing:null, addEventListener() {}, update() {}};
  const makeElement = tag => {
    const listeners = {};
    return {
      tag, dataset:{}, hidden:false, disabled:false, textContent:'', className:'', type:'',
      setAttribute() {}, addEventListener:(name, handler) => { listeners[name] = handler; },
      append(...children) { this.children = children; },
      click() { return listeners.click?.({target:this}); },
    };
  };
  const scannerRoot = {dataset:{scannerPhase:'processing'}};
  const document = {
    body:{appendChild:item => { updateNotice = item; }},
    querySelector:selector => selector === '[data-web-scanner]' ? scannerRoot : selector === '[data-pwa-update]' ? updateNotice : null,
    querySelectorAll:() => [],
    createElement:makeElement,
  };
  const navigator = {
    userAgent:'Android', standalone:false,
    serviceWorker:{
      controller:{}, register:async () => registration,
      addEventListener:(name, handler) => { serviceWorkerListeners[name] = handler; },
    },
  };
  const window = {
    isSecureContext:true, navigator,
    matchMedia:() => ({matches:false}),
    location:{reload:() => { reloads += 1; }},
    addEventListener:(name, handler) => { windowListeners[name] = handler; },
  };
  vm.runInNewContext(source, {window, document, navigator});
  await Promise.resolve(); await Promise.resolve();
  assert.ok(updateNotice, 'a waiting update is announced');
  const updateButton = updateNotice.children[2];
  assert.equal(updateButton.disabled, true, 'update cannot reload while a transaction is processing');
  windowListeners['j2m:scanner-phase']({detail:{phase:'selected'}});
  assert.equal(updateButton.disabled, false, 'the employee may update after reaching a safe state');
  updateButton.click();
  assert.equal(posted.length, 1); assert.equal(posted[0].type, 'SKIP_WAITING');
  assert.equal(reloads, 0, 'requesting activation does not reload before the new worker controls the page');
  serviceWorkerListeners.controllerchange();
  assert.equal(reloads, 1, 'reload happens only after explicit confirmation and controller change');
}

function element(dataset = {}) {
  const listeners = {};
  const classes = new Set(['hidden']);
  return {
    dataset, disabled:false, value:'', textContent:'', id:'', hidden:false,
    className:'',
    classList:{add:name => classes.add(name), remove:name => classes.delete(name), toggle:(name, force) => force ? classes.add(name) : classes.delete(name), contains:name => classes.has(name)},
    setAttribute() {}, focus() { this.focused = true; },
    addEventListener:(name, handler) => { listeners[name] = handler; },
    dispatch:async name => listeners[name]?.({target:this}),
    replaceChildren() {}, appendChild(option) { if (!this.value) this.value = option.value; },
  };
}

function scannerHarness(fetchImpl, {mobile = false, startError = null} = {}) {
  const nodes = {
    status:element(), startPanel:element(), cameraPanel:element(), cameraSelect:element(), cameraSelectWrap:element(), start:element(), stop:element(), reader:element(),
    customerPanel:element(), customerName:element(), customerBalance:element(), customerProgram:element(), stamps:element(), points:element(),
    confirmPanel:element(), confirmTitle:element(), confirmCustomer:element(), confirmBefore:element(), confirmEffect:element(), cancel:element(), confirm:element(),
    successPanel:element(), successName:element(), successBalance:element(), scanNext:element(), purchase:element(), redeem:element(), reset:element(),
    earn:element({action:'earn_stamp'}), redeemAction:element({action:'redeem_stamp'}),
  };
  nodes.reader.id = 'test-reader';
  const one = {
    '[data-scanner-status]':nodes.status, '[data-start-panel]':nodes.startPanel, '[data-camera-panel]':nodes.cameraPanel, '[data-camera-select]':nodes.cameraSelect, '[data-camera-select-wrap]':nodes.cameraSelectWrap,
    '[data-start-camera]':nodes.start, '[data-stop-camera]':nodes.stop, '[data-qr-reader]':nodes.reader,
    '[data-customer-panel]':nodes.customerPanel, '[data-customer-name]':nodes.customerName,
    '[data-customer-balance]':nodes.customerBalance, '[data-customer-program]':nodes.customerProgram, '[data-stamps-actions]':nodes.stamps, '[data-points-actions]':nodes.points,
    '[data-confirm-panel]':nodes.confirmPanel, '[data-confirm-title]':nodes.confirmTitle, '[data-confirm-customer]':nodes.confirmCustomer,
    '[data-confirm-before]':nodes.confirmBefore, '[data-confirm-effect]':nodes.confirmEffect, '[data-cancel-operation]':nodes.cancel, '[data-confirm-operation]':nodes.confirm,
    '[data-success-panel]':nodes.successPanel, '[data-success-name]':nodes.successName, '[data-success-balance]':nodes.successBalance, '[data-scan-next]':nodes.scanNext, '[data-purchase-amount]':nodes.purchase,
    '[data-redeem-amount]':nodes.redeem, '[data-reset-scanner]':nodes.reset,
  };
  const root = {dataset:{}, querySelector:selector => one[selector], querySelectorAll:selector => selector === '[data-action]' ? [nodes.earn, nodes.redeemAction] : [nodes.purchase, nodes.redeem]};
  const documentListeners = {};
  const document = {
    hidden:false,
    querySelector:selector => selector === '[data-web-scanner]' ? root : selector === 'meta[name="csrf-token"]' ? {content:'csrf'} : null,
    querySelectorAll:() => [], createElement:() => element(), addEventListener:(name, handler) => { documentListeners[name] = handler; },
  };
  const windowListeners = {};
  const instances = [];
  let cameraEnumerationCalls = 0;
  class Html5Qrcode {
    static async getCameras() { cameraEnumerationCalls += 1; return [{id:'camera-1', label:'Principal'}, {id:'camera-2', label:'Trasera'}]; }
    constructor() { this.paused = false; this.stopped = false; instances.push(this); }
    async start(camera, _config, callback) { this.startSource = camera; this.callback = callback; if (startError) throw startError; }
    pause() { this.paused = true; }
    resume() { this.paused = false; }
    async stop() { this.stopped = true; }
  }
  let assignedUrl = null;
  function CustomEvent(name, options) { this.type = name; this.detail = options?.detail; }
  const window = {J2MScannerState:{ScannerState}, Html5Qrcode, CustomEvent, isSecureContext:true, matchMedia:query => ({matches:mobile && query.includes('max-width')}), dispatchEvent:() => {}, location:{pathname:'/staff/portal/scanner', assign:url => { assignedUrl = url; }}, addEventListener:(name, handler) => { windowListeners[name] = handler; }};
  const context = {window, document, navigator:{mediaDevices:{getUserMedia(){}}}, Html5Qrcode, crypto:globalThis.crypto, AbortController, setTimeout, clearTimeout, fetch:fetchImpl};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../app/static/js/web-scanner.js'), 'utf8'), context);
  return {nodes, instances, windowListeners, documentListeners, assignedUrl:() => assignedUrl, cameraEnumerationCalls:() => cameraEnumerationCalls};
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

  const mobile = scannerHarness(async () => lookupSuccess('Cliente móvil'), {mobile:true});
  await mobile.nodes.start.dispatch('click');
  assert.equal(mobile.cameraEnumerationCalls(), 0, 'mobile does not enumerate or expose camera choices');
  assert.equal(mobile.instances[0].startSource.facingMode.exact, 'environment', 'mobile requires the rear camera without a silent front fallback');

  const denied = scannerHarness(async () => lookupSuccess('unused'), {mobile:true, startError:{name:'NotAllowedError'}});
  await denied.nodes.start.dispatch('click');
  assert.match(denied.nodes.status.textContent, /permiso/i); assert.equal(denied.nodes.startPanel.classList.contains('hidden'), false);
  const unavailable = scannerHarness(async () => lookupSuccess('unused'), {mobile:true, startError:{name:'OverconstrainedError'}});
  await unavailable.nodes.start.dispatch('click');
  assert.match(unavailable.nodes.status.textContent, /cámara trasera/i); assert.equal(unavailable.nodes.startPanel.classList.contains('hidden'), false);

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
  assert.equal(processCalls, 0, 'selecting a movement only opens explicit confirmation');
  const firstConfirmation = uncertain.nodes.confirm.dispatch('click');
  await uncertain.nodes.confirm.dispatch('click');
  await firstConfirmation;
  oldCallback('qr-repetido-durante-operacion');
  assert.equal(processCalls, 1, 'QR reads never create automatic operations');
  await uncertain.nodes.earn.dispatch('click');
  await uncertain.nodes.confirm.dispatch('click');
  assert.equal(processCalls, 2);
  assert.equal(operationKeys[0], operationKeys[1], 'uncertain retry keeps the same idempotency key');
  assert.equal(uncertain.nodes.customerName.textContent, 'Cliente operación');

  let disconnectedCalls = 0;
  const disconnected = scannerHarness(async () => { disconnectedCalls += 1; throw new Error('offline'); });
  await disconnected.nodes.start.dispatch('click');
  disconnected.instances[0].callback('qr-sin-red'); await sleep(10);
  assert.equal(disconnectedCalls, 1);
  assert.equal(disconnected.instances[0].paused, false, 'network loss safely permits scanning again');

  const expired = scannerHarness(async () => new Response(
    JSON.stringify({message:'Token CSRF inválido o ausente.'}),
    {status:400, headers:{'Content-Type':'application/json'}},
  ));
  await expired.nodes.start.dispatch('click');
  expired.instances[0].callback('qr-expirado');
  await sleep(10);
  assert.equal(expired.assignedUrl(), '/staff/login?csrf_error=1', 'expired CSRF requires a clean login');
}

Promise.all([testServiceWorkerUpgrade(), testPwaUpdateWaitsForSafeScannerState(), testCameraIntegration()]).then(() => process.stdout.write('phase74 javascript behavior: ok\n'));
