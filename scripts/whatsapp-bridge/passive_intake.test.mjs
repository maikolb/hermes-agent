import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';

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

function mediaMessage({ id = 'MEDIA-1', viewOnce = false, ephemeral = false } = {}) {
  const inner = {
    imageMessage: {
      caption: 'Erro mostrado no print',
      mimetype: 'image/png',
      fileLength: 12,
    },
  };
  const wrapped = viewOnce ? { viewOnceMessageV2: { message: inner } } : inner;
  return {
    key: { id, remoteJid: CONCURSA_JID, participant: '5511999999999@s.whatsapp.net', fromMe: false },
    messageTimestamp: 1_787_266_800,
    message: ephemeral ? { ephemeralMessage: { message: wrapped } } : wrapped,
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
  assert.throws(
    () => createPassiveIntake({
      rawConfig: config([{
        project: 'concursa-ai',
        jid: CONCURSA_JID,
        wake: { profile: '../escape', cron_job_id: 'b87b386b5cd5' },
      }]),
      rootDir: root,
    }),
    /invalid profile/,
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

test('validates and exposes only a bounded cross-profile wake target', () => {
  const root = path.resolve(os.tmpdir(), 'hermes-passive-wake-route-test');
  const intake = createPassiveIntake({
    rawConfig: config([{
      project: 'concursa-ai',
      jid: CONCURSA_JID,
      wake: {
        profile: 'hermes-project-factory',
        cron_job_id: 'b87b386b5cd5',
      },
    }]),
    rootDir: root,
  });

  assert.deepEqual(intake.routeFor(CONCURSA_JID)?.wake, {
    profile: 'hermes-project-factory',
    cronJobId: 'b87b386b5cd5',
  });
});

test('persists readable raw markdown, idempotent IDs and project isolation', () => {
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
    assert.equal(first.readyPersisted, true);
    assert.equal(replay.duplicate, true);
    assert.equal(first.eventId, replay.eventId);
    assert.ok(first.spoolPath.startsWith(path.join(root, 'concursa-ai', 'raw')));
    assert.deepEqual(readdirSync(path.dirname(first.spoolPath)).sort(), ['message.md', 'ready.md']);

    const onDisk = readFileSync(first.spoolPath, 'utf8');
    assert.match(onDisk, /Tela trava ao salvar/);
    assert.doesNotMatch(onDisk, /5511999999999/);
    assert.match(onDisk, /schema: WhatsAppRawMessageV1/);
    assert.match(onDisk, /reply_to_message_id: "QUOTE-1"/);
    assert.match(onDisk, /actor: "participant_[a-f0-9]{24}"/);
    assert.ok(dov.spoolPath.startsWith(path.join(root, 'dovcrm', 'raw')));
    assert.match(readFileSync(dov.spoolPath, 'utf8'), /CRM não salva contato/);
    assert.ok(existsSync(path.join(root, 'concursa-ai', 'curated')));
    assert.ok(existsSync(path.join(root, 'dovcrm', 'curated')));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('captures fromMe metadata without confusing another participant with Titan', () => {
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
    assert.equal(result.matched, true);
    assert.equal(result.persisted, true);
    assert.match(readFileSync(result.spoolPath, 'utf8'), /from_titan_number: true/);

    const external = intake.captureMessage({
      msg: message({ id: 'MSG-EXTERNAL', fromMe: false }),
      chatId: CONCURSA_JID,
      senderId: '5511888888888@s.whatsapp.net',
    });
    assert.match(readFileSync(external.spoolPath, 'utf8'), /from_titan_number: false/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('captures original media bytes beside raw markdown and binds them by hash', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-passive-media-'));
  try {
    const intake = createPassiveIntake({
      rawConfig: config([{ project: 'concursa-ai', jid: CONCURSA_JID }]),
      rootDir: root,
    });
    const captured = intake.captureMessage({
      msg: mediaMessage(),
      chatId: CONCURSA_JID,
      senderId: '5511999999999@s.whatsapp.net',
    });
    assert.equal(captured.hasMedia, true);
    assert.equal(intake.mediaState('concursa-ai', captured.eventId), 'pending');
    const bytes = Buffer.from('89504e470d0a1a0a74657374', 'hex');
    const media = intake.captureMedia({
      project: 'concursa-ai',
      eventId: captured.eventId,
      spoolPath: captured.spoolPath,
      kind: captured.mediaMetadata[0].kind,
      mime: captured.mediaMetadata[0].mime,
      bytes,
    });
    assert.equal(media.status, 'captured');
    assert.equal(media.readyPersisted, true);
    assert.equal(intake.mediaState('concursa-ai', captured.eventId), 'captured');
    const eventRoot = path.dirname(captured.spoolPath);
    const recordPath = path.join(eventRoot, 'evidence.png');
    assert.deepEqual(readFileSync(recordPath), bytes);
    const ready = readFileSync(path.join(eventRoot, 'ready.md'), 'utf8');
    assert.match(ready, /media_state: "captured"/);
    assert.match(ready, /attachment_path: "evidence.png"/);
    assert.match(ready, new RegExp(media.sha256));
    assert.equal(intake.captureMedia({
      project: 'concursa-ai',
      eventId: captured.eventId,
      spoolPath: captured.spoolPath,
      kind: 'image',
      mime: 'image/png',
      bytes,
    }).duplicate, true);
    assert.deepEqual(intake.listRawEventFiles('concursa-ai', captured.eventId), [
      'evidence.png',
      'message.md',
      'ready.md',
    ]);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('view-once media is marked privacy-restricted without capturing bytes', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-passive-view-once-'));
  try {
    const intake = createPassiveIntake({
      rawConfig: config([{ project: 'concursa-ai', jid: CONCURSA_JID }]),
      rootDir: root,
    });
    const captured = intake.captureMessage({
      msg: mediaMessage({ viewOnce: true, ephemeral: true }),
      chatId: CONCURSA_JID,
      senderId: '5511999999999@s.whatsapp.net',
    });
    assert.equal(captured.privacyRestricted, true);
    const failure = intake.captureMediaFailure({
      project: 'concursa-ai',
      eventId: captured.eventId,
      spoolPath: captured.spoolPath,
      code: 'media-privacy-restricted',
    });
    assert.equal(failure.status, 'failed');
    assert.equal(intake.mediaState('concursa-ai', captured.eventId), 'failed');
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
