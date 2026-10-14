/** Per-grammar mapping policies. Extensions can supply their own policy in the registry. */
import type { Panel, Row } from './contracts';
export type FieldRole = 'dimension' | 'measure' | 'quantitative' | 'ordered';
export interface MappingPolicy {
	channels: Record<string, FieldRole>;
	coordinates?: (panel: Panel) => string[];
	validate?: (panel: Panel, rows: Row[]) => string | null;
}
const sequenceCoordinates = (panel: Panel) => [panel.encodings.x, ...(panel.encodings.series ? [panel.encodings.series] : [])];
export const builtinPolicies: Record<string, MappingPolicy> = {
	table: { channels: {} },
	heatmap: { channels: { x:'dimension', y:'dimension', value:'measure' }, coordinates:p=>[p.encodings.x,p.encodings.y] },
	scatter: { channels: { x:'quantitative', y:'quantitative' } },
	line: { channels: { x:'ordered', y:'measure' }, coordinates:sequenceCoordinates },
	comparison: { channels: { x:'dimension', y:'measure' }, coordinates:sequenceCoordinates },
	distribution: { channels:{}, coordinates:sequenceCoordinates }
};
