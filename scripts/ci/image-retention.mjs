// Pure retention policy shared by private mirrors and upstream packages.
const digestPattern = /^sha256:[0-9a-f]{64}$/;
export const indexTypes = new Set([
  'application/vnd.oci.image.index.v1+json',
  'application/vnd.docker.distribution.manifest.list.v2+json',
]);
const imageTypes = new Set(['application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json']);

export function planRetention(manifests, references, { keepNewest, maxDeletions, deployedDigest = '', cacheCutoff = null }) {
  if (!Number.isInteger(keepNewest) || keepNewest < 1 || !Number.isInteger(maxDeletions) || maxDeletions < 0) {
    throw new Error('Retention limits must be nonnegative integers, with at least one newest image retained');
  }
  if (cacheCutoff !== null && !Number.isFinite(cacheCutoff)) throw new Error('Invalid retention cutoff');
  const rows = manifests.map(m => {
    const created = Date.parse(m.createdTime);
    if (!digestPattern.test(m.digest) || !Number.isFinite(created) || (m.tags != null && !Array.isArray(m.tags)) || (!indexTypes.has(m.mediaType) && !imageTypes.has(m.mediaType))) {
      throw new Error('Manifest metadata requires a digest, creation time and valid tags');
    }
    if (indexTypes.has(m.mediaType) && (!Array.isArray(m.references) || m.references.length === 0)) {
      throw new Error(`Index ${m.digest} is missing its child-manifest metadata`);
    }
    const children = indexTypes.has(m.mediaType) ? m.references.map(r => r.digest) : [];
    if (children.some(d => !digestPattern.test(d))) throw new Error('Invalid child-manifest digest');
    return { digest: m.digest, created, tags: m.tags ?? [], children };
  }).sort((a, b) => b.created - a.created);
  const byDigest = new Map(rows.map(r => [r.digest, r]));
  if (byDigest.size !== rows.length) throw new Error('Duplicate manifest digest');
  const children = new Set(rows.flatMap(r => r.children));
  const roots = rows.filter(r => !children.has(r.digest));
  const keep = new Set(roots.slice(0, keepNewest).map(r => r.digest));
  if (deployedDigest) keep.add(deployedDigest);
  references.forEach(d => keep.add(d));
  rows.filter(r => r.tags.some(t => !/^[0-9a-f]{40}$/.test(t))).forEach(r => keep.add(r.digest));
  if (cacheCutoff !== null) rows.filter(r => r.created >= cacheCutoff).forEach(r => keep.add(r.digest));
  // References protect the entire graph, including untagged platform images,
  // attestations and nested indices. Missing metadata aborts before mutation.
  const pending = [...keep];
  const visited = new Set();
  while (pending.length) {
    const digest = pending.pop();
    if (visited.has(digest)) continue;
    const row = byDigest.get(digest);
    if (!row) throw new Error(`Retained image graph is incomplete: ${digest}`);
    visited.add(digest);
    for (const child of row.children) {
      keep.add(child);
      pending.push(child);
    }
  }
  const candidates = rows.filter(r => !keep.has(r.digest))
    .sort((a, b) => a.created - b.created);
  const deletions = candidates.slice(0, maxDeletions).map(r => r.digest);
  return { deletions, stats: { total: rows.length, kept: keep.size, deleting: deletions.length, remaining: candidates.length - deletions.length } };
}
