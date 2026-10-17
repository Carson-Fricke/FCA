// Decide retention from the complete OCI manifest graph before any deletion.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

import { indexTypes, planRetention } from './image-retention.mjs';

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const directory = process.argv[2];
  const manifests = JSON.parse(readFileSync(join(directory, 'manifests.json'), 'utf8'));
  for (const manifest of manifests) {
    if (indexTypes.has(manifest.mediaType)) {
      const detail = JSON.parse(readFileSync(join(directory, 'indices', manifest.digest.slice(7) + '.json'), 'utf8'));
      if (detail.digest !== manifest.digest) throw new Error('Index metadata acknowledgement mismatch');
      manifest.references = detail.references;
    }
  }
  const references = readFileSync(join(directory, 'references.raw'), 'utf8').split('\n').filter(Boolean);
  const result = planRetention(manifests, references, {
    keepNewest: Number(process.env.KEEP_NEWEST), maxDeletions: Number(process.env.MAX_DELETIONS),
    deployedDigest: process.env.REPO_DEPLOYED_DIGEST || '',
    cacheCutoff: process.env.REPO_IS_CACHE === 'true' ? Date.now() - 7 * 24 * 3600 * 1000 : null,
  });
  writeFileSync(join(directory, 'delete-digests.json'), JSON.stringify(result.deletions));
  writeFileSync(join(directory, 'stats.json'), JSON.stringify(result.stats));
}
