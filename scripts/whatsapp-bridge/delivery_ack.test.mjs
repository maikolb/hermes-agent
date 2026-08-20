/**
 * Regression: an outbound WhatsApp response is not delivered merely because
 * sock.sendMessage() returned a local message id.  The bridge must wait for a
 * server acknowledgement (or a fromMe server echo) before returning HTTP 200.
 */

import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';

import {
  assertOutboundDestination,
  createDeliveryAckTracker,
} from './delivery_ack.js';

{
  const tracker = createDeliveryAckTracker({ timeoutMs: 100 });
  const pending = tracker.wait('after-send');
  tracker.observeUpdates([{ key: { id: 'after-send' }, update: { status: 2 } }]);
  assert.equal(await pending, 2);
}

{
  const tracker = createDeliveryAckTracker({ timeoutMs: 100 });
  tracker.observeUpdates([{ key: { id: 'already-seen' }, update: { status: 'SERVER_ACK' } }]);
  assert.equal(await tracker.wait('already-seen'), 2);
}

{
  const tracker = createDeliveryAckTracker({ timeoutMs: 100 });
  const pending = tracker.wait('server-echo');
  tracker.observeMessage({ key: { id: 'server-echo', fromMe: true } });
  assert.equal(await pending, 2);
}

{
  const tracker = createDeliveryAckTracker({ timeoutMs: 100 });
  const pending = tracker.wait('rejected');
  tracker.observeUpdates([{ key: { id: 'rejected' }, update: { status: 0 } }]);
  await assert.rejects(pending, /rejected by WhatsApp/i);
}

{
  const tracker = createDeliveryAckTracker({ timeoutMs: 10 });
  await assert.rejects(tracker.wait('never-acked'), /delivery acknowledgement timed out/i);
}

{
  assert.doesNotThrow(() => assertOutboundDestination(
    { key: { id: 'ok', remoteJid: '123@lid' } },
    '123@lid',
  ));
  assert.throws(() => assertOutboundDestination(
    { key: { id: 'wrong', remoteJid: '999@lid' } },
    '123@lid',
  ), /outbound destination mismatch/i);
}

const bridgeSource = readFileSync(new URL('./bridge.js', import.meta.url), 'utf8');
assert.match(bridgeSource, /createDeliveryAckTracker/);
assert.match(bridgeSource, /deliveryAcks\.observeUpdates\(updates\)/);
assert.match(bridgeSource, /deliveryAcks\.observeMessage\(msg\)/);
assert.match(bridgeSource, /await deliveryAcks\.wait\(sent\.key\.id\)/);
assert.match(bridgeSource, /assertOutboundDestination\(sent, chatId\)/);

console.log('✅ WhatsApp delivery-ack regression tests passed.');
