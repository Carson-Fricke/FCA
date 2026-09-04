import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const PINNED_CORE_VERSION = "2026.8.9";

const npmGlobalRoot = execSync("npm root -g", { encoding: "utf8" }).trim();
const corePath = join(npmGlobalRoot, "@cleocode", "core");
const corePackageJson = JSON.parse(readFileSync(join(corePath, "package.json"), "utf8"));

if (corePackageJson.version !== PINNED_CORE_VERSION) {
  throw new Error(
    `This script pins @cleocode/core internals to ${PINNED_CORE_VERSION} but found ` +
      `${corePackageJson.version}. Internal dist paths likely moved; re-verify the ` +
      "embedding modules before using this workaround (see kryptobaseddev/cleo#1217).",
  );
}

const brainEmbeddingUrl = pathToFileURL(
  join(corePath, "dist", "memory", "brain-embedding.js"),
).href;
const observeUrl = pathToFileURL(
  join(corePath, "dist", "memory", "retrieval", "observe.js"),
).href;

const { initDefaultProvider, getEmbeddingProvider } = await import(brainEmbeddingUrl);
const { populateEmbeddings } = await import(observeUrl);
const memorySqliteUrl = pathToFileURL(
  join(corePath, "dist", "store", "memory-sqlite.js"),
).href;
const { getBrainNativeDb } = await import(memorySqliteUrl);

await initDefaultProvider();
const provider = getEmbeddingProvider();
if (!provider) {
  throw new Error("No embedding provider was registered by initDefaultProvider()");
}

await provider.embed("warmup");
console.log("Embedding pipeline primed; backfilling observations without vectors...");

const result = await populateEmbeddings(resolve(process.cwd()));

const nativeDb = getBrainNativeDb(resolve(process.cwd()));
const orphanIds = nativeDb
  .prepare(
    "SELECT e.id FROM brain_embeddings e " +
      "WHERE NOT EXISTS (SELECT 1 FROM brain_observations o WHERE o.id = e.id)",
  )
  .all();
let orphansDeleted = 0;
for (const row of orphanIds) {
  nativeDb.prepare("DELETE FROM brain_embeddings WHERE id = ?").run(row.id);
  orphansDeleted++;
}

console.log(JSON.stringify({ ...result, orphansDeleted }));
