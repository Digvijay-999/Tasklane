import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

export const SAMPLES = resolve(import.meta.dirname, '.samples');
const REPO = resolve(import.meta.dirname, '..', '..');
const API = process.env.TASKLANE_API_URL ?? 'http://127.0.0.1:8000';

/** Check the stack is up with the demo settings, and generate the sample files once. */
export default async function globalSetup() {
  let meta: { fault_injection_enabled: boolean };
  try {
    meta = (await (await fetch(`${API}/v1/meta`)).json()) as typeof meta;
  } catch {
    throw new Error(`The Tasklane API is not reachable at ${API}. Start the stack first (docker compose up -d).`);
  }
  if (!meta.fault_injection_enabled) {
    throw new Error('Start the stack with the demo settings (ENABLE_FAULT_INJECTION=true, STUB_DELAY_SECONDS=1, ...): see WALKTHROUGH_PHASE9.md.');
  }
  if (!existsSync(resolve(SAMPLES, 'report.pdf')) || !existsSync(resolve(SAMPLES, 'repo.zip'))) {
    mkdirSync(SAMPLES, { recursive: true });
    for (const tool of ['devtools.pdf_samples', 'devtools.zip_samples']) {
      execFileSync('docker', ['compose', 'run', '--rm', '-v', `${SAMPLES}:/out`, 'api', 'python', '-m', tool, '/out'], {
        cwd: REPO,
        stdio: 'ignore',
      });
    }
  }
}
