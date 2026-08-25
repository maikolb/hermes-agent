import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import { createPassiveIntakeWakeDispatcher } from './passive_intake_wake.js';


const ROUTE = Object.freeze({
  project: 'sandbox',
  wake: Object.freeze({
    profile: 'hermes-project-factory',
    cronJobId: 'b87b386b5cd5',
  }),
});


test('does nothing for a route without a configured wake', () => {
  let calls = 0;
  const dispatcher = createPassiveIntakeWakeDispatcher({
    spawnProcess: () => { calls += 1; },
  });

  assert.equal(dispatcher.runtimeConfigured, false);
  assert.equal(dispatcher.dispatch({ project: 'sandbox', wake: null }), false);
  assert.equal(calls, 0);
});


test('detaches the existing cross-profile executor with hidden stdio', () => {
  const calls = [];
  let unrefCalls = 0;
  const python = path.resolve('C:/Hermes/venv/Scripts/python.exe');
  const root = path.resolve('C:/Hermes/hermes-agent');
  const dispatcher = createPassiveIntakeWakeDispatcher({
    pythonExecutable: python,
    agentRoot: root,
    environment: { BASELINE: '1' },
    spawnProcess: (...args) => {
      calls.push(args);
      return { unref: () => { unrefCalls += 1; } };
    },
  });

  assert.equal(dispatcher.runtimeConfigured, true);
  assert.equal(dispatcher.dispatch(ROUTE), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], python);
  assert.deepEqual(calls[0][1], [
    '-m',
    'hermes_cli.passive_intake_wake',
    '--profile',
    'hermes-project-factory',
    '--job-id',
    'b87b386b5cd5',
  ]);
  assert.equal(calls[0][2].cwd, root);
  assert.equal(calls[0][2].detached, true);
  assert.equal(calls[0][2].stdio, 'ignore');
  assert.equal(calls[0][2].windowsHide, true);
  assert.equal(calls[0][2].env.HERMES_INTAKE_EVENT_WAKE, '1');
  assert.equal(unrefCalls, 1);
});


test('fails closed when a configured wake has no absolute runtime', () => {
  const dispatcher = createPassiveIntakeWakeDispatcher({
    pythonExecutable: 'python',
    agentRoot: '.',
  });

  assert.throws(() => dispatcher.dispatch(ROUTE), /absolute paths/);
});
