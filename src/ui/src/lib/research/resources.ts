/** Observational, bounded artifact loading shared by chat and full-size exhibits. */
import { apiBaseUrl, backendKind, getExploratoryOutput, type ExploratoryOutputPage } from '$lib/api';
import { decodeEvidence, decodeView, decodeSemantics, type Evidence, type Exhibit, type DatasetSemantics } from './contracts';

export type PageReader = typeof getExploratoryOutput;
const pages = new Map<string, Promise<ExploratoryOutputPage>>();
const MAX_RECORDS = 5000;
const MAX_BYTES = 2_000_000;

async function page(reader: PageReader, job: string, instance?: string, name?: string, offset = 0) {
 // Only production immutable pages are shared; injected readers have isolated lifetimes.
 const key = JSON.stringify([backendKind, apiBaseUrl, job, instance, name, offset]);
 const cached = reader === getExploratoryOutput ? pages.get(key) : undefined;
 if (cached) return cached;
 const request = reader(job, instance, name, offset).then((result) => {
  if (result.job_id !== job || result.artifactName !== 'exploratory_outputs.json' || !/^[a-f0-9]{64}$/.test(result.artifactSha256)) throw new Error('Evidence identity mismatch');
  if (name && (result.instanceId !== instance || result.name !== name)) throw new Error('Output identity mismatch');
  return result;
 });
 if (reader === getExploratoryOutput) {
  pages.set(key, request); if (pages.size > 64) pages.delete(pages.keys().next().value!);
  request.catch(() => { if (pages.get(key) === request) pages.delete(key); });
 }
 return request;
}

export function researchResources(job: string, reader: PageReader = getExploratoryOutput) {
 let sha: string | undefined;
 const check = (p: ExploratoryOutputPage) => { if (sha && p.artifactSha256 !== sha) throw new Error('Artifact changed during inspection'); sha = p.artifactSha256; return p; };
 return {
  async semantics(instance:string,name:string,signal:AbortSignal):Promise<DatasetSemantics> {
   const p=check(await page(reader,job,instance,name));if(signal.aborted)throw new DOMException('Cancelled','AbortError');
   if(p.output?.kind!=='dataset_semantics')throw new Error('Output is not dataset semantics');
   return decodeSemantics(p.output.semantics);
  },
  async catalog(signal: AbortSignal) {
   const outputs: NonNullable<ExploratoryOutputPage['outputs']> = [];
   let offset = 0;
   while (!signal.aborted) {
    const p = check(await page(reader, job, undefined, undefined, offset)); if (signal.aborted) throw new DOMException('Cancelled', 'AbortError');
    outputs.push(...p.outputs ?? []);
    if (p.nextOffset == null) return { outputs, sha: p.artifactSha256 };
    if (p.nextOffset <= offset || outputs.length >= 128) throw new Error('Output catalog exceeds inspection budget');
    offset = p.nextOffset;
   }
   throw new DOMException('Cancelled', 'AbortError');
  },
  async evidence(instance: string, name: string, signal: AbortSignal): Promise<Evidence> {
   let offset = 0; let bytes = 0; let combined: Record<string, unknown> | undefined; let total = 0;
   while (!signal.aborted) {
    const p = check(await page(reader, job, instance, name, offset)); if (signal.aborted) throw new DOMException('Cancelled', 'AbortError');
    if (!p.output) throw new Error('Missing output evidence');
    const sealed = p.output.kind === 'causal_observations';
    const records = sealed ? p.output.dataset as Record<string, unknown> : p.output;
    if (!records || typeof records !== 'object') throw new Error('Invalid causal observation dataset');
    const key = ['rows', 'points', 'events'].find((k) => Array.isArray(records[k]));
    bytes += JSON.stringify(p.output).length;
    total = p.total ?? 0;
    if (!combined) combined = sealed
     ? { ...p.output, dataset: { ...records, ...(key ? { [key]: [] } : {}) } }
     : { ...p.output, ...(key ? { [key]: [] } : {}) };
    const combinedRecords = sealed ? combined.dataset as Record<string, unknown> : combined;
    if (key) (combinedRecords[key] as unknown[]).push(...records[key] as unknown[]);
    if (p.nextOffset == null) return decodeEvidence(combined, key ? total : 0, false);
    if (p.nextOffset <= offset) throw new Error('Evidence cursor did not advance');
    if (p.nextOffset >= MAX_RECORDS || bytes >= MAX_BYTES) return decodeEvidence(combined, total, true);
    offset = p.nextOffset;
   }
   throw new DOMException('Cancelled', 'AbortError');
  },
  async exhibit(instance: string, name: string, signal: AbortSignal): Promise<Exhibit> {
   const p = check(await page(reader, job, instance, name)); if (signal.aborted) throw new DOMException('Cancelled', 'AbortError');
   if (p.output?.kind !== 'research_view') throw new Error('Output is not a research view');
   return { instanceId: instance, name, view: decodeView(p.output.view) };
  }
 };
}
