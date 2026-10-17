// Hydrate OCI descriptors read-only, then map the shared deletion plan to GHCR IDs.
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { indexTypes, planRetention } from './image-retention.mjs';

const directory = process.argv[2];
const versions = [];
for (let page = 1; existsSync(join(directory, `page-${page}.json`)); page++) {
  versions.push(...JSON.parse(readFileSync(join(directory, `page-${page}.json`), 'utf8')));
}
if (versions.some(v => !/^sha256:[0-9a-f]{64}$/.test(v.name))) throw new Error('Package version name is not a manifest digest');
const owner = process.env.GITHUB_REPOSITORY_OWNER.toLowerCase();
const packageName = process.env.PACKAGE_NAME;
if (!/^[a-z0-9._-]+$/.test(owner) || !/^[a-z0-9._-]+$/.test(packageName)) throw new Error('Invalid registry package identity');
const authentication = await fetch(`https://ghcr.io/token?service=ghcr.io&scope=${encodeURIComponent(`repository:${owner}/${packageName}:pull`)}`, {
  headers: { Authorization: `Basic ${Buffer.from(`${process.env.GITHUB_ACTOR}:${process.env.GH_TOKEN}`).toString('base64')}` },
  signal: AbortSignal.timeout(30000), redirect: 'error',
});
if (!authentication.ok) throw new Error(`Registry authentication failed (${authentication.status})`);
const { token } = await authentication.json();
if (!token) throw new Error('Registry authentication returned no token');
const manifests = new Array(versions.length);
let cursor = 0;
await Promise.all(Array.from({ length: Math.min(8, versions.length) }, async () => {
  while (cursor < versions.length) {
    const index = cursor++;
    const version = versions[index];
    const response = await fetch(`https://ghcr.io/v2/${owner}/${packageName}/manifests/${version.name}`, {
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' },
      signal: AbortSignal.timeout(30000), redirect: 'error',
    });
    if (!response.ok) throw new Error(`Manifest hydration failed for ${version.name} (${response.status}); cleanup aborted`);
    const descriptor = await response.json();
    manifests[index] = {
      digest: version.name, createdTime: version.created_at, tags: version.metadata.container.tags,
      mediaType: descriptor.mediaType,
      references: indexTypes.has(descriptor.mediaType) ? descriptor.manifests : null,
    };
  }
}));
const plan = planRetention(manifests, [], {
  keepNewest: Number(process.env.KEEP_NEWEST), maxDeletions: 100,
  cacheCutoff: Date.now() - Number(process.env.MIN_AGE_DAYS) * 24 * 3600 * 1000,
});
const ids = new Map(versions.map(v => [v.name, v.id]));
writeFileSync(join(directory, 'delete-ids.json'), JSON.stringify(plan.deletions.map(digest => ids.get(digest))));
writeFileSync(join(directory, 'stats.json'), JSON.stringify(plan.stats));
console.log(JSON.stringify(plan.stats));
