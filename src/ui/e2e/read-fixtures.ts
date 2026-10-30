/** Current read-contract producers for small scripted external HTTP fixtures. */
import { toTranscriptMessage } from '../src/lib/transcript';
import type { TimelineItemWire } from '../src/lib/generated';

export function transcriptItems(items: TimelineItemWire[]) {
 return items.map(item => item.kind === 'message' ? { ...item, payload: toTranscriptMessage(item.payload) } : item);
}

export function nodeSummaries<T extends { result?: unknown; children?: unknown; resultAvailable?: boolean }>(nodes: T[]) {
 return nodes.map(({ result, children, ...node }) => ({ ...node, resultAvailable: node.resultAvailable ?? result != null }));
}
