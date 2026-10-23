// Enforce Git's text classification and LF policy without touching binary data.
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export function inventory(output) {
  return output.split('\0').filter(Boolean).map(record => {
    const tab = record.indexOf('\t');
    if (tab < 0) throw new Error('Invalid Git line-ending inventory');
    const metadata = record.slice(0, tab);
    return {
      path: record.slice(tab + 1),
      index: metadata.match(/(?:^|\s)i\/(\S+)/)?.[1],
      worktree: metadata.match(/(?:^|\s)w\/(\S+)/)?.[1],
      binary: /attr\/-text(?:\s|$)/.test(metadata),
    };
  });
}

export function violations(entries, indexOnly = false) {
  const invalid = value => value === 'crlf' || value === 'mixed';
  return entries.filter(entry => !entry.binary && (
    invalid(entry.index) || (!indexOnly && invalid(entry.worktree))
  ));
}

export function normalizeLF(bytes) {
  return Buffer.from(bytes.filter((byte, index) => byte !== 13 || bytes[index + 1] !== 10));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const args = process.argv.slice(2);
  if (args.some(arg => !['--check', '--fix', '--index'].includes(arg))) throw new Error('Use --check, --fix, or --check --index');
  if (args.includes('--fix') && args.includes('--index')) throw new Error('--fix edits the working tree, not the index');
  const readInventory = () => inventory(execFileSync('git', [
    'ls-files', '--eol', '-z', '--cached', '--others', '--exclude-standard',
  ], { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }));
  if (args.includes('--fix')) {
    const changed = readInventory().filter(entry => !entry.binary && ['crlf', 'mixed'].includes(entry.worktree));
    for (const { path } of changed) writeFileSync(path, normalizeLF(readFileSync(path)));
    console.log(`Normalized ${changed.length} text files to LF; binary and ignored files were untouched.`);
  }
  const problems = violations(readInventory(), args.includes('--index'));
  if (problems.length) {
    console.error(`LF required in ${problems.length} text files:\n${problems.slice(0, 30).map(entry => entry.path).join('\n')}`);
    console.error('Run node scripts/ci/line-endings.mjs --fix, then git add --renormalize .');
    process.exitCode = 1;
  } else {
    console.log('Repository text line endings: LF.');
  }
}
