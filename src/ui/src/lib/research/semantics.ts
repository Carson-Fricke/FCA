/** Pure output interpretation and bounded-preview mapping validation. */
import { researchPanels } from './registry';
import { finite, integer, type Dataset, type DatasetSemantics, type Evidence, type Panel } from './contracts';
import type { RecommendedDataset, FieldProfile } from './recommendations';
export interface SemanticOutput {
	dataset:Dataset; evidence:Evidence; dimensions:string[]; measures:string[]; ordered:string[];
	views:Panel[]; note:string; declared:boolean; profiles?:FieldProfile[];
}
export function describeOutput(evidence:Evidence, dataset?:Dataset, views:Panel[]=[], semantics?:DatasetSemantics, profile?:RecommendedDataset):SemanticOutput {
	const binding=semantics?.dataset ?? dataset ?? profile?.dataset ?? { id:'data',output:'Returned data',fields:evidence.columns.map(name=>{const present=evidence.rows.map(r=>r[name]).filter(v=>v!=null);const time=['series','events'].includes(String(evidence.output.kind))&&['event_time_ns','available_at_ns'].includes(name);return {name,label:name,description:'Scientific meaning was not published.',field_type:time?'timestamp_ns' as const:present.length&&present.every(v=>typeof v==='boolean')?'boolean' as const:present.length&&present.every(v=>finite(v)!==null)?'number' as const:'string' as const,unit:null};}),identity:null,selection_group:null,event_time:null,available_at:null };
	const dimensions=semantics?.dimensions??(profile?.declared?profile.dimensions:binding.fields.filter(f=>f.name!==binding.identity).map(f=>f.name));
	return { dataset:binding,evidence,views:[...(profile?.panels??[]),...(semantics?.preferred_views??[]),...views],dimensions,measures:semantics?.measures??profile?.measures??binding.fields.filter(f=>f.field_type==='number').map(f=>f.name),ordered:semantics?.ordered??profile?.ordered??binding.fields.filter(f=>['number','timestamp_ns'].includes(f.field_type)).map(f=>f.name),declared:!!semantics||!!profile?.declared,profiles:profile?.profiles,note:semantics?.row_meaning??profile?.row_meaning??'Scientific meaning was not published. Field types describe observed values; custom mappings remain available.' };
}
export function candidates(output:SemanticOutput,kind:string,channel:string):string[] {
	const role=researchPanels[kind]?.policy?.channels[channel];
	const names=role==='dimension'?output.dimensions:role==='ordered'?output.ordered:role==='measure'?output.measures:role==='quantitative'?[...output.dimensions,...output.measures]:[];
	return [...new Set(names)].filter(name=> role!=='measure'&&role!=='quantitative'||output.dataset.fields.find(f=>f.name===name)?.field_type==='number');
}
/** Manual exploration deliberately permits every published field and repeated axes. */
export function manualCandidates(output:SemanticOutput):string[] { return output.dataset.fields.map(f=>f.name); }
export function defaultPanel(output:SemanticOutput,kind:string):Panel {
	const saved=output.views.find(panel=>panel.kind===kind&&!mappingProblem(output,panel));
	if(saved)return {...saved,id:'custom'};
	const encodings:Record<string,string>={};
	for(const channel of Object.keys(researchPanels[kind]?.policy?.channels??{})) encodings[channel]=candidates(output,kind,channel).find(name=>!Object.values(encodings).includes(name))??'';
	return {id:'custom',dataset:output.dataset.id,kind,title:output.dataset.output,encodings,description:null};
}
export function customAvailable(output:SemanticOutput,kind:string):boolean {
	return !!researchPanels[kind] && output.dataset.fields.length>0;
}
export function mappingProblem(output:SemanticOutput,panel:Panel,evidence=output.evidence,manual=false):string|null {
	const extension=researchPanels[panel.kind];
	if(!extension)return 'This visual extension is unavailable. Inspect its source in Returned data.';
	for(const channel of extension.required)if(!panel.encodings[channel]||!output.dataset.fields.some(f=>f.name===panel.encodings[channel]))return 'Required field mappings are missing.';
	if(output.declared&&!manual)for(const channel of Object.keys(extension.policy?.channels??{}))if(!candidates(output,panel.kind,channel).includes(panel.encodings[channel]))return `Choose a compatible ${channel==='value'?'color measure':`${channel.toUpperCase()} field`}.`;
	const channels=Object.keys(extension.policy?.channels??{});
	if(!manual&&new Set(channels.map(c=>panel.encodings[c])).size!==channels.length)return 'Each axis and color measure must use a different field.';
	const coordinates=extension.policy?.coordinates?.(panel);
	if(coordinates){const seen=new Set<string>();for(const row of evidence.rows){
		if(coordinates.some(field=>row[field]==null))continue;
		const key=JSON.stringify(coordinates.map(field=>{const f=output.dataset.fields.find(f=>f.name===field);return f?.field_type==='number'?finite(row[field]):f?.field_type==='timestamp_ns'?String(integer(row[field])):row[field];}));
		if(seen.has(key))return panel.kind==='heatmap'?'Multiple observations occupy the same heatmap cell. Filter another dimension or inspect the table. No averaging is applied.':'Multiple observations share an X value within a series. Filter another dimension or inspect the table. No aggregation is applied.';
		seen.add(key);
	}}
	return extension.policy?.validate?.(panel,evidence.rows)??null;
}
export function recommendation(output:SemanticOutput):{panel:Panel;reason:string} {
	const declared=output.views.find(p=>p.kind!=='table'&&!mappingProblem(output,p));
	if(declared)return {panel:declared,reason:`${declared.description??declared.title} ${output.note}`};
	const numeric=output.dataset.fields.filter(f=>f.field_type==='number'&&f.name!==output.dataset.identity);
	const time=output.dataset.fields.find(f=>f.field_type==='timestamp_ns'&&f.name!=='available_at_ns');
	if(time&&numeric.length){const p={id:'suggested-sequence',dataset:output.dataset.id,kind:'line',title:'Published sequence',encodings:{x:time.name,y:numeric[0].name},description:null};if(!mappingProblem(output,p))return {panel:p,reason:output.note};}
	if(numeric.length>=2)return {panel:{id:'suggested-scatter',dataset:output.dataset.id,kind:'scatter',title:'Observed relationship',encodings:{x:numeric[0].name,y:numeric[1].name},description:null},reason:`Two quantitative fields on the same observations. ${output.note}`};
	return {panel:defaultPanel(output,'table'),reason:output.views.some(p=>p.kind!=='table')?'The declared chart needs filtering or a clearer row grain. Table preserves observations without aggregation.':output.note};
}
export function filteredEvidence(output:SemanticOutput,query:string,filters:Record<string,string>):Evidence {
	const identity=output.dataset.identity;const exact=identity&&output.evidence.rows.some(row=>String(row[identity])===query);
	return {...output.evidence,rows:output.evidence.rows.filter(row=>Object.entries(filters).every(([name,value])=>!value||JSON.stringify(row[name]??null)===value)&&(exact?String(row[identity!])===query:Object.values(row).join(' ').toLowerCase().includes(query.toLowerCase())))};
}
export function sortedEvidence(output:SemanticOutput,panel:Panel,evidence:Evidence):Evidence {
	if(panel.kind!=='line')return evidence;
	const field=panel.encodings.x;const timestamp=output.dataset.fields.find(f=>f.name===field)?.field_type==='timestamp_ns';
	return {...evidence,rows:[...evidence.rows].sort((a,b)=>{const left=timestamp?integer(a[field]):finite(a[field]);const right=timestamp?integer(b[field]):finite(b[field]);if(left===null)return right===null?0:1;if(right===null)return -1;return left<right?-1:left>right?1:0;})};
}
