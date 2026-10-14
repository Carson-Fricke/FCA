import type { ToolResult } from '$lib/types';
import { isRecord, readString } from './safe';

export interface ToolRowHeader {
	icon: 'book' | 'search' | 'list' | 'dot';
	action: string;
	subject?: string;
	count?: string;
}
export type ToolHeaderAdapter = (result: ToolResult) => ToolRowHeader;

function fields(result: ToolResult) {
	return {
		data: isRecord(result.structured) ? result.structured : {},
		args: isRecord(result.arguments) ? result.arguments : {}
	};
}
function completed(result: ToolResult): boolean {
	return result.ok && !result.pending && !result.outcome;
}
function countLabel(count: number, singular: string, plural: string): string {
	return `${count} ${count === 1 ? singular : plural}`;
}

/** Unknown tools remain readable without inventing counts or reading payload prose. */
export const genericHeader: ToolHeaderAdapter = (result) => {
	const { data, args } = fields(result);
	const subject = readString(data, 'title') ?? readString(data, 'topic') ?? readString(args, 'topic');
	return {
		icon: result.operation === 'read' ? 'book' : 'dot',
		action: result.operation === 'read' && subject ? 'Read' :
			result.summary || (result.tool ?? result.datumType ?? 'Tool result').replaceAll('_', ' '),
		subject: result.operation === 'read' ? subject ?? undefined : undefined
	};
};

export const knowledgeHeader: ToolHeaderAdapter = (result) => {
	const { data } = fields(result);
	return { icon: 'book', action: 'Read', subject: readString(data, 'title') ?? 'knowledge reference',
		count: completed(result) && typeof data.content === 'string' ? '1 source' : undefined };
};

export const sdkHeader: ToolHeaderAdapter = (result) => {
	const { data, args } = fields(result);
	return { icon: 'book', action: 'Read SDK', subject: readString(data, 'topic') ?? readString(args, 'topic') ?? undefined };
};

export const sketchHeader: ToolHeaderAdapter = (result) => {
	const { data, args } = fields(result);
	const sketch = isRecord(data.sketch) ? data.sketch : {};
	return { icon: 'book', action: 'Read strategy sketch',
		subject: readString(sketch, 'title') ?? readString(args, 'sketch_id') ?? undefined };
};

export const searchHeader: ToolHeaderAdapter = (result) => {
	const { data, args } = fields(result);
	return { icon: 'search', action: 'Searched', subject: readString(args, 'query') ?? undefined,
		count: completed(result) && Array.isArray(data.results)
			? countLabel(data.results.length, 'match returned', 'matches returned') : undefined };
};

/** Counts describe only the returned collection, never an inferred catalog total. */
export function listHeader(subject: string, collection: string): ToolHeaderAdapter {
	return (result) => {
		const { data } = fields(result);
		const rows = data[collection];
		return { icon: 'list', action: `Listed ${subject}`,
			count: completed(result) && Array.isArray(rows) ? `${rows.length} returned` : undefined };
	};
}

export function readHeader(subject: string, id: string): ToolHeaderAdapter {
	return (result) => {
		const { data, args } = fields(result);
		return { icon: 'book', action: `Read ${subject}`,
			subject: readString(data, 'title') ?? readString(data, id) ?? readString(args, id) ?? readString(data, 'programId') ?? undefined };
	};
}
