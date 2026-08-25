import path from 'path';
import { spawn } from 'child_process';

/**
 * Fire the existing Hermes cross-profile cron executor after raw is durable.
 * The child is detached and the cron store claim owns de-duplication.
 */
export function createPassiveIntakeWakeDispatcher({
  pythonExecutable = '',
  agentRoot = '',
  spawnProcess = spawn,
  environment = process.env,
} = {}) {
  const python = String(pythonExecutable || '').trim();
  const root = String(agentRoot || '').trim();
  const runtimeConfigured = path.isAbsolute(python) && path.isAbsolute(root);

  function dispatch(route) {
    const wake = route?.wake;
    if (!wake) return false;
    if (!runtimeConfigured) {
      throw new Error('passive intake wake runtime is not configured with absolute paths');
    }
    const child = spawnProcess(
      python,
      [
        '-m',
        'hermes_cli.passive_intake_wake',
        '--profile',
        wake.profile,
        '--job-id',
        wake.cronJobId,
      ],
      {
        cwd: root,
        detached: true,
        env: { ...environment, HERMES_INTAKE_EVENT_WAKE: '1' },
        stdio: 'ignore',
        windowsHide: true,
      },
    );
    child.unref();
    return true;
  }

  return Object.freeze({ dispatch, runtimeConfigured });
}
