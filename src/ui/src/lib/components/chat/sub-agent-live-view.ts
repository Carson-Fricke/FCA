import { getContext, setContext } from 'svelte';
import type { SubAgentNode } from '$lib/types';

/**
 * Live sub-agent state the chat surface holds for its registry cards.
 *
 * The surface provides this at its component root so the registry-selected
 * renderer (`SubAgentHandleCard`) upgrades a persisted `sub_agent_handle`
 * datum with the same liveness the workflow card has: the durable node's
 * current status, whether the agent owes a reply right now, and the
 * drill-in navigation into the nested conversation.
 */
export interface SubAgentLiveView {
	/** The live node record for one spawned sub-agent, when the tree knows it. */
	nodeFor: (subAgentId: string) => SubAgentNode | null;
	/** True while the agent owes a reply (its last scoped message is a prompt). */
	pendingFor: (subAgentId: string) => boolean;
	/** Drill into the sub-agent's own chat surface. */
	onEnterSubAgent: (subAgentId: string) => void;
}

/** Reads the current live view; the surface's props stay reactive through it. */
export type SubAgentLiveViewSource = () => SubAgentLiveView;

const SUB_AGENT_LIVE_VIEW_KEY = Symbol('sub-agent-live-view');

export function setSubAgentLiveView(source: SubAgentLiveViewSource): void {
	setContext(SUB_AGENT_LIVE_VIEW_KEY, source);
}

export function getSubAgentLiveView(): SubAgentLiveViewSource | null {
	return (
		getContext<SubAgentLiveViewSource | undefined>(SUB_AGENT_LIVE_VIEW_KEY) ?? null
	);
}
