// Prevent an ordinary push from silently leaving SDK or other repository edits behind.
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

export function pendingPaths(status) {
  const records = status.split('\0');
  const paths = [];
  for (let index = 0; index < records.length; index++) {
    const record = records[index];
    if (!record) continue;
    paths.push(record.slice(3));
    if (/[RC]/.test(record.slice(0, 2))) index++;
  }
  return paths;
}

export function deliveryAllowed(paths, explicitUserReason) {
  return paths.length === 0 || Boolean(explicitUserReason?.trim());
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const paths = pendingPaths(execFileSync('git', ['status', '--porcelain=v1', '-z', '--untracked-files=all'], { encoding: 'utf8' }));
  const reason = process.env.FCA_EXPLICIT_PARTIAL_PUSH_REASON;
  if (!deliveryAllowed(paths, reason)) {
    console.error(`Push blocked: ${paths.length} repository changes remain uncommitted:\n${paths.slice(0, 30).join('\n')}`);
    console.error('Commit all pending changes. A partial push requires an explicit user request; see AGENTS.md.');
    process.exitCode = 1;
  } else if (paths.length) {
    console.log(`Partial push explicitly requested by the user: ${reason.trim()}`);
  } else {
    console.log('Delivery check: no repository changes left uncommitted.');
  }
}
