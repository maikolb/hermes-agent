import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';

import {
  PassiveIntakeConfigError,
  PassiveIntakeEgressError,
  createPassiveIntake,
} from './passive_intake.js';

const CONCURSA_JID = '120363111111111111@g.us';
const DOV_JID = '120363222222222222@g.us';

function config(routes, enabled = true) {
  return JSON.stringify({ enabled, routes });
}

function message({ id = 'MSG-1', text = 'Tela trava ao salvar', fromMe = false, remoteJid = CONCURSA_JID } = {}) {
  return {
    key: { id, remoteJid, participant: '5511999999999@s.whatsapp.net', fromMe },
    messageTimestamp: 1_787_266_800,
    message: { extendedTextMessage: { text, contextInfo: { stanzaId: 'QUOTE-1' } } },
  };
}

test('disabled-by-default configuration has no routes', () => {
  const intake = createPassiveIntake({ rawConfig: '', rootDir: '' });
  assert.equal(intake.enabled, false);
  assert.equal(intake.routeCount, 0);
  assert.equal(intake.routeFor(CONCURSA_JID), null);
});

test('invalid enabled configuration fails closed at initialization', () => {
  const root = path.resolve(os.tmpdir(), 'hermes-passive-config-test');
  assert.throws(
    () => createPassiveIntake({ rawConfig: '{broken', rootDir: root }),
    PassiveIntakeConfigError,
  );
  assert.throws(
    () => createPassiveIntake({ rawConfig: config([]), rootDir: root }),
    /requires at least one route/,
  );
  assert.throws(
    () => createPassiveIntake({
      rawConfig: config([{ project: '../escape', jid: CONCURSA_JID }]),
      rootDir: root,
    }),
    /invalid project slug/,
  );
  assert.throws(
    () => createPassiveIntake({
      rawConfig: config([{ project: 'concursa-ai', jid: 'not-a-group' }]),
      rootDir: root,
    }),
    /exact WhatsApp group JID/,
  );
});

test('routes only the exact configured JID and denies its egress', () => {
  const root = path.resolve(os.tmpdir(), 'hermes-passive-route-test');
  const intake = createPassiveIntake({
    rawConfig: config([{ project: 'concursa-ai', jid: CONCURSA_JID }]),
    rootDir: root,
  });
  assert.equal(intake.routeFor(CONCURSA_JID)?.project, 'concursa-ai');
  assert.equal(intake.routeFor(DOV_JID), null);
  assert.equal(intake.routeFor('5511888888888@s.whatsapp.net'), null);
  for (const action of ['send', 'edit', 'send-media', 'send-poll', 'send-location', 'typing', 'read', 'metadata']) {
    assert.throws(() => intake.assertEgressAllowed(CONCURSA_JID, action), PassiveIntakeEgressError);
  }
  assert.doesNotThrow(() => intake.assertEgressAllowed(DOV_JID, 'send'));
});

test('persists encrypted, idempotent and project-isolated envelopes', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-passive-intake-'));
  try {
    const intake = createPassiveIntake({
      rawConfig: config([
        { project: 'concursa-ai', jid: CONCURSA_JID },
        { project: 'dovcrm', jid: DOV_JID },
      ]),
      rootDir: root,
      now: () => Date.parse('2026-08-21T13:00:00.000Z'),
    });
    const first = intake.captureMessage({
      msg: message(),
      chatId: CONCURSA_JID,
      senderId: '5511999999999@s.whatsapp.net',
    });
    const replay = intake.captureMessage({
      msg: message(),
      chatId: CONCURSA_JID,
      senderId: '5511999999999@s.whatsapp.net',
    });
    const dov = intake.captureMessage({
      msg: message({ id: 'MSG-1', text: 'CRM não salva contato', remoteJid: DOV_JID }),
      chatId: DOV_JID,
      senderId: '5511999999999@s.whatsapp.net',
    });
    assert.equal(first.matched, true);
    assert.equal(first.persisted, true);
    assert.equal(replay.duplicate, true);
    assert.equal(first.eventId, replay.eventId);
    assert.ok(first.spoolPath.startsWith(path.join(root, 'concursa-ai', 'spool')));
    assert.equal(readdirSync(path.dirname(first.spoolPath)).length, 1);

    const onDisk = readFileSync(first.spoolPath, 'utf8');
    assert.doesNotMatch(onDisk, /Tela trava ao salvar/);
    assert.doesNotMatch(onDisk, /5511999999999/);
    const envelope = intake.readEnvelope(first.spoolPath, 'concursa-ai');
    const dovEnvelope = intake.readEnvelope(dov.spoolPath, 'dovcrm');
    assert.equal(envelope.schema, 'IntakeEnvelopeV1');
    assert.equal(envelope.project, 'concursa-ai');
    assert.equal(envelope.content.text, 'Tela trava ao salvar');
    assert.equal(envelope.content.replyToNativeId, 'QUOTE-1');
    assert.equal(envelope.actor.pseudonymized, true);
    assert.ok(dov.spoolPath.startsWith(path.join(root, 'dovcrm', 'spool')));
    assert.equal(dovEnvelope.project, 'dovcrm');
    assert.equal(dovEnvelope.content.text, 'CRM não salva contato');
    assert.notEqual(envelope.actor.id, dovEnvelope.actor.id);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('fromMe messages in passive groups are consumed without being spooled', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-passive-from-me-'));
  try {
    const intake = createPassiveIntake({
      rawConfig: config([{ project: 'concursa-ai', jid: CONCURSA_JID }]),
      rootDir: root,
    });
    const result = intake.captureMessage({
      msg: message({ fromMe: true }),
      chatId: CONCURSA_JID,
      senderId: '5511999999999@s.whatsapp.net',
    });
    assert.deepEqual(result, {
      matched: true,
      project: 'concursa-ai',
      persisted: false,
      reason: 'from_me',
    });
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
