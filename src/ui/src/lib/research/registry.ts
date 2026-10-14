/** Local extension registration. Evidence never supplies executable components. */
import type { Component } from 'svelte';
import type { Dataset, Evidence, Panel, Selection } from './contracts';
import PlotPanel from './panels/PlotPanel.svelte';
import TablePanel from './panels/TablePanel.svelte';
import { builtinPolicies, type MappingPolicy } from './policies';

export interface PanelProps { panel: Panel; dataset: Dataset; evidence: Evidence; selection: Selection | null; onselect: (selection: Selection | null) => void; cursor: string | null; oncursor: (value: string | null) => void }
export interface PanelExtension { component: Component<PanelProps>; required: string[]; label?:string; policy?:MappingPolicy }
export const researchPanels: Record<string, PanelExtension> = Object.assign(Object.create(null), {
 table: { component: TablePanel, required: [] },
 heatmap: { component: PlotPanel, required: ['x', 'y', 'value'] },
 line: { component: PlotPanel, required: ['x', 'y'] },
 scatter: { component: PlotPanel, required: ['x', 'y'] },
 comparison: { component: PlotPanel, required: ['x', 'y'] },
 distribution: { component: PlotPanel, required: ['x', 'y'] },
 events: { component: PlotPanel, required: ['x', 'y'] },
 intervals: { component: PlotPanel, required: ['x', 'end', 'y'] },
 composition: { component: PlotPanel, required: ['x', 'y', 'z'] },
 graph: { component: PlotPanel, required: ['source', 'target'] }
});
for(const [kind,policy] of Object.entries(builtinPolicies))researchPanels[kind].policy=policy;
for(const [kind,label] of Object.entries({table:'Table',heatmap:'Heatmap',scatter:'Scatterplot',line:'Sequence',comparison:'Comparison'}))researchPanels[kind].label=label;

export function registerResearchPanel(kind: string, extension: PanelExtension) {
 if (!kind.trim() || researchPanels[kind]) throw new Error(`Research panel ${kind} already registered or invalid`);
 researchPanels[kind] = extension;
}
