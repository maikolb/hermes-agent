import path from 'path';
import {
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  unlinkSync,
  writeFileSync,
} from 'fs';
import { createHash, createHmac, randomBytes } from 'crypto';

const PROJECT_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const GROUP_JID_PATTERN = /^[0-9][0-9-]{4,63}@g\.us$/;
const EVENT_ID_PATTERN = /^[a-f0-9]{64}$/;
const PROFILE_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62})$/;
const CRON_JOB_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{5,127}$/;
const MAX_ROUTES = 32;
const MAX_TEXT_LENGTH = 32_768;
const MAX_FILENAME_LENGTH = 255;
const MAX_MIME_LENGTH = 128;
const MAX_MEDIA_BYTES = 100_000_000;

export class PassiveIntakeConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = 'PassiveIntakeConfigError';
    this.code = 'PASSIVE_INTAKE_CONFIG_INVALID';
  }
}

export class PassiveIntakeEgressError extends Error {
  constructor(action) {
    super(`WhatsApp ${action || 'egress'} is disabled for passive intake routes`);
    this.name = 'PassiveIntakeEgressError';
    this.code = 'PASSIVE_INTAKE_EGRESS_DENIED';
  }
}

function isPlainObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function assertOnlyKeys(value, allowed, label) {
  const unexpected = Object.keys(value).filter(key => !allowed.has(key));
  if (unexpected.length > 0) {
    throw new PassiveIntakeConfigError(`${label} contains unsupported fields`);
  }
}

function parseRawConfig(rawConfig) {
  if (rawConfig === undefined || rawConfig === null || String(rawConfig).trim() === '') {
    return { enabled: false, routes: [] };
  }
  try {
    return JSON.parse(String(rawConfig));
  } catch {
    throw new PassiveIntakeConfigError('passive intake configuration is not valid JSON');
  }
}

export function parsePassiveIntakeConfig(rawConfig, rootDir) {
  const parsed = parseRawConfig(rawConfig);
  if (!isPlainObject(parsed)) {
    throw new PassiveIntakeConfigError('passive intake configuration must be an object');
  }
  assertOnlyKeys(parsed, new Set(['enabled', 'routes']), 'passive intake configuration');

  if (parsed.enabled !== undefined && typeof parsed.enabled !== 'boolean') {
    throw new PassiveIntakeConfigError('passive intake enabled must be boolean');
  }
  const enabled = parsed.enabled === true;
  const routesValue = parsed.routes === undefined ? [] : parsed.routes;
  if (!Array.isArray(routesValue) || routesValue.length > MAX_ROUTES) {
    throw new PassiveIntakeConfigError(`passive intake routes must be an array of at most ${MAX_ROUTES} items`);
  }
  if (enabled && routesValue.length === 0) {
    throw new PassiveIntakeConfigError('enabled passive intake requires at least one route');
  }
  if (enabled && (!rootDir || !path.isAbsolute(String(rootDir)))) {
    throw new PassiveIntakeConfigError('enabled passive intake requires an absolute root');
  }

  const seenProjects = new Set();
  const seenJids = new Set();
  const routes = routesValue.map((route, index) => {
    if (!isPlainObject(route)) {
      throw new PassiveIntakeConfigError(`passive intake route ${index} must be an object`);
    }
    assertOnlyKeys(route, new Set(['project', 'jid', 'wake']), `passive intake route ${index}`);
    const project = typeof route.project === 'string' ? route.project.trim() : '';
    const jid = typeof route.jid === 'string' ? route.jid.trim() : '';
    if (!PROJECT_PATTERN.test(project)) {
      throw new PassiveIntakeConfigError(`passive intake route ${index} has an invalid project slug`);
    }
    if (!GROUP_JID_PATTERN.test(jid)) {
      throw new PassiveIntakeConfigError(`passive intake route ${index} must use an exact WhatsApp group JID`);
    }
    if (seenProjects.has(project) || seenJids.has(jid)) {
      throw new PassiveIntakeConfigError('passive intake projects and group JIDs must be unique');
    }
    let wake = null;
    if (route.wake !== undefined) {
      if (!isPlainObject(route.wake)) {
        throw new PassiveIntakeConfigError(`passive intake route ${index} wake must be an object`);
      }
      assertOnlyKeys(
        route.wake,
        new Set(['profile', 'cron_job_id']),
        `passive intake route ${index} wake`,
      );
      const profile = typeof route.wake.profile === 'string' ? route.wake.profile.trim() : '';
      const cronJobId = typeof route.wake.cron_job_id === 'string'
        ? route.wake.cron_job_id.trim()
        : '';
      if (!PROFILE_PATTERN.test(profile)) {
        throw new PassiveIntakeConfigError(`passive intake route ${index} wake has an invalid profile`);
      }
      if (!CRON_JOB_ID_PATTERN.test(cronJobId)) {
        throw new PassiveIntakeConfigError(`passive intake route ${index} wake has an invalid cron job ID`);
      }
      wake = Object.freeze({ profile, cronJobId });
    }
    seenProjects.add(project);
    seenJids.add(jid);
    return Object.freeze({ project, jid, wake });
  });

  return Object.freeze({
    enabled,
    rootDir: rootDir ? path.resolve(String(rootDir)) : '',
    routes: Object.freeze(routes),
  });
}

function boundedString(value, maxLength) {
  if (value === undefined || value === null) return '';
  return String(value).replace(/\u0000/g, '').slice(0, maxLength);
}

function unwrapMessage(message) {
  let current = isPlainObject(message) ? message : {};
  for (let depth = 0; depth < 6; depth += 1) {
    const nested = current.ephemeralMessage?.message
      || current.viewOnceMessage?.message
      || current.viewOnceMessageV2?.message
      || current.documentWithCaptionMessage?.message;
    if (!isPlainObject(nested)) break;
    current = nested;
  }
  return current;
}

function isViewOnceMessage(message) {
  let current = isPlainObject(message) ? message : {};
  for (let depth = 0; depth < 6; depth += 1) {
    if (
      isPlainObject(current.viewOnceMessage)
      || isPlainObject(current.viewOnceMessageV2)
      || isPlainObject(current.viewOnceMessageV2Extension)
    ) {
      return true;
    }
    const nested = current.ephemeralMessage?.message
      || current.documentWithCaptionMessage?.message;
    if (!isPlainObject(nested)) break;
    current = nested;
  }
  return false;
}

function firstText(message) {
  const candidates = [
    message.conversation,
    message.extendedTextMessage?.text,
    message.imageMessage?.caption,
    message.videoMessage?.caption,
    message.documentMessage?.caption,
    message.buttonsResponseMessage?.selectedDisplayText,
    message.listResponseMessage?.title,
    message.templateButtonReplyMessage?.selectedDisplayText,
  ];
  const value = candidates.find(candidate => typeof candidate === 'string' && candidate.length > 0);
  return boundedString(value, MAX_TEXT_LENGTH);
}

function contextInfoFor(message) {
  for (const value of Object.values(message)) {
    if (isPlainObject(value?.contextInfo)) return value.contextInfo;
  }
  return {};
}

function contentKindFor(message) {
  const key = Object.keys(message).find(name => name.endsWith('Message') || name === 'conversation');
  if (!key) return 'unknown';
  return boundedString(key.replace(/Message$/, '').replace(/^conversation$/, 'text'), 64);
}

function attachmentMetadata(message) {
  const definitions = [
    ['image', message.imageMessage],
    ['video', message.videoMessage],
    ['audio', message.audioMessage],
    ['document', message.documentMessage],
    ['sticker', message.stickerMessage],
  ];
  return definitions
    .filter(([, value]) => isPlainObject(value))
    .map(([kind, value]) => ({
      kind,
      mime: boundedString(value.mimetype, MAX_MIME_LENGTH).toLowerCase(),
      fileName: boundedString(value.fileName, MAX_FILENAME_LENGTH),
      bytes: Number.isSafeInteger(Number(value.fileLength)) && Number(value.fileLength) >= 0
        ? Number(value.fileLength)
        : null,
    }));
}

function timestampMilliseconds(value, fallback) {
  let numberValue;
  if (value && typeof value.toNumber === 'function') {
    numberValue = value.toNumber();
  } else {
    numberValue = Number(value);
  }
  if (!Number.isFinite(numberValue) || numberValue <= 0) return fallback;
  return numberValue < 10_000_000_000 ? Math.trunc(numberValue * 1000) : Math.trunc(numberValue);
}

function atomicWriteExclusive(finalPath, data, { mode = 0o600 } = {}) {
  if (existsSync(finalPath)) return false;
  mkdirSync(path.dirname(finalPath), { recursive: true });
  const temporaryPath = `${finalPath}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`;
  let descriptor;
  try {
    descriptor = openSync(temporaryPath, 'wx', mode);
    if (typeof data === 'string') {
      writeFileSync(descriptor, data, { encoding: 'utf8' });
    } else {
      writeFileSync(descriptor, data);
    }
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = undefined;
    try {
      linkSync(temporaryPath, finalPath);
    } catch (error) {
      if (error?.code === 'EEXIST' || existsSync(finalPath)) {
        try { unlinkSync(temporaryPath); } catch {}
        return false;
      }
      throw error;
    }
    try { unlinkSync(temporaryPath); } catch {}
    return true;
  } finally {
    if (descriptor !== undefined) {
      try { closeSync(descriptor); } catch {}
    }
    if (existsSync(temporaryPath)) {
      try { unlinkSync(temporaryPath); } catch {}
    }
  }
}

function readOrCreateActorKey(rootDir) {
  const keyPath = path.join(rootDir, '.actor-key');
  mkdirSync(rootDir, { recursive: true });
  if (!existsSync(keyPath)) {
    atomicWriteExclusive(keyPath, randomBytes(32).toString('hex'));
  }
  const encoded = readFileSync(keyPath, 'utf8').trim();
  if (!/^[a-f0-9]{64}$/.test(encoded)) {
    throw new Error('passive intake actor key is invalid');
  }
  return Buffer.from(encoded, 'hex');
}

function safeProjectRoot(rootDir, project) {
  const resolvedRoot = path.resolve(rootDir);
  const projectRoot = path.resolve(resolvedRoot, project);
  if (projectRoot !== resolvedRoot && !projectRoot.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw new Error('passive intake project path escaped the root');
  }
  return projectRoot;
}

function eventPaths(rootDir, project, eventId) {
  if (!EVENT_ID_PATTERN.test(String(eventId || ''))) {
    throw new Error('passive intake event ID is invalid');
  }
  const projectRoot = safeProjectRoot(rootDir, project);
  const rawRoot = path.join(projectRoot, 'raw');
  const curatedRoot = path.join(projectRoot, 'curated');
  const eventRoot = path.join(rawRoot, eventId);
  mkdirSync(rawRoot, { recursive: true });
  mkdirSync(curatedRoot, { recursive: true });
  return {
    eventRoot,
    messagePath: path.join(eventRoot, 'message.md'),
    readyPath: path.join(eventRoot, 'ready.md'),
    receiptPath: path.join(eventRoot, 'receipt.md'),
  };
}

function markdownScalar(value) {
  return JSON.stringify(value === undefined ? null : value);
}

function rawMessageMarkdown(envelope) {
  const quoted = envelope.quotedText
    ? `\n\n## Texto citado\n\n${envelope.quotedText}`
    : '';
  const body = envelope.text || '(mensagem sem texto; veja a evidência anexada)';
  return [
    '---',
    'schema: WhatsAppRawMessageV1',
    'version: 1',
    'status: pending',
    `intake_id: ${markdownScalar(envelope.intakeId)}`,
    `message_id: ${markdownScalar(envelope.nativeId)}`,
    `project: ${markdownScalar(envelope.project)}`,
    `occurred_at: ${markdownScalar(envelope.occurredAt)}`,
    `received_at: ${markdownScalar(envelope.receivedAt)}`,
    `actor: ${markdownScalar(envelope.actor)}`,
    `from_titan_number: ${envelope.fromTitanNumber ? 'true' : 'false'}`,
    `reply_to_message_id: ${markdownScalar(envelope.replyToNativeId)}`,
    `content_kind: ${markdownScalar(envelope.contentKind)}`,
    `media_expected: ${envelope.mediaExpected ? 'true' : 'false'}`,
    `media_kind: ${markdownScalar(envelope.mediaKind)}`,
    `media_mime: ${markdownScalar(envelope.mediaMime)}`,
    `media_original_name: ${markdownScalar(envelope.mediaOriginalName)}`,
    'untrusted_input: true',
    '---',
    '',
    '# Conteúdo recebido',
    '',
    body,
    quoted,
    '',
  ].join('\n');
}

function readyMarkdown({ eventId, mediaState, attachmentPath = null, kind = null, mime = null, bytes = null, sha256 = null, error = null }) {
  return [
    '---',
    'schema: WhatsAppRawReadyV1',
    'version: 1',
    'status: ready',
    `intake_id: ${markdownScalar(`wa_${eventId}`)}`,
    `media_state: ${markdownScalar(mediaState)}`,
    `attachment_path: ${markdownScalar(attachmentPath)}`,
    `attachment_kind: ${markdownScalar(kind)}`,
    `attachment_mime: ${markdownScalar(mime)}`,
    `attachment_bytes: ${bytes === null ? 'null' : bytes}`,
    `attachment_sha256: ${markdownScalar(sha256)}`,
    `media_error: ${markdownScalar(error)}`,
    '---',
    '',
  ].join('\n');
}

function retryableMediaMarkdown({ eventId, error }) {
  return [
    '---',
    'schema: WhatsAppRawMediaRetryV1',
    'version: 1',
    'status: retryable',
    `intake_id: ${markdownScalar(`wa_${eventId}`)}`,
    `media_error: ${markdownScalar(error)}`,
    '---',
    '',
  ].join('\n');
}

function extensionFor(kind, mime) {
  const normalized = String(mime || '').split(';', 1)[0].trim().toLowerCase();
  const byMime = new Map([
    ['image/jpeg', '.jpg'], ['image/png', '.png'], ['image/gif', '.gif'], ['image/webp', '.webp'],
    ['audio/ogg', '.ogg'], ['audio/opus', '.opus'], ['audio/mpeg', '.mp3'], ['audio/wav', '.wav'], ['audio/mp4', '.m4a'],
    ['video/mp4', '.mp4'], ['video/quicktime', '.mov'], ['video/webm', '.webm'],
    ['application/pdf', '.pdf'], ['text/plain', '.txt'],
  ]);
  if (byMime.has(normalized)) return byMime.get(normalized);
  return ({ image: '.img', video: '.video', audio: '.audio', document: '.bin', sticker: '.webp' })[kind] || '.bin';
}

function validateSourcePath(paths, sourcePath) {
  const resolved = path.resolve(String(sourcePath || ''));
  if (resolved !== path.resolve(paths.messagePath) || !existsSync(resolved)) {
    throw new Error('passive intake media has no durable raw message');
  }
}

export function createPassiveIntake({ rawConfig, rootDir, now = () => Date.now() } = {}) {
  const config = parsePassiveIntakeConfig(rawConfig, rootDir);
  const routeByJid = new Map(config.routes.map(route => [route.jid, route]));
  const rawFingerprint = `${rawConfig === undefined ? '' : String(rawConfig)}\u0000${config.rootDir}`;
  const configHash = createHash('sha256').update(rawFingerprint).digest('hex').slice(0, 16);

  function routeFor(chatId) {
    if (!config.enabled || typeof chatId !== 'string') return null;
    return routeByJid.get(chatId) || null;
  }

  function assertEgressAllowed(chatId, action) {
    if (routeFor(chatId)) throw new PassiveIntakeEgressError(action);
  }

  function captureMessage({ msg, chatId, senderId }) {
    const route = routeFor(chatId);
    if (!route) return { matched: false };

    const privacyRestricted = isViewOnceMessage(msg?.message);
    const message = unwrapMessage(msg?.message);
    const receivedAtMs = now();
    const occurredAtMs = timestampMilliseconds(msg?.messageTimestamp, receivedAtMs);
    const actorKey = readOrCreateActorKey(config.rootDir);
    const projectActorKey = createHmac('sha256', actorKey).update(route.project).digest();
    const nativeId = boundedString(msg?.key?.id, 256);
    const contextInfo = contextInfoFor(message);
    const text = firstText(message);
    const quotedText = firstText(unwrapMessage(contextInfo.quotedMessage));
    const contentKind = contentKindFor(message);
    const actorHash = createHmac('sha256', projectActorKey)
      .update(boundedString(senderId, 256))
      .digest('hex')
      .slice(0, 24);
    const routeHash = createHmac('sha256', projectActorKey).update(chatId).digest('hex');
    const eventMaterial = nativeId || JSON.stringify({ occurredAtMs, actorHash, contentKind, text });
    const eventId = createHash('sha256')
      .update(`${route.project}\u0000${routeHash}\u0000${eventMaterial}`)
      .digest('hex');
    const attachments = attachmentMetadata(message);
    const paths = eventPaths(config.rootDir, route.project, eventId);
    const envelope = {
      intakeId: `wa_${eventId}`,
      project: route.project,
      nativeId: nativeId || null,
      occurredAt: new Date(occurredAtMs).toISOString(),
      receivedAt: new Date(receivedAtMs).toISOString(),
      actor: `participant_${actorHash}`,
      fromTitanNumber: msg?.key?.fromMe === true,
      replyToNativeId: boundedString(contextInfo.stanzaId, 256) || null,
      contentKind,
      text,
      quotedText,
      mediaExpected: attachments.length > 0,
      mediaKind: attachments[0]?.kind || null,
      mediaMime: attachments[0]?.mime || null,
      mediaOriginalName: attachments[0]?.fileName || null,
    };

    if (existsSync(paths.receiptPath)) {
      return {
        matched: true,
        project: route.project,
        eventId,
        persisted: false,
        duplicate: true,
        reason: 'already_curated',
        spoolPath: paths.messagePath,
        hasMedia: attachments.length > 0,
        mediaMetadata: attachments,
        privacyRestricted,
      };
    }

    const persisted = atomicWriteExclusive(paths.messagePath, rawMessageMarkdown(envelope));
    const readyPersisted = attachments.length === 0
      ? atomicWriteExclusive(paths.readyPath, readyMarkdown({ eventId, mediaState: 'none' }))
      : false;
    return {
      matched: true,
      project: route.project,
      eventId,
      persisted,
      readyPersisted,
      duplicate: !persisted,
      spoolPath: paths.messagePath,
      hasMedia: attachments.length > 0,
      mediaMetadata: attachments,
      privacyRestricted,
    };
  }

  function captureMedia({ project, eventId, spoolPath, kind, mime, bytes }) {
    if (!Buffer.isBuffer(bytes) || bytes.length <= 0 || bytes.length > MAX_MEDIA_BYTES) {
      throw new Error('passive intake media size is invalid');
    }
    const paths = eventPaths(config.rootDir, project, eventId);
    validateSourcePath(paths, spoolPath);
    if (existsSync(paths.receiptPath)) {
      return { persisted: false, duplicate: true, status: 'done' };
    }
    const safeKind = ['image', 'video', 'audio', 'document', 'sticker'].includes(kind) ? kind : 'document';
    const safeMime = boundedString(mime, MAX_MIME_LENGTH).toLowerCase();
    const fileName = `evidence${extensionFor(safeKind, safeMime)}`;
    const target = path.join(paths.eventRoot, fileName);
    const digest = createHash('sha256').update(bytes).digest('hex');
    const persisted = atomicWriteExclusive(target, bytes);
    const readyPersisted = atomicWriteExclusive(paths.readyPath, readyMarkdown({
      eventId,
      mediaState: 'captured',
      attachmentPath: fileName,
      kind: safeKind === 'sticker' ? 'image' : safeKind,
      mime: safeMime,
      bytes: bytes.length,
      sha256: digest,
    }));
    return {
      persisted,
      readyPersisted,
      duplicate: !persisted && !readyPersisted,
      status: 'captured',
      sha256: digest,
      path: target,
    };
  }

  function captureMediaFailure({ project, eventId, spoolPath, code }) {
    const paths = eventPaths(config.rootDir, project, eventId);
    validateSourcePath(paths, spoolPath);
    const allowedCodes = new Set([
      'media-download-failed',
      'media-download-timeout',
      'media-empty',
      'media-privacy-restricted',
      'media-too-large',
    ]);
    const reasonCode = allowedCodes.has(code) ? code : 'media-download-failed';
    if (reasonCode === 'media-download-failed' || reasonCode === 'media-download-timeout') {
      const persisted = atomicWriteExclusive(
        path.join(paths.eventRoot, 'media-error.md'),
        retryableMediaMarkdown({ eventId, error: reasonCode }),
      );
      return {
        persisted,
        readyPersisted: false,
        duplicate: !persisted,
        status: 'retryable',
        reasonCode,
      };
    }
    const persisted = atomicWriteExclusive(paths.readyPath, readyMarkdown({
      eventId,
      mediaState: 'failed',
      error: reasonCode,
    }));
    return {
      persisted,
      readyPersisted: persisted,
      duplicate: !persisted,
      status: 'failed',
      reasonCode,
    };
  }

  function mediaState(project, eventId) {
    const paths = eventPaths(config.rootDir, project, eventId);
    if (existsSync(paths.receiptPath)) return 'done';
    if (!existsSync(paths.readyPath)) return 'pending';
    try {
      const value = readFileSync(paths.readyPath, 'utf8');
      if (value.includes('media_state: "captured"')) return 'captured';
      if (value.includes('media_state: "failed"')) return 'failed';
      if (value.includes('media_state: "none"')) return 'none';
    } catch {}
    return 'invalid';
  }

  function listRawEventFiles(project, eventId) {
    const paths = eventPaths(config.rootDir, project, eventId);
    if (!existsSync(paths.eventRoot)) return [];
    return readdirSync(paths.eventRoot).sort();
  }

  return Object.freeze({
    enabled: config.enabled,
    routeCount: config.routes.length,
    configHash,
    routeFor,
    assertEgressAllowed,
    captureMessage,
    captureMedia,
    captureMediaFailure,
    mediaState,
    listRawEventFiles,
  });
}
