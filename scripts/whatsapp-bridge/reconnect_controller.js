export function createReconnectCoordinator({
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
} = {}) {
  let generation = 0;
  let pendingTimer = null;

  function clearPending() {
    if (pendingTimer === null) return;
    clearTimeoutFn(pendingTimer);
    pendingTimer = null;
  }

  return {
    beginAttempt() {
      clearPending();
      generation += 1;
      return generation;
    },

    currentGeneration() {
      return generation;
    },

    isCurrent(candidate) {
      return candidate === generation;
    },

    schedule(candidate, delayMs, callback) {
      if (candidate !== generation) return false;
      clearPending();
      const scheduledGeneration = candidate;
      pendingTimer = setTimeoutFn(() => {
        pendingTimer = null;
        if (scheduledGeneration !== generation) return;
        callback();
      }, delayMs);
      return true;
    },

    cancelPending(candidate) {
      if (candidate !== generation) return false;
      clearPending();
      return true;
    },

    hasPendingReconnect() {
      return pendingTimer !== null;
    },

    cancel() {
      clearPending();
      generation += 1;
    },
  };
}
