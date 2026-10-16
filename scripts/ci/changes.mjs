// Keep workflow selection tied to the checked-out build inputs, including SDK
// dependencies shared by the Python distribution and the two Rust workspaces.
import { execFileSync } from 'node:child_process';
import { appendFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const names = ['python', 'harness', 'lake', 'bicep', 'ui', 'main_image', 'thesis_image', 'main_deploy', 'thesis_deploy'];
const lakePython = /^src\/lake(?:client)?\/.*\.py$/;
const portableSdk = /^src\/harness\/(?:strategy-sdk|contracts|num)\//;

export function classify(paths, force = false) {
  const selected = Object.fromEntries(names.map(name => [name, force]));
  const choose = (...keys) => keys.forEach(key => { selected[key] = true; });
  for (const path of paths) {
    if (path === '.github/workflows/python-bicep-ci.yml' || path.startsWith('scripts/ci/changes')) {
      choose(...names);
    }
    if (path === 'Dockerfile' || path === '.dockerignore' || path === 'scripts/ci/merge_cargo_recipes.py') choose('python', 'harness', 'lake');
    if (path === 'Dockerfile.thesis' || path === 'scripts/ci/prepare_python_sources.py') choose('python');
    if (path.endsWith('.py') || /^(?:pyproject\.toml|uv\.lock)$/.test(path) || /^(?:tests|config|src\/thesis|src\/thesis_runtime)\//.test(path) || portableSdk.test(path) || path.startsWith('src/lake/contracts/')) {
      choose('python');
    }
    // Both workspaces depend on the other workspace's portable crates. Keep
    // cross-workspace behavior checks whenever either Rust tree changes.
    if (/^src\/(?:harness|lake)\//.test(path)) choose('harness', 'lake');
    if (/^config\/(?:harness|fees)\//.test(path)) choose('harness');
    if (/^config\/(?:lake|market-lake)\//.test(path)) choose('lake');
    if (path.startsWith('infra/')) choose('bicep', 'main_deploy', 'thesis_deploy');
    if (path.startsWith('src/ui/')) choose('ui');

    const distribution = /^(?:pyproject\.toml|uv\.lock)$/.test(path) || /^src\/(?:thesis|thesis_runtime)\//.test(path) || lakePython.test(path) || portableSdk.test(path) || path.startsWith('src/lake/contracts/');
    if (distribution || path === '.dockerignore') choose('main_image', 'thesis_image');
    if (path === 'Dockerfile' || /^src\/(?:harness|lake)\//.test(path) || path.startsWith('config/') || path === 'scripts/ci/merge_cargo_recipes.py' || path === 'scripts/ci/prepare_python_sources.py' || path === 'scripts/ci/installed_sdk_smoke.py' || path === '.github/workflows/publish-image.yml') choose('main_image');
    if (path === 'Dockerfile.thesis' || path.startsWith('config/thesis/') || path === 'scripts/ci/prepare_python_sources.py' || path === 'scripts/ci/installed_sdk_smoke.py') choose('thesis_image');
    if (path === '.github/workflows/deploy-main.yml' || path === '.github/workflows/quant-smoke.yml' || path === '.github/workflows/prune-acr.yml') choose('main_deploy');
    if (path === '.github/workflows/deploy-thesis.yml' || path === 'scripts/ci/wait_aca_revision.sh') choose('thesis_deploy', 'ui');
  }
  if (selected.main_image) selected.main_deploy = true;
  if (selected.thesis_image) selected.thesis_deploy = true;
  return selected;
}

export function changedPaths(base, head, event) {
  if (!/^[0-9a-f]{40}$/.test(head)) throw new Error('CHANGE_HEAD must be a full commit SHA');
  if (!base || /^0+$/.test(base) || !['push', 'pull_request'].includes(event)) return null;
  if (!/^[0-9a-f]{40}$/.test(base)) throw new Error('CHANGE_BASE must be a full commit SHA');
  const range = event === 'pull_request' ? `${base}...${head}` : `${base}..${head}`;
  return execFileSync('git', ['diff', '--name-only', '-z', range, '--'], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 }).split('\0').filter(Boolean);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const paths = changedPaths(process.env.CHANGE_BASE, process.env.CHANGE_HEAD, process.env.CHANGE_EVENT);
  const selected = classify(paths ?? [], paths === null);
  const output = Object.entries(selected).map(([key, value]) => `${key}=${value}\n`).join('');
  if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, output);
  process.stdout.write(output);
}
