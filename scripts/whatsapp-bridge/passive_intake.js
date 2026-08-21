import path from 'path';
import {
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from 'fs';
import {
  createCipheriv,
  createDecipheriv,
  createHash,
  createHmac,
  randomBytes,
} from 'crypto';

const PROJECT_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const GROUP_JID_PATTERN = /^[0-9][0-9-]{4,63}@g\.us$/;
const MAX_ROUTES = 32;
const MAX_TEXT_LENGTH = 32_768;
const MAX_FILENAME_LENGTH = 255;
const MAX_MIME_LENGTH = 128;

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
    throw new PassiveIntakeConfigError('enabled passive intake requires an absolute spool root');
  }

  const seenProjects = new Set();
  const seenJids = new Set();
  const routes = routesValue.map((route, index) => {
    if (!isPlainObject(route)) {
      throw new PassiveIntakeConfigError(`passive intake route ${index} must be an object`);
    }
    assertOnlyKeys(route, new Set(['project', 'jid']), `passive intake route ${index}`);
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
    seenProjects.add(project);
    seenJids.add(jid);
    return Object.freeze({ project, jid });
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
      mime: boundedString(value.mimetype, MAX_MIME_LENGTH),
      fileName: boundedString(value.fileName, MAX_FILENAME_LENGTH),
      bytes: Number.isSafeInteger(Number(value.fileLength)) && Number(value.fileLength) >= 0
        ? Number(value.fileLength)
        : null,
      mediaCaptured: false,
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
    writeFileSync(descriptor, data, { encoding: 'utf8' });
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = undefined;
    try {
      // A hard-link publish is create-if-absent on both Windows and POSIX.
      // Unlike rename(), it cannot silently replace an event persisted by a
      // concurrent replay.
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

function readOrCreateSecret(secretPath) {
  mkdirSync(path.dirname(secretPath), { recursive: true });
  if (!existsSync(secretPath)) {
    atomicWriteExclusive(secretPath, randomBytes(32).toString('hex'));
  }
  const encoded = readFileSync(secretPath, 'utf8').trim();
  if (!/^[a-f0-9]{64}$/.test(encoded)) {
    throw new Error('passive intake secret material is invalid');
  }
  return Buffer.from(encoded, 'hex');
}

function encryptEnvelope(envelope, key) {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  const plaintext = Buffer.from(JSON.stringify(envelope), 'utf8');
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  return {
    schema: 'EncryptedIntakeEnvelopeV1',
    algorithm: 'A256GCM',
    keyId: createHash('sha256').update(key).digest('hex').slice(0, 16),
    iv: iv.toString('base64'),
    tag: cipher.getAuthTag().toString('base64'),
    ciphertext: ciphertext.toString('base64'),
  };
}

function decryptEnvelope(record, key) {
  if (!isPlainObject(record) || record.schema !== 'EncryptedIntakeEnvelopeV1' || record.algorithm !== 'A256GCM') {
    throw new Error('passive intake spool record has an unsupported format');
  }
  const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(record.iv, 'base64'));
  decipher.setAuthTag(Buffer.from(record.tag, 'base64'));
  const plaintext = Buffer.concat([
    decipher.update(Buffer.from(record.ciphertext, 'base64')),
    decipher.final(),
  ]);
  return JSON.parse(plaintext.toString('utf8'));
}

function safeProjectRoot(rootDir, project) {
  const resolvedRoot = path.resolve(rootDir);
  const projectRoot = path.resolve(resolvedRoot, project);
  if (projectRoot !== resolvedRoot && !projectRoot.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw new Error('passive intake project path escaped the spool root');
  }
  return projectRoot;
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
    if (msg?.key?.fromMe) {
      return { matched: true, project: route.project, persisted: false, reason: 'from_me' };
    }

    const message = unwrapMessage(msg?.message);
    const ingestedAtMs = now();
    const occurredAtMs = timestampMilliseconds(msg?.messageTimestamp, ingestedAtMs);
    const projectRoot = safeProjectRoot(config.rootDir, route.project);
    const privateRoot = path.join(projectRoot, 'private');
    const actorKey = readOrCreateSecret(path.join(privateRoot, 'pseudonym.key'));
    const storageKey = readOrCreateSecret(path.join(privateRoot, 'storage.key'));
    const nativeId = boundedString(msg?.key?.id, 256);
    const contextInfo = contextInfoFor(message);
    const text = firstText(message);
    const contentKind = contentKindFor(message);
    const actorId = createHmac('sha256', actorKey)
      .update(boundedString(senderId, 256))
      .digest('hex');
    const routeId = createHmac('sha256', actorKey).update(chatId).digest('hex');
    const eventMaterial = nativeId || JSON.stringify({ occurredAtMs, actorId, contentKind, text });
    const eventId = createHash('sha256')
      .update(`${route.project}\u0000${routeId}\u0000${eventMaterial}`)
      .digest('hex');
    const envelope = {
      schema: 'IntakeEnvelopeV1',
      version: 1,
      intakeId: `wa_${eventId}`,
      project: route.project,
      source: 'whatsapp',
      nativeId: nativeId || null,
      occurredAt: new Date(occurredAtMs).toISOString(),
      ingestedAt: new Date(ingestedAtMs).toISOString(),
      actor: { id: `wa_actor_${actorId}`, pseudonymized: true },
      route: { id: `wa_group_${routeId}`, kind: 'whatsapp-group' },
      content: {
        kind: contentKind,
        text,
        replyToNativeId: boundedString(contextInfo.stanzaId, 256) || null,
        attachments: attachmentMetadata(message),
      },
      integrity: {
        eventHash: eventId,
        configHash,
      },
    };
    const date = new Date(occurredAtMs);
    const datePath = [
      String(date.getUTCFullYear()),
      String(date.getUTCMonth() + 1).padStart(2, '0'),
      String(date.getUTCDate()).padStart(2, '0'),
    ];
    const spoolPath = path.join(projectRoot, 'spool', ...datePath, `${eventId}.json`);
    const encryptedRecord = encryptEnvelope(envelope, storageKey);
    const persisted = atomicWriteExclusive(spoolPath, `${JSON.stringify(encryptedRecord)}\n`);
    return {
      matched: true,
      project: route.project,
      eventId,
      persisted,
      duplicate: !persisted,
      spoolPath,
    };
  }

  function readEnvelope(spoolPath, project) {
    const projectRoot = safeProjectRoot(config.rootDir, project);
    const resolvedSpoolPath = path.resolve(spoolPath);
    if (!resolvedSpoolPath.startsWith(`${projectRoot}${path.sep}`)) {
      throw new Error('passive intake spool path is outside the project root');
    }
    const storageKey = readOrCreateSecret(path.join(projectRoot, 'private', 'storage.key'));
    return decryptEnvelope(JSON.parse(readFileSync(resolvedSpoolPath, 'utf8')), storageKey);
  }

  return Object.freeze({
    enabled: config.enabled,
    routeCount: config.routes.length,
    configHash,
    routeFor,
    assertEgressAllowed,
    captureMessage,
    readEnvelope,
  });
}
