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

testServiceWorkerUpgrade().then(() => process.stdout.write('phase74 javascript behavior: ok\n'));
