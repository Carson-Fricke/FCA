import type { Component } from 'svelte';
import type { ToolResult } from '$lib/types';
import { genericHeader, knowledgeHeader, sdkHeader, searchHeader, listHeader, readHeader, sketchHeader, type ToolHeaderAdapter } from './headers';
import GenericToolCard from './GenericToolCard.svelte';
import BacktestToolCard from './BacktestToolCard.svelte';
import RunToolCard from './RunToolCard.svelte';
import JobToolCard from './JobToolCard.svelte';
import ArtifactsToolCard from './ArtifactsToolCard.svelte';
import OperatorQuestionCard from './OperatorQuestionCard.svelte';
import SubAgentHandleCard from './SubAgentHandleCard.svelte';
import WorkflowOutcomeCard from './WorkflowOutcomeCard.svelte';
import WorkflowExecutionCard from './WorkflowExecutionCard.svelte';
import StudyToolCard from './StudyToolCard.svelte';
import ReferenceToolCard from './ReferenceToolCard.svelte';
import SketchToolCard from './SketchToolCard.svelte';

export interface ToolResultCardProps {
	result: ToolResult;
	workspaceId: string;
 hideSummary?: boolean;
}

/** Renderers are selected only by the declared current datum type. */
interface ToolResultRegistration {
 component: Component<ToolResultCardProps>;
 header?: ToolHeaderAdapter;
}
function register(component: Component<ToolResultCardProps>, header?: ToolHeaderAdapter): ToolResultRegistration {
 return {component, header};
}
export const TOOL_RESULT_RENDERERS: Record<string, ToolResultRegistration> = {
	// Well-known datum types; unregistered ones fall back to the generic card.
	tool_result: register(GenericToolCard),
	workflow_execution: register(WorkflowExecutionCard),
	workflow_outcome: register(WorkflowOutcomeCard),
	sub_agent_handle: register(SubAgentHandleCard),
	sub_agent_status: register(GenericToolCard),
	operator_question: register(OperatorQuestionCard),
	run_study: register(StudyToolCard),
	author_study: register(StudyToolCard),
	update_study: register(StudyToolCard),
	validate_study: register(StudyToolCard),
	read_study_output: register(StudyToolCard, readHeader('study output', 'output_name')),
	search_knowledge: register(ReferenceToolCard, searchHeader),
	read_knowledge: register(ReferenceToolCard, knowledgeHeader),
	read_sdk_reference: register(ReferenceToolCard, sdkHeader),
	author_strategy_sketch: register(SketchToolCard),
	update_strategy_sketch: register(SketchToolCard),
	read_strategy_sketch: register(SketchToolCard, sketchHeader),
	// Harness tools explicitly declare these datum types.
	run_backtest: register(BacktestToolCard),
	get_job_result: register(RunToolCard),
	get_job_status: register(JobToolCard, readHeader('job status', 'job_id')),
	list_artifacts: register(ArtifactsToolCard, listHeader('artifacts', 'artifacts')),
	list_studies: register(GenericToolCard, listHeader('studies', 'programs')),
	list_strategies: register(GenericToolCard, listHeader('strategies', 'programs')),
	list_indicators: register(GenericToolCard, listHeader('indicators', 'programs')),
	list_strategy_sketches: register(GenericToolCard, listHeader('strategy sketches', 'items')),
	read_study: register(GenericToolCard, readHeader('study', 'program_id')),
	read_strategy: register(GenericToolCard, readHeader('strategy', 'program_id')),
	read_indicator: register(GenericToolCard, readHeader('indicator', 'program_id')),
	read_artifact: register(GenericToolCard, readHeader('artifact', 'name'))
};

/** Unknown valid datum types use the generic renderer. */
export function toolResultComponent(datumType: string): Component<ToolResultCardProps> {
 return TOOL_RESULT_RENDERERS[datumType]?.component ?? GenericToolCard;
}

/** Rich result types opt out of the shared collapsed-read default. */
const INLINE_RESULTS = new Set<Component<ToolResultCardProps>>([
	SketchToolCard, BacktestToolCard, WorkflowExecutionCard, WorkflowOutcomeCard,
	SubAgentHandleCard, OperatorQuestionCard, RunToolCard
]);

const OPERATION_PRESENTATION: Record<NonNullable<ToolResult['operation']>, 'result' | 'receipt'> = {
	read: 'receipt', write: 'receipt', execute: 'receipt', interact: 'result'
};

export function toolResultPresentation(component: Component<ToolResultCardProps>, result: ToolResult): 'result' | 'receipt' {
	if (!result.ok || result.pending || result.outcome) return 'receipt';
	if (component === StudyToolCard) return typeof result.structured?.job_id === 'string' ? 'result' : 'receipt';
	if (INLINE_RESULTS.has(component)) return 'result';
	return result.operation ? OPERATION_PRESENTATION[result.operation] : 'receipt';
}

/** Header adapters share the renderer registry; transcript callers know no tool names. */
export function toolResultHeader(result: ToolResult) {
 const adapter = TOOL_RESULT_RENDERERS[result.datumType]?.header ?? genericHeader;
 return adapter(result);
}
