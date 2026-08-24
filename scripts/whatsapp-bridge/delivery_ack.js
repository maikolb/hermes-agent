const STATUS = Object.freeze({
  ERROR: 0,
  PENDING: 1,
  SERVER_ACK: 2,
  DELIVERY_ACK: 3,
  READ: 4,
  PLAYED: 5,
});

function normalizeStatus(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (/^\d+$/.test(trimmed)) return Number.parseInt(trimmed, 10);
    return STATUS[trimmed.toUpperCase()] ?? null;
  }
  return null;
}

export function createDeliveryAckTracker({ timeoutMs = 10000, maxEntries = 1024 } = {}) {
  const observed = new Map();
  const waiters = new Map();

  function remember(messageId, status) {
    observed.delete(messageId);
    observed.set(messageId, status);
    while (observed.size > maxEntries) {
      observed.delete(observed.keys().next().value);
    }
  }

  function settle(messageId, status) {
    if (!messageId) return;
    const normalized = normalizeStatus(status);
    if (normalized === null) return;
    remember(String(messageId), normalized);

    const waiter = waiters.get(String(messageId));
    if (!waiter) return;
    if (normalized === STATUS.ERROR) {
      clearTimeout(waiter.timer);
      waiters.delete(String(messageId));
      waiter.reject(new Error(`outbound message ${messageId} was rejected by WhatsApp`));
    } else if (normalized >= STATUS.SERVER_ACK) {
      clearTimeout(waiter.timer);
      waiters.delete(String(messageId));
      waiter.resolve(normalized);
    }
  }

  function observeUpdates(updates) {
    for (const { key, update } of updates || []) {
      settle(key?.id, update?.status);
    }
  }

  function observeMessage(message) {
    if (!message?.key?.fromMe || !message.key.id) return;
    const explicit = normalizeStatus(message.status);
    // A fromMe messages.upsert event is a server echo.  Even when Baileys does
    // not attach a numeric status, that echo proves the server accepted it.
    settle(message.key.id, explicit === null ? STATUS.SERVER_ACK : explicit);
  }

  function wait(messageId) {
    const id = String(messageId || '');
    if (!id) return Promise.reject(new Error('outbound send returned no message id'));

    const status = observed.get(id);
    if (status === STATUS.ERROR) {
      return Promise.reject(new Error(`outbound message ${id} was rejected by WhatsApp`));
    }
    if (status >= STATUS.SERVER_ACK) return Promise.resolve(status);

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        waiters.delete(id);
        reject(new Error(`delivery acknowledgement timed out for outbound message ${id}`));
      }, timeoutMs);
      waiters.set(id, { resolve, reject, timer });
    });
  }

  return { observeMessage, observeUpdates, wait };
}

export function assertOutboundDestination(sent, requestedChatId) {
  const actual = String(sent?.key?.remoteJid || '').trim().toLowerCase();
  const expected = String(requestedChatId || '').trim().toLowerCase();
  if (!actual || !expected || actual !== expected) {
    throw new Error(`outbound destination mismatch: expected ${expected || '<missing>'}, got ${actual || '<missing>'}`);
  }
}
