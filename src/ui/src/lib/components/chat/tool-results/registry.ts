import type { Component } from 'svelte';
import type { ToolResult } from '$lib/types';
import GenericToolCard from './GenericToolCard.svelte';
import BacktestToolCard from './BacktestToolCard.svelte';
import SweepToolCard from './SweepToolCard.svelte';
import RunToolCard from './RunToolCard.svelte';
import JobToolCard from './JobToolCard.svelte';
import ArtifactsToolCard from './ArtifactsToolCard.svelte';
import OperatorQuestionCard from './OperatorQuestionCard.svelte';
import WorkflowOutcomeCard from './WorkflowOutcomeCard.svelte';
import WorkflowExecutionCard from './WorkflowExecutionCard.svelte';

export interface ToolResultCardProps {
	result: ToolResult;
	workspaceId: string;
}

/**
 * Renderer registry keyed by datum type and by tool name.
 *
 * Datum types are the primary key: every tool result declares its canonical
 * `datumType`, so dedicated cards can be registered per type later without
 * touching call sites. Harness tools keep their own tool name as the implicit
 * datum type, so the tool-name keys remain valid for results that carry no
 * `datumType` (backward compatibility).
 */
export const TOOL_RESULT_RENDERERS: Record<string, Component<ToolResultCardProps>> = {
	// Well-known datum types; unregistered ones fall back to the generic card.
	tool_result: GenericToolCard,
	workflow_execution: WorkflowExecutionCard,
	workflow_outcome: WorkflowOutcomeCard,
	sub_agent_handle: GenericToolCard,
	sub_agent_status: GenericToolCard,
	operator_question: OperatorQuestionCard,
	// Harness tool names, doubling as their implicit datum types.
	run_backtest: BacktestToolCard,
	run_sweep: SweepToolCard,
	get_run: RunToolCard,
	get_backtest_status: JobToolCard,
	list_artifacts: ArtifactsToolCard
};

/**
 * Resolve the renderer for one tool result: the payload's `datumType` first,
 * then the tool name for backward compatibility, then the generic card.
 */
export function toolResultComponent(
	tool: string,
	datumType?: string | null
): Component<ToolResultCardProps> {
	if (datumType) {
		const byDatumType = TOOL_RESULT_RENDERERS[datumType];
		if (byDatumType) return byDatumType;
	}
	return TOOL_RESULT_RENDERERS[tool] ?? GenericToolCard;
}
