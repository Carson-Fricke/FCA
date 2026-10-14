/** Presentation profiles are observed facts, separate from author-supplied meaning. */
import type { Dataset, Field, Panel } from './contracts';
export interface FieldProfile extends Field {
 count:number; missing:number; cardinality:number; range:[number|string,number|string]|null;
 quantiles:(number|string)[]|null; top_values:{value:string;count:number}[];
 histogram:{lower:number;upper:number;count:number}[];
}
export interface RecommendedDataset {
 instanceId:string; name:string; dataset:Dataset; row_meaning:string; declared:boolean;
 dimensions:string[]; measures:string[]; ordered:string[]; profiles:FieldProfile[];
 total:number; panels:Panel[]; candidates:Panel[];
}
export interface StudyRecommendations {
 job_id:string; artifactSha256:string; status:'ranked'|'deterministic'; note:string;
 receipt:{model:string;usage?:Record<string,unknown>}; datasets:RecommendedDataset[];
}
