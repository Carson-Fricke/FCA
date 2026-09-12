import type { Component } from 'svelte';
import type { ToolResult } from '$lib/types';
import GenericToolCard from './GenericToolCard.svelte';
import BacktestToolCard from './BacktestToolCard.svelte';
import SweepToolCard from './SweepToolCard.svelte';
import RunToolCard from './RunToolCard.svelte';
import JobToolCard from './JobToolCard.svelte';
import ArtifactsToolCard from './ArtifactsToolCard.svelte';

export interface ToolResultCardProps {
	result: ToolResult;
	workspaceId: string;
}

export const TOOL_RESULT_RENDERERS: Record<string, Component<ToolResultCardProps>> = {
	run_backtest: BacktestToolCard,
	run_sweep: SweepToolCard,
	get_run: RunToolCard,
	get_backtest_status: JobToolCard,
	list_artifacts: ArtifactsToolCard
};

export function toolResultComponent(tool: string): Component<ToolResultCardProps> {
	return TOOL_RESULT_RENDERERS[tool] ?? GenericToolCard;
}
