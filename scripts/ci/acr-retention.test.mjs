import assert from 'node:assert/strict';
import test from 'node:test';
import { planRetention } from './acr-retention.mjs';

const digest = n => `sha256:${n.toString(16).padStart(64, '0')}`;
const image = (n, day, children = [], tags = []) => ({
  digest: digest(n), createdTime: `2026-10-${String(day).padStart(2, '0')}T00:00:00Z`, tags,
  mediaType: children.length ? 'application/vnd.oci.image.index.v1+json' : 'application/vnd.oci.image.manifest.v1+json',
  references: children.length ? children.map(n => ({ digest: digest(n) })) : null,
});
const limits = { keepNewest: 1, maxDeletions: 100 };

test('newest image retains old shared payloads, attestations and nested indices', () => {
  const rows = [image(1, 1), image(2, 2), image(3, 3, [1, 2]), image(4, 17, [3, 5]), image(5, 4), image(6, 5)];
  const result = planRetention(rows, [], limits);
  assert.deepEqual(result.deletions, [digest(6)]);
  assert.equal(result.stats.kept, 5);
});

test('deployed, revision-referenced, newest and operator-tagged roots retain their complete graphs', () => {
  const rows = [image(1, 1), image(2, 2, [1]), image(3, 3), image(4, 4, [3]), image(5, 5), image(6, 6, [5], ['operator-release']), image(7, 17), image(8, 8)];
  const result = planRetention(rows, [digest(4)], { ...limits, deployedDigest: digest(2) });
  assert.deepEqual(result.deletions, [digest(8)]);
  assert.equal(result.stats.kept, 7);
});

test('incomplete retained graphs and unhydrated indices abort without a deletion plan', () => {
  assert.throws(() => planRetention([image(2, 17, [1])], [], limits), /incomplete/);
  const root = image(2, 17, [1]);
  root.references = null;
  assert.throws(() => planRetention([image(1, 1), root], [], limits), /child-manifest metadata/);
  assert.throws(() => planRetention([image(1, 1)], [digest(9)], limits), /incomplete/);
});

test('deletions remain oldest-first and bounded, with a grace window for cache manifests', () => {
  const rows = [image(1, 1), image(2, 2), image(3, 3), image(4, 17)];
  assert.deepEqual(planRetention(rows, [], { ...limits, maxDeletions: 1 }), {
    deletions: [digest(1)], stats: { total: 4, kept: 1, deleting: 1, remaining: 2 },
  });
  assert.deepEqual(planRetention(rows, [], { ...limits, cacheCutoff: Date.parse('2026-10-03T00:00:00Z') }).deletions, [digest(1), digest(2)]);
  assert.deepEqual(planRetention([image(1, 1), image(2, 16, [1]), image(3, 17)], [], {
    ...limits, cacheCutoff: Date.parse('2026-10-10T00:00:00Z'),
  }).deletions, []);
});
