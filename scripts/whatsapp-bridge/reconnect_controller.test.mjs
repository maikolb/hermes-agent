import test from 'node:test';
import assert from 'node:assert/strict';

import { createReconnectCoordinator } from './reconnect_controller.js';

function createFakeTimers() {
  let nextId = 1;
  const timers = new Map();
  const cleared = [];

  return {
    cleared,
    timers,
    setTimeoutFn(callback, delayMs) {
      const id = nextId++;
      timers.set(id, { callback, delayMs });
      return id;
    },
    clearTimeoutFn(id) {
      cleared.push(id);
      timers.delete(id);
    },
    run(id) {
      const timer = timers.get(id);
      assert.ok(timer, `timer ${id} should exist`);
      timers.delete(id);
      timer.callback();
    },
  };
}

test('keeps only one reconnect timer for the active socket generation', () => {
  const fake = createFakeTimers();
  const coordinator = createReconnectCoordinator(fake);
  const generation = coordinator.beginAttempt();

  assert.equal(coordinator.schedule(generation, 3000, () => {}), true);
  const firstTimer = [...fake.timers.keys()][0];
  assert.equal(coordinator.schedule(generation, 3000, () => {}), true);

  assert.deepEqual(fake.cleared, [firstTimer]);
  assert.equal(fake.timers.size, 1);
});

test('ignores reconnect callbacks and events from stale sockets', () => {
  const fake = createFakeTimers();
  const coordinator = createReconnectCoordinator(fake);
  const calls = [];
  const staleGeneration = coordinator.beginAttempt();

  coordinator.schedule(staleGeneration, 3000, () => calls.push('stale'));
  const staleTimer = [...fake.timers.keys()][0];
  const currentGeneration = coordinator.beginAttempt();

  assert.equal(coordinator.isCurrent(staleGeneration), false);
  assert.equal(coordinator.isCurrent(currentGeneration), true);
  assert.equal(fake.timers.has(staleTimer), false);
  assert.equal(coordinator.schedule(staleGeneration, 3000, () => calls.push('late')), false);

  coordinator.schedule(currentGeneration, 1000, () => calls.push('current'));
  const currentTimer = [...fake.timers.keys()][0];
  fake.run(currentTimer);

  assert.deepEqual(calls, ['current']);
  assert.equal(coordinator.hasPendingReconnect(), false);
});

test('cancel invalidates the active generation and pending reconnect', () => {
  const fake = createFakeTimers();
  const coordinator = createReconnectCoordinator(fake);
  const generation = coordinator.beginAttempt();

  coordinator.schedule(generation, 3000, () => {});
  coordinator.cancel();

  assert.equal(coordinator.isCurrent(generation), false);
  assert.equal(coordinator.hasPendingReconnect(), false);
  assert.equal(fake.timers.size, 0);
});

test('a successful open cancels a pending reconnect without invalidating the socket', () => {
  const fake = createFakeTimers();
  const coordinator = createReconnectCoordinator(fake);
  const generation = coordinator.beginAttempt();

  coordinator.schedule(generation, 3000, () => {});

  assert.equal(coordinator.cancelPending(generation), true);
  assert.equal(coordinator.isCurrent(generation), true);
  assert.equal(coordinator.hasPendingReconnect(), false);
  assert.equal(fake.timers.size, 0);
});
