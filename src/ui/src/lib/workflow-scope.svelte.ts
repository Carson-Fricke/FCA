/** Navigation, expansion, durable node capabilities and operator submission only. */
import { listChatSubAgents, postNodeMessage } from './api';
import { TimelineController } from './timeline.svelte';
import { canonicalPhaseName } from './workflow-graph';
import { deriveSubAgentTree, isWorkflowNodeRole, subAgentNodeRef, type SubAgentTreeNode } from './subagent';
import { applyHashTarget, parseScope, phaseScopeId, phaseScopeTarget, scopeFromHash, scopeStorageKey, serializeScope, ScopeController, type ScopeSegment } from './scope.svelte';
import type { ChatMessage, SubAgentNode } from './types';
import { isTerminalWorkflow } from './workflow-live';
import { untrack } from 'svelte';

export interface AttachOptions {
	rootLabel?: string;
	scope?: string | null;
	hash?: string | null;
}

export class WorkflowScopeController {
	knownNodes = $state<SubAgentNode[]>([]);
	expanded = $state<Record<string, boolean>>({});
	busy = $state(false);
	error = $state('');
	readonly scope: ScopeController;
	readonly timeline: TimelineController;
	#workspaceId: string;
	#chatId: string | null = null;
	#generation = 0;
	#initialLoad: Promise<void> | null = null;
	#refreshingNodes = false;
	#nodeRefreshPending = false;
	#requests = new AbortController();
	#pendingPhase = $state<{ executionId: string; phase: string; readOnly: boolean; generation: number } | null>(null);

	constructor(workspaceId: string, hostId: string, timeline: TimelineController) {
		this.#workspaceId = workspaceId;
		this.timeline = timeline;
		this.scope = new ScopeController(scopeStorageKey(workspaceId, hostId));
	}

	tree = $derived.by(() => deriveSubAgentTree(this.timeline.events, this.knownNodes));
	#nodes = $derived.by(() => {
		const byId = new Map<string, SubAgentTreeNode>();
		const byPhase = new Map<string, SubAgentTreeNode[]>();
		const visit = (items: SubAgentTreeNode[]): void => {
			for (const item of items) {
				byId.set(item.node.subAgentId, item);
				if (item.node.workflowExecutionId && item.node.phase && item.node.phaseExecutor === true) {
					const key = phaseScopeId(item.node.workflowExecutionId, canonicalPhaseName(item.node.phase));
					const bucket = byPhase.get(key) ?? [];
					bucket.push(item);
					byPhase.set(key, bucket);
				}
				visit(item.children);
			}
		};
		visit(this.tree);
		return { byId, byPhase };
	});
	get activeSegment(): ScopeSegment | null { return this.scope.active; }
	get expansionKey(): string { return `thesis.subagent.expanded.${this.#workspaceId}.${this.#chatId ?? ''}`; }

	attachChat(chatId: string, options: AttachOptions = {}): void {
		this.detach();
		this.#requests = new AbortController();
		const generation = this.#generation;
		this.#chatId = chatId;
		this.knownNodes = [];
		this.error = '';
		this.expanded = this.readExpansion();
		const root: ScopeSegment = { kind: 'conversation', id: chatId, label: options.rootLabel ?? 'Conversation' };
		let segments = parseScope(options.scope);
		if (segments[0]?.kind === 'conversation' && segments[0].id !== root.id) segments = [];
		if (!segments.some((segment) => segment.kind === 'conversation')) segments = [root, ...segments];
		this.scope.reset(applyHashTarget(segments, scopeFromHash(options.hash)));
		this.resolvePhaseScope();
		this.#refreshingNodes = !!this.#chatId;
		this.#initialLoad = listChatSubAgents(this.#workspaceId, chatId, this.#requests.signal)
			.then((nodes) => {
				if (generation !== this.#generation) return;
				this.knownNodes = nodes;
				this.resolvePhaseScope();
				this.normalizeScope();
				this.resolvePendingPhase();
				this.loadActiveHistory();
			}).catch((error) => {
				if (generation === this.#generation) this.error = error instanceof Error ? error.message : 'Agent metadata could not be loaded';
			}).finally(() => this.finishNodeRefresh(generation));
	}

	detach(): void {
		this.#requests.abort();
		this.#generation += 1;
		this.#pendingPhase = null;
		for (const segment of this.scope.segments) {
			if (segment.kind === 'sub_agent') this.timeline.cancelHistory(segment.id);
		}
		this.#chatId = null;
		this.#initialLoad = null;
		this.#refreshingNodes = false;
		this.#nodeRefreshPending = false;
		this.busy = false;
	}

	refreshNodes(): void {
		if (!this.#chatId) return;
		if (this.#refreshingNodes) {
			this.#nodeRefreshPending = true;
			return;
		}
		const generation = this.#generation;
		this.#refreshingNodes = true;
		void listChatSubAgents(this.#workspaceId, this.#chatId, this.#requests.signal).then((nodes) => {
			if (generation !== this.#generation) return;
			this.error = '';
			const previousNodes = new Map(this.knownNodes.map((node) => [node.subAgentId, node]));
			this.knownNodes = nodes.map((node) => {
				const previous = previousNodes.get(node.subAgentId);
				const previousAt = Date.parse(previous?.updatedAt ?? previous?.createdAt ?? '');
				const incomingAt = Date.parse(node.updatedAt ?? node.createdAt ?? '');
				return previous && Number.isFinite(previousAt) && (!Number.isFinite(incomingAt) || incomingAt < previousAt) ? previous : node;
			});
			this.resolvePhaseScope();
			this.resolvePendingPhase();
		}).catch((error) => {
			if (generation === this.#generation) this.error = error instanceof Error ? error.message : 'Agent metadata could not be loaded';
		}).finally(() => this.finishNodeRefresh(generation));
	}

	private finishNodeRefresh(generation: number): void {
		if (generation !== this.#generation) return;
		this.#refreshingNodes = false;
		// Collapse in-flight triggers into one trailing read, including the initial listing.
		if (this.#nodeRefreshPending) {
			this.#nodeRefreshPending = false;
			this.refreshNodes();
		}
	}

	async ensureLoaded(): Promise<void> { if (this.#initialLoad) await this.#initialLoad; }

	async enterPhase(executionId: string, phase: string, readOnly = false): Promise<void> {
		const key = phaseScopeId(executionId, canonicalPhaseName(phase));
		this.#pendingPhase = null;
		readOnly ||= isTerminalWorkflow(this.timeline.workflows.get(executionId)?.execution);
		const matches = this.#nodes.byPhase.get(key) ?? [];
		const matchIds = new Set(matches.map((item) => item.node.subAgentId));
		const phaseAgents = matches.filter((item) => !item.node.parentSubAgentId || !matchIds.has(item.node.parentSubAgentId));
		if (phaseAgents.length > 1) {
			this.error = `Phase ${phase} has multiple agents in execution ${executionId}; open an explicit agent link.`;
			return;
		}
		this.error = '';
		if (phaseAgents[0]) {
			this.enterSubAgent(phaseAgents[0].node);
			if (readOnly) this.scope.updateActive({ readOnly: true });
		} else {
			this.navigateTo(0);
			this.scope.push({ kind: 'phase', id: phaseScopeId(executionId, phase), executionId, phase, label: `Phase ${phase}`, readOnly: true });
			this.#pendingPhase = { executionId, phase, readOnly, generation: this.#generation };
		}
	}

	/** Resolve only the still-selected click; events and metadata share the same node index. */
	resolvePendingPhase(): void {
		const pending = this.#pendingPhase;
		if (!pending) return;
		const active = this.scope.active;
		if (pending.generation !== this.#generation || active?.kind !== 'phase' ||
			active.id !== phaseScopeId(pending.executionId, pending.phase)) {
			untrack(() => { this.#pendingPhase = null; });
			return;
		}
		if (!this.#nodes.byPhase.get(phaseScopeId(pending.executionId, canonicalPhaseName(pending.phase)))?.length) return;
		untrack(() => { void this.enterPhase(pending.executionId, pending.phase, pending.readOnly); });
	}

	eventsForPhase(executionId: string, phase: string) {
		return this.timeline.eventsForExecution(executionId).filter((event) =>
			['workflow.phase', 'workflow.progress', 'workflow.phase_result'].includes(event.eventType) && canonicalPhaseName(String(event.payload['phase'])) === canonicalPhaseName(phase));
	}

	private resolvePhaseScope(): void {
		let workflowId: string | undefined;
		const segments = this.scope.segments.map((segment) => {
			if (segment.kind === 'workflow') workflowId = segment.id;
			if (segment.kind !== 'phase') return segment;
			let target = phaseScopeTarget(segment.id);
			if (!target) {
				const candidates = [...this.timeline.workflows.keys()];
				const historyComplete = !this.timeline.history.root || this.timeline.history.root.complete;
				const id = workflowId ?? (historyComplete && candidates.length === 1 ? candidates[0] : undefined);
				if (!id) {
					this.error = 'This persisted phase link does not identify an execution. Open the phase from its workflow card.';
					return { ...segment, readOnly: true };
				}
				target = { executionId: id, phase: segment.id };
			}
			return { ...segment, ...target, id: phaseScopeId(target.executionId, target.phase), label: `Phase ${target.phase}`, readOnly: true };
		});
		this.scope.reset(segments.filter((segment) => segment.kind !== 'workflow'));
	}

	enterSubAgent(node: SubAgentNode): void {
		if (isWorkflowNodeRole(node.role)) return;
		this.#pendingPhase = null;
		const chain = [...this.ancestorsOf(node), node];
		let keep = Math.max(0, this.scope.segments.findIndex((segment) => segment.kind === 'conversation'));
		let start = 0;
		for (let index = 0; index < chain.length - 1; index += 1) {
			const found = this.scope.segments.findIndex((segment) => segment.kind === 'sub_agent' && segment.id === chain[index].subAgentId);
			if (found !== -1) { keep = found; start = index + 1; }
		}
		if (keep < this.scope.segments.length - 1) this.navigateTo(keep);
		for (const item of chain.slice(start)) this.scope.push(this.segmentFor(item));
		this.loadActiveHistory();
	}

	private ancestorsOf(node: SubAgentNode): SubAgentNode[] {
		const chain: SubAgentNode[] = [];
		const seen = new Set([node.subAgentId]);
		let parentId = node.parentSubAgentId;
		while (parentId && !seen.has(parentId)) {
			seen.add(parentId);
			const parent = this.subAgentById(parentId);
			if (!parent) break;
			chain.unshift(parent);
			parentId = parent.parentSubAgentId;
		}
		return chain;
	}

	private segmentFor(node: SubAgentNode): ScopeSegment {
		return { kind: 'sub_agent', id: node.subAgentId, label: node.role || node.subAgentId, role: node.role,
			phase: node.phase ?? undefined, executionId: node.workflowExecutionId ?? undefined };
	}

	private normalizeScope(): void {
		const segments = this.scope.segments;
		if (!segments.length) return;
		const rebuilt = [segments[0]];
		for (const segment of segments.slice(1)) {
			if (segment.kind !== 'sub_agent') { rebuilt.push(segment); continue; }
			const node = this.subAgentById(segment.id);
			if (!node) break;
			for (const item of [...this.ancestorsOf(node), node]) {
				if (!rebuilt.some((candidate) => candidate.kind === 'sub_agent' && candidate.id === item.subAgentId)) {
					const persisted = segments.find((candidate) => candidate.kind === 'sub_agent' && candidate.id === item.subAgentId);
					rebuilt.push({ ...this.segmentFor(item), readOnly: persisted?.readOnly });
				}
			}
		}
		if (serializeScope(rebuilt) !== serializeScope(segments)) this.scope.reset(rebuilt);
	}

	back(): void { this.navigateTo(Math.max(0, this.scope.segments.length - 2)); }

	navigateTo(index: number): void {
		this.#pendingPhase = null;
		for (const segment of this.scope.segments.slice(index + 1)) {
			if (segment.kind === 'sub_agent') this.timeline.cancelHistory(segment.id);
		}
		this.scope.navigateTo(index);
		this.loadActiveHistory();
	}

	private loadActiveHistory(): void {
		const active = this.scope.active;
		if (active?.kind === 'sub_agent') void this.timeline.loadHistory(active.id);
	}

	toggleExpand(id: string): void {
		this.expanded = { ...this.expanded, [id]: !this.expanded[id] };
		try { localStorage.setItem(this.expansionKey, JSON.stringify(this.expanded)); } catch { /* Best effort. */ }
	}
	isExpanded(id: string): boolean { return this.expanded[id] === true; }
	childrenOf(id: string): SubAgentTreeNode[] { return this.#nodes.byId.get(id)?.children ?? []; }
	subAgentById(id: string): SubAgentNode | null { return this.#nodes.byId.get(id)?.node ?? null; }

	async submit(node: SubAgentNode, body: string): Promise<ChatMessage> {
		if (isWorkflowNodeRole(node.role) || !node.acceptsInput || !body.trim() || !this.#chatId) throw new Error('This sub-agent does not accept input');
		this.busy = true;
		const generation = this.#generation;
		const signal = this.#requests.signal;
		try {
			const message = await postNodeMessage(this.#workspaceId, this.#chatId, subAgentNodeRef(node.subAgentId), { body: body.trim() }, signal);
			signal.throwIfAborted();
			return message;
		} finally { if (generation === this.#generation) this.busy = false; }
	}

	private readExpansion(): Record<string, boolean> {
		try {
			const parsed = JSON.parse(localStorage.getItem(this.expansionKey) ?? '{}');
			return Object.fromEntries(Object.entries(parsed).filter(([, value]) => value === true)) as Record<string, boolean>;
		} catch { return {}; }
	}
}
