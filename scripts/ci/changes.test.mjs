import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import test from 'node:test';
import { classify, changedPaths } from './changes.mjs';

test('repository text policy changes verify and rebuild both SDK consumers', () => {
  for (const path of ['.gitattributes', '.editorconfig']) {
    const result = classify([path]);
    for (const key of ['python', 'harness', 'lake', 'ui', 'main_image', 'thesis_image']) assert.equal(result[key], true, `${path}: ${key}`);
  }
});

test('UI-only changes select UI without rebuilding either backend', () => {
  const result = classify(['src/ui/src/routes/+page.svelte']);
  assert.equal(result.ui, true);
  for (const key of ['python', 'harness', 'lake', 'main_image', 'main_deploy', 'thesis_image', 'thesis_deploy']) assert.equal(result[key], false, key);
});

test('portable SDK changes retain cross-product verification and both images', () => {
  for (const path of ['src/harness/strategy-sdk/src/lib.rs', 'src/harness/contracts/src/lib.rs', 'src/harness/num/src/lib.rs', 'src/lake/contracts/src/lib.rs']) {
    const result = classify([path]);
    for (const key of ['python', 'harness', 'lake', 'main_image', 'thesis_image']) assert.equal(result[key], true, `${path}: ${key}`);
  }
});

test('nonportable Rust changes retain both Rust suites without Thesis rebuilds', () => {
  const result = classify(['src/lake/ingestion/src/equities.rs']);
  for (const key of ['harness', 'lake', 'main_image']) assert.equal(result[key], true);
  for (const key of ['python', 'thesis_image', 'thesis_deploy', 'ui']) assert.equal(result[key], false);
});

test('distribution and infrastructure inputs select their actual consumers', () => {
  const python = classify(['src/thesis_runtime/sessions/session.py']);
  for (const key of ['python', 'main_image', 'thesis_image', 'main_deploy', 'thesis_deploy']) assert.equal(python[key], true);
  assert.equal(python.harness, false);
  const infra = classify(['infra/modules/market-lake.bicep']);
  for (const key of ['bicep', 'main_deploy', 'thesis_deploy']) assert.equal(infra[key], true);
  assert.equal(infra.main_image, false);
  assert.equal(infra.thesis_image, false);
});

test('manual and initial branch runs select every required check', () => {
  assert.ok(Object.values(classify([], true)).every(Boolean));
  assert.equal(changedPaths('', 'a'.repeat(40), 'workflow_dispatch'), null);
  assert.equal(changedPaths('0'.repeat(40), 'a'.repeat(40), 'push'), null);
  assert.throws(() => changedPaths('--bad-ref', 'a'.repeat(40), 'push'));
});

test('commit-range execution selects deletions and PR changes without unrelated base changes', () => {
  const script = resolve('scripts/ci/changes.mjs');
  const directory = mkdtempSync(`${tmpdir()}/fca-change-selection-`);
  const git = (...args) => execFileSync('git', args, { cwd: directory, encoding: 'utf8' }).trim();
  const write = (name, content) => writeFileSync(`${directory}/${name}`, content);
  try {
    git('init', '--quiet');
    git('config', 'user.name', 'CI fixture');
    git('config', 'user.email', 'ci-fixture@example.invalid');
    write('initial.py', 'before');
    git('add', '.');
    git('commit', '--quiet', '-m', 'base');
    const base = git('rev-parse', 'HEAD');
    git('checkout', '--quiet', '-b', 'feature');
    rmSync(`${directory}/initial.py`);
    git('add', '.');
    git('commit', '--quiet', '-m', 'delete Python input');
    const head = git('rev-parse', 'HEAD');
    git('checkout', '--quiet', '-b', 'updated-base', base);
    mkdirSync(`${directory}/infra`);
    write('infra/unrelated.bicep', '');
    git('add', '.');
    git('commit', '--quiet', '-m', 'unrelated base update');
    const updatedBase = git('rev-parse', 'HEAD');
    const output = execFileSync(process.execPath, [script], {
      cwd: directory,
      encoding: 'utf8',
      env: { ...process.env, GITHUB_OUTPUT: '', CHANGE_BASE: updatedBase, CHANGE_HEAD: head, CHANGE_EVENT: 'pull_request' },
    });
    assert.match(output, /^python=true$/m);
    assert.match(output, /^bicep=false$/m);
    assert.match(output, /^harness=false$/m);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
