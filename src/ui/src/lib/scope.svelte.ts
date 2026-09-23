/**
 * In-pane scope navigation: conversation → workflow → sub-agent → …
 *
 * The scope stack is view state. It is serialized into the `?scope=` query
 * parameter as a compact `kind:id` path and mirrored into localStorage so a
 * reload restores the same in-pane location. Deep links may also carry
 * `#sub_agent={id}`, which is appended as the active sub-agent segment.
 *
 * Pure `serializeScope`/`parseScope` helpers are exported separately so the
 * path grammar is unit-testable without a component or the DOM.
 */

export type ScopeSegmentKind = 'conversation' | 'workflow' | 'sub_agent' | 'phase';

export interface ScopeSegment {
	kind: ScopeSegmentKind;
	id: string;
	label: string;
	role?: string;
	phase?: string;
	/** A finished phase's orchestrator is entered read-only. */
	readOnly?: boolean;
}

const SCOPE_KINDS: ScopeSegmentKind[] = [
	'conversation',
	'workflow',
	'sub_agent',
	'phase'
];

function isScopeKind(value: string): value is ScopeSegmentKind {
	return (SCOPE_KINDS as string[]).includes(value);
}

/** A human label for a segment reconstructed from the URL alone. */
export function defaultScopeLabel(kind: ScopeSegmentKind, id: string): string {
	if (kind === 'conversation') return 'Conversation';
	if (kind === 'workflow') return 'Workflow';
	if (kind === 'phase') return `Phase ${id}`;
	return id;
}

/** Serialize a scope path to `kind:id/kind:id`. */
export function serializeScope(segments: ScopeSegment[]): string {
	return segments
		.filter((segment) => segment.id)
		.map((segment) => `${segment.kind}:${encodeURIComponent(segment.id)}`)
		.join('/');
}

/** Parse a `?scope=` value back into segments, dropping malformed entries. */
export function parseScope(value: string | null | undefined): ScopeSegment[] {
	if (!value) return [];
	const segments: ScopeSegment[] = [];
	for (const part of value.split('/')) {
		if (!part) continue;
		const separator = part.indexOf(':');
		const kind = separator === -1 ? part : part.slice(0, separator);
		const id = decodeURIComponent(separator === -1 ? '' : part.slice(separator + 1));
		if (!id || !isScopeKind(kind)) continue;
		segments.push({ kind, id, label: defaultScopeLabel(kind, id) });
	}
	return segments;
}

/** Parse a `#sub_agent={id}` deep link into a sub-agent segment. */
export function scopeFromHash(hash: string | null | undefined): ScopeSegment | null {
	if (!hash) return null;
	const cleaned = hash.startsWith('#') ? hash.slice(1) : hash;
	const params = new URLSearchParams(cleaned);
	const id = params.get('sub_agent');
	if (!id) return null;
	return { kind: 'sub_agent', id, label: defaultScopeLabel('sub_agent', id) };
}

/**
 * Merge a hash deep-link target into a parsed scope path. The target is
 * appended when absent; an existing segment is re-selected instead of
 * duplicated.
 */
export function applyHashTarget(
	segments: ScopeSegment[],
	target: ScopeSegment | null
): ScopeSegment[] {
	if (!target) return segments;
	const existing = segments.findIndex(
		(segment) => segment.kind === target.kind && segment.id === target.id
	);
	if (existing !== -1) return segments.slice(0, existing + 1);
	return [...segments, target];
}

function storage(): Storage | null {
	try {
		return typeof localStorage !== 'undefined' ? localStorage : null;
	} catch {
		return null;
	}
}

/**
 * Observable scope path shared by the conversation pane and the full-page
 * workflow route. `segments[0]` is the conversation root; the last segment is
 * the active node.
 */
export class ScopeController {
	segments = $state<ScopeSegment[]>([]);
	private readonly storageKey: string;

	constructor(storageKey: string, initial: ScopeSegment[] = []) {
		this.storageKey = storageKey;
		this.segments = this.read() ?? initial;
	}

	get active(): ScopeSegment | null {
		return this.segments.at(-1) ?? null;
	}

	get canGoBack(): boolean {
		return this.segments.length > 1;
	}

	/** Replace the entire path (typically the conversation root). */
	reset(segments: ScopeSegment[]): void {
		this.segments = segments;
		this.persist();
	}

	/**
	 * Enter a node. Re-entering an ancestor switches to it (truncating the
	 * path) so siblings switch without leaving and without duplicating.
	 */
	push(segment: ScopeSegment): void {
		const existing = this.segments.findIndex(
			(candidate) => candidate.kind === segment.kind && candidate.id === segment.id
		);
		if (existing !== -1) {
			// Re-entering an ancestor truncates to it (refreshing the label).
			const kept = this.segments.slice(0, existing + 1);
			kept[kept.length - 1] = { ...kept[kept.length - 1], ...segment };
			this.segments = kept;
		} else {
			this.segments = [...this.segments, segment];
		}
		this.persist();
	}

	/** Return one level; returns the popped segment. */
	back(): ScopeSegment | null {
		if (!this.canGoBack) return null;
		const popped = this.segments[this.segments.length - 1];
		this.segments = this.segments.slice(0, -1);
		this.persist();
		return popped;
	}

	/** Navigate to a breadcrumb index, dropping everything after it. */
	navigateTo(index: number): void {
		if (index < 0 || index >= this.segments.length) return;
		this.segments = this.segments.slice(0, index + 1);
		this.persist();
	}

	/** Refresh the active segment's label/role without changing the path. */
	updateActive(patch: Partial<ScopeSegment>): void {
		if (!this.segments.length) return;
		const next = [...this.segments];
		next[next.length - 1] = { ...next[next.length - 1], ...patch };
		this.segments = next;
		this.persist();
	}

	/** The `scope` query value for the current path. */
	toQuery(): string {
		return serializeScope(this.segments);
	}

	private persist(): void {
		const store = storage();
		if (!store) return;
		try {
			store.setItem(this.storageKey, serializeScope(this.segments));
		} catch {
			// Quota or privacy mode: scope persistence is best-effort.
		}
	}

	private read(): ScopeSegment[] | null {
		const store = storage();
		if (!store) return null;
		try {
			const raw = store.getItem(this.storageKey);
			if (raw === null) return null;
			const parsed = parseScope(raw);
			return parsed.length ? parsed : null;
		} catch {
			return null;
		}
	}
}

/** The localStorage key for one host's scope path. */
export function scopeStorageKey(workspaceId: string, hostId: string): string {
	return `thesis.scope.${workspaceId}.${hostId}`;
}
