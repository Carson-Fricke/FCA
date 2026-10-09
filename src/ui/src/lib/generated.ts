// Generated from backend schema exports. Do not edit.
export type ActorScope = 'user' | 'assistant' | 'system' | 'tool' | 'workflow';
/**
 * The discriminator carried by one conversation item.
 */
export type TranscriptItemKind = 'message' | 'tool' | 'workflow' | 'sub_agent' | 'gate' | 'artifact';
export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';
export type MessageState = 'draft' | 'accepted' | 'streaming' | 'completed' | 'failed' | 'deleted';
export type ChatState = 'active' | 'archived' | 'deleted';
export type CompletionMode = 'complete_on_text' | 'explicit_completion';
/**
 * The two agent-backed phase kinds; deterministic work is never a sub-agent.
 */
export type SubAgentKind = 'conversation' | 'one_shot';
/**
 * Lifecycle status projected for one durable sub-agent.
 */
export type SubAgentStatus = 'pending' | 'running' | 'needs_input' | 'completed' | 'failed' | 'cancelled';
export type ExecutionEventType =
	| 'workflow.launched'
	| 'workflow.started'
	| 'workflow.phase'
	| 'workflow.progress'
	| 'workflow.phase_result'
	| 'workflow.steered'
	| 'workflow.stopped'
	| 'workflow.cancelled'
	| 'workflow.completed'
	| 'workflow.failed'
	| 'message.accepted'
	| 'tool.submitted'
	| 'tool.started'
	| 'tool.result'
	| 'tool.failed'
	| 'sub_agent.spawned'
	| 'sub_agent.progress'
	| 'sub_agent.output'
	| 'sub_agent.needs_input'
	| 'sub_agent.completed'
	| 'sub_agent.failed';
/**
 * Who may answer a node's question, as trusted actor kinds.
 */
export type OperatorActorKind = 'human_operator' | 'parent_agent' | 'agent';
export type RuntimeActivityWire = ExternalInput | ModelOutput | RuntimeTool | Delivery;
export type WorkflowExecutionState = 'queued' | 'running' | 'stopped' | 'cancelled' | 'completed' | 'failed';
export type TimelineItemWire = TimelineMessageWire | TimelineEventWire | TimelineExecutionWire;

export interface WireTypes {
	ActorRequest: ActorRequest;
	ActorScope: ActorScope;
	ChatMessageListWire: ChatMessageListWire;
	ChatMessageWire: ChatMessageWire;
	ChatState: ChatState;
	ChatSubAgentMessageRequest: ChatSubAgentMessageRequest;
	ChatSubAgentTreeWire: ChatSubAgentTreeWire;
	ChatThreadListWire: ChatThreadListWire;
	ChatThreadWire: ChatThreadWire;
	CompletionMode: CompletionMode;
	CreateAttachmentRequest: CreateAttachmentRequest;
	CreateChatRequest: CreateChatRequest;
	CreateInvestigationRequest: CreateInvestigationRequest;
	CreateMessageRequest: CreateMessageRequest;
	CreateMessageResponseWire: CreateMessageResponseWire;
	CreateStrategyRequest: CreateStrategyRequest;
	CreateWidgetRequest: CreateWidgetRequest;
	CreateWorkspaceRequest: CreateWorkspaceRequest;
	DatumWire: DatumWire;
	DeliveredDatumWire: DeliveredDatumWire;
	Delivery: Delivery;
	ErrorDetail: ErrorDetail;
	ErrorResponse: ErrorResponse;
	ExecutionEventType: ExecutionEventType;
	ExecutionEventWire: ExecutionEventWire;
	ExternalInput: ExternalInput;
	LaunchBacktestRequest: LaunchBacktestRequest;
	LaunchHarnessBacktestRequest: LaunchHarnessBacktestRequest;
	MessageContent: MessageContent;
	MessageRole: MessageRole;
	MessageState: MessageState;
	MessageStateRequest: MessageStateRequest;
	ModelOutput: ModelOutput;
	NodeInputReceiptWire: NodeInputReceiptWire;
	OperatorActorKind: OperatorActorKind;
	OperatorPolicyDecisionWire: OperatorPolicyDecisionWire;
	RecordLinks: RecordLinks;
	RunScreenerRequest: RunScreenerRequest;
	RuntimeActivityWire: RuntimeActivityWire;
	RuntimeTool: RuntimeTool;
	SaveScreenerRequest: SaveScreenerRequest;
	ScanAttachmentRequest: ScanAttachmentRequest;
	SessionMessageWire: SessionMessageWire;
	SteerWorkflowRequest: SteerWorkflowRequest;
	SteeringExecutionWire: SteeringExecutionWire;
	SteeringPercolationWire: SteeringPercolationWire;
	SubAgentKind: SubAgentKind;
	SubAgentNodeListWire: SubAgentNodeListWire;
	SubAgentNodeWire: SubAgentNodeWire;
	SubAgentStatus: SubAgentStatus;
	SubmitToolJobRequest: SubmitToolJobRequest;
	TimelineEventWire: TimelineEventWire;
	TimelineExecutionWire: TimelineExecutionWire;
	TimelineItemWire: TimelineItemWire;
	TimelineMessageWire: TimelineMessageWire;
	TimelinePageWire: TimelinePageWire;
	ToolCorrelation: ToolCorrelation;
	ToolRequest: ToolRequest;
	ToolResultRequest: ToolResultRequest;
	ToolResultWire: ToolResultWire;
	TranscriptItemKind: TranscriptItemKind;
	UploadAttachmentRequest: UploadAttachmentRequest;
	WorkflowDefinitionWire: WorkflowDefinitionWire;
	WorkflowExecutionState: WorkflowExecutionState;
	WorkflowExecutionWire: WorkflowExecutionWire;
}
export interface ActorRequest {
	actor_id: string;
}
export interface ChatMessageListWire {
	items: ChatMessageWire[];
}
/**
 * Complete serialized chat record, including persisted hash and timestamps.
 */
export interface ChatMessageWire {
	actor_id: string;
	attachment_ids: string[];
	body: string;
	chat_id: string;
	content_hash: string;
	conversation_id: string | null;
	created_at: string;
	created_by: string;
	data:
		| ((
				| (ExternalInput | ModelOutput | RuntimeTool | Delivery)
				| {
						[k: string]: unknown;
				  }
		  ) & {
				datums?: DatumWire[];
				sessionMessage?: SessionMessageWire;
				[k: string]: unknown;
		  })
		| null;
	execution_id: string | null;
	input_position: number;
	intent_epoch: number;
	investigation_ids: string[];
	kind: TranscriptItemKind;
	message_id: string;
	parent_conversation_id: string | null;
	reasoning: string | null;
	role: MessageRole;
	root_conversation_id: string | null;
	schema_version: number;
	state: MessageState;
	tool_result: ToolResultWire | DeliveredDatumWire | null;
	updated_at: string | null;
	workspace_id: string;
}
export interface ExternalInput {
	activityKind: 'external_input';
	subAgentId?: string | null;
	[k: string]: unknown;
}
export interface ModelOutput {
	activityKind: 'model_output';
	invocationId?: string | null;
	subAgentId?: string | null;
	[k: string]: unknown;
}
export interface RuntimeTool {
	activityKind: 'runtime_tool';
	subAgentId?: string | null;
	toolActivity?: 'started' | 'finished';
	toolCallId?: string | null;
	[k: string]: unknown;
}
export interface Delivery {
	activityKind: 'delivery';
	deliveryKey?: string | null;
	subAgentId?: string | null;
	[k: string]: unknown;
}
export interface DatumWire {
	datumType: string;
	failure: {
		[k: string]: unknown;
	} | null;
	ok: boolean;
	structured: {
		[k: string]: unknown;
	};
	summary: string | null;
	text: string;
}
/**
 * An immutable message in the caller-supplied session scope and order.
 */
export interface SessionMessageWire {
	content: MessageContent;
	direction: 'incoming' | 'outgoing' | 'instruction';
	message_id: string;
	position?: number;
}
/**
 * Uniform content for operator input, deliveries, prompts, and results.
 */
export interface MessageContent {
	correlation?: ToolCorrelation | null;
	requests?: ToolRequest[];
	result?: {
		[k: string]: unknown;
	} | null;
	source: string;
	text: string;
}
/**
 * The immutable, exact join to a request in a particular message.
 */
export interface ToolCorrelation {
	request_id: string;
	request_message_id: string;
	tool_name: string;
}
/**
 * One model-requested operation, without a provider wire representation.
 */
export interface ToolRequest {
	arguments?: string;
	id: string;
	name: string;
}
export interface ToolResultWire {
	callId: string | null;
	datumType?: string | null;
	failure: {
		[k: string]: unknown;
	} | null;
	ok: boolean;
	structured: {
		[k: string]: unknown;
	};
	summary: string | null;
	text: string;
	tool: string | null;
}
/**
 * Persisted deliveries carried datum fields plus routing metadata.
 *
 * These are not model-requested calls, so they have no call ID or tool name.
 * Keep this shape readable without weakening direct tool-result validation.
 */
export interface DeliveredDatumWire {
	datumType: string;
	failure: {
		[k: string]: unknown;
	} | null;
	ok: boolean;
	structured: {
		[k: string]: unknown;
	};
	summary: string | null;
	text: string;
	[k: string]: unknown;
}
/**
 * One operator input message addressed to a chat-scoped sub-agent.
 *
 * The answering actor is never accepted here: it is derived from the
 * authenticated session by the route.
 */
export interface ChatSubAgentMessageRequest {
	body: string;
}
/**
 * Canonical tree response used by the projection and HTTP schema export.
 */
export interface ChatSubAgentTreeWire {
	chatId: string;
	items: SubAgentNodeWire[];
	roots: SubAgentNodeWire[];
}
/**
 * One chat-scoped sub-agent node and its direct children.
 */
export interface SubAgentNodeWire {
	acceptsInput: boolean;
	allowedTools: string[];
	chatId: string;
	children?: SubAgentNodeWire[];
	completionContractRef: string | null;
	completionMode: CompletionMode;
	createdAt: string | null;
	delegatedBudget: number;
	inputContractRef: string | null;
	kind: SubAgentKind;
	parentSubAgentId: string | null;
	phase: string | null;
	phaseAttemptId: string | null;
	phaseExecutor: boolean;
	result: {
		[k: string]: unknown;
	} | null;
	resultRef: string | null;
	returnsToSubAgentId: string | null;
	role: string;
	specVersion: string;
	status: SubAgentStatus;
	subAgentId: string;
	updatedAt: string | null;
	workflowExecutionId: string | null;
}
export interface ChatThreadListWire {
	items: ChatThreadWire[];
}
/**
 * Complete serialized thread with source-derived links and provenance.
 */
export interface ChatThreadWire {
	chat_id: string;
	content_hash: string;
	conversation_id: string | null;
	created_at: string;
	created_by: string;
	input_consumed_positions: {
		[k: string]: number;
	};
	links: RecordLinks;
	owner_actor_id: string;
	parent_conversation_id: string | null;
	root_conversation_id: string | null;
	schema_version: number;
	state: ChatState;
	title: string;
	updated_at: string | null;
	workspace_id: string;
}
/**
 * Explicit links shared by chat, investigation, and execution records.
 */
export interface RecordLinks {
	chat_ids?: string[];
	experiment_ids?: string[];
	investigation_ids?: string[];
	job_ids?: string[];
	strategy_ids?: string[];
	workflow_execution_ids?: string[];
	[k: string]: unknown;
}
export interface CreateAttachmentRequest {
	actor_id: string;
	filename: string;
	media_type: string;
	message_id: string;
	size_bytes: number;
}
export interface CreateChatRequest {
	experiment_ids?: string[];
	investigation_ids?: string[];
	owner_actor_id: string;
	strategy_ids?: string[];
	title: string;
}
export interface CreateInvestigationRequest {
	chat_ids?: string[];
	experiment_ids?: string[];
	objective: string;
	owner_actor_id: string;
	strategy_ids?: string[];
	title: string;
}
export interface CreateMessageRequest {
	actor_id: string;
	attachment_ids?: string[];
	body: string;
	execution_id?: string | null;
	intent_epoch?: number;
	investigation_ids?: string[];
	role?: 'user' | 'assistant' | 'system' | 'tool';
	steering_intent?: string | null;
	targets_execution_id?: string | null;
}
export interface CreateMessageResponseWire {
	actor_id: string;
	attachment_ids: string[];
	body: string;
	chat_id: string;
	content_hash: string;
	conversation_id: string | null;
	created_at: string;
	created_by: string;
	data:
		| ((
				| (ExternalInput | ModelOutput | RuntimeTool | Delivery)
				| {
						[k: string]: unknown;
				  }
		  ) & {
				datums?: DatumWire[];
				sessionMessage?: SessionMessageWire;
				[k: string]: unknown;
		  })
		| null;
	execution_id: string | null;
	input_position: number;
	intent_epoch: number;
	investigation_ids: string[];
	kind: TranscriptItemKind;
	message_id: string;
	parent_conversation_id: string | null;
	reasoning: string | null;
	role: MessageRole;
	root_conversation_id: string | null;
	schema_version: number;
	state: MessageState;
	steering?: SteeringPercolationWire | null;
	tool_result: ToolResultWire | DeliveredDatumWire | null;
	updated_at: string | null;
	workspace_id: string;
}
/**
 * The steering acknowledgement attached to an accepted chat message.
 */
export interface SteeringPercolationWire {
	intent: string | null;
	percolatedTo: SteeringExecutionWire[];
	targetsExecutionId: string | null;
}
export interface SteeringExecutionWire {
	executionId: string;
	intentEpoch: number;
}
/**
 * Request boundary for a new strategy registry entry.
 */
export interface CreateStrategyRequest {
	description?: string;
	name: string;
	status?: string;
	symbol: string;
}
/**
 * Request boundary for widget creation; shapes follow WidgetDefinition.
 */
export interface CreateWidgetRequest {
	data?: {
		[k: string]: unknown;
	};
	pinned?: boolean;
	title: string;
	type: string;
}
/**
 * Request boundary for workspace creation.
 */
export interface CreateWorkspaceRequest {
	description?: string;
	initials?: string | null;
	name: string;
}
export interface ErrorDetail {
	code: string;
	details?: unknown;
	message: string;
}
export interface ErrorResponse {
	error: ErrorDetail;
}
/**
 * Execution event fields with the aliases used by ExecutionEvent.as_dict.
 */
export interface ExecutionEventWire {
	actorId: string;
	actorScope: ActorScope | string;
	correlationId: string;
	cursor: number;
	eventId: string;
	eventType: ExecutionEventType | string;
	executionId: string;
	intentEpoch: number;
	lateResult: boolean;
	occurredAt: string;
	payload: {
		[k: string]: unknown;
	};
	workspaceId: string;
}
/**
 * Request boundary mirroring the harness activation request contract.
 */
export interface LaunchBacktestRequest {
	bundle_ref: string;
	config_path: string;
	environment?: 'paper' | 'shadow' | 'live';
	name?: string | null;
	strategy_id?: string | null;
}
/**
 * The canonical launch body accepted by ``POST /v1/backtests/jobs``.
 */
export interface LaunchHarnessBacktestRequest {
	config_path: string;
	workspace?: string | null;
}
export interface MessageStateRequest {
	state: MessageState;
}
/**
 * Node-answer HTTP receipt: durable message plus admission provenance.
 */
export interface NodeInputReceiptWire {
	actor_id: string;
	attachment_ids: string[];
	body: string;
	chat_id: string;
	content_hash: string;
	conversation_id: string | null;
	created_at: string;
	created_by: string;
	data:
		| ((
				| (ExternalInput | ModelOutput | RuntimeTool | Delivery)
				| {
						[k: string]: unknown;
				  }
		  ) & {
				datums?: DatumWire[];
				sessionMessage?: SessionMessageWire;
				[k: string]: unknown;
		  })
		| null;
	execution_id: string | null;
	input_position: number;
	intent_epoch: number;
	investigation_ids: string[];
	kind: TranscriptItemKind;
	message_id: string;
	operatorPolicy: OperatorPolicyDecisionWire;
	parent_conversation_id: string | null;
	reasoning: string | null;
	role: MessageRole;
	root_conversation_id: string | null;
	schema_version: number;
	state: MessageState;
	subAgentId: string;
	tool_result: ToolResultWire | DeliveredDatumWire | null;
	updated_at: string | null;
	workspace_id: string;
}
/**
 * Flattened provenance returned by OperatorPolicyDecision.as_dict().
 */
export interface OperatorPolicyDecisionWire {
	actorId: string;
	actorKind: OperatorActorKind;
	allowed: boolean;
	allowedActorKinds: OperatorActorKind[];
	nodeId: string | null;
	policy: 'human_only' | 'custom';
	role: string;
}
/**
 * Request boundary for one screening run of a saved definition.
 */
export interface RunScreenerRequest {
	as_of: string;
	definition_id: string;
}
/**
 * Request boundary for one authored screener definition.
 */
export interface SaveScreenerRequest {
	/**
	 * @minItems 1
	 */
	bar_intervals: [string, ...string[]];
	lookback_days: number;
	name: string;
	parameters?: {
		[k: string]: unknown;
	} | null;
	rank_metric: string;
	source: string;
	universe_revision?: string | null;
}
export interface ScanAttachmentRequest {
	available: boolean;
	error_message?: string | null;
}
export interface SteerWorkflowRequest {
	actor_id: string;
	body: string;
	message_id?: string | null;
}
export interface SubAgentNodeListWire {
	items: SubAgentNodeWire[];
}
export interface SubmitToolJobRequest {
	actor_id: string;
	experiment_ids?: string[];
	investigation_id?: string | null;
	job_id?: string | null;
	strategy_ids?: string[];
	tool_name: string;
}
export interface TimelineEventWire {
	chatId: string;
	executionId?: string | null;
	id: string;
	kind: 'event';
	payload: ExecutionEventWire;
	seq: number;
	threadId?: string | null;
	ts: string;
	workspaceId: string;
}
export interface TimelineExecutionWire {
	chatId: string;
	executionId?: string | null;
	id: string;
	kind: 'execution';
	payload: WorkflowExecutionWire;
	seq: number;
	threadId?: string | null;
	ts: string;
	workspaceId: string;
}
/**
 * Complete serialized workflow execution record.
 */
export interface WorkflowExecutionWire {
	chat_id: string;
	content_hash: string;
	conversation_id: string | null;
	created_at: string;
	created_by: string;
	execution_id: string;
	experiment_ids: string[];
	finished_at: string | null;
	intent_epoch: number;
	investigation_id: string | null;
	job_ids: string[];
	parent_conversation_id: string | null;
	phase: string | null;
	requested_by: string;
	root_conversation_id: string | null;
	schema_version: number;
	state: WorkflowExecutionState;
	steering_message_ids: string[];
	strategy_ids: string[];
	trigger_message_id: string;
	updated_at: string | null;
	workflow_id: string;
	workflow_version: string;
	workspace_id: string;
}
export interface TimelineMessageWire {
	chatId: string;
	executionId?: string | null;
	id: string;
	kind: 'message';
	payload: ChatMessageWire;
	seq: number;
	threadId?: string | null;
	ts: string;
	workspaceId: string;
}
export interface TimelinePageWire {
	after: number;
	items: TimelineItemWire[];
	nextBefore: number | null;
}
export interface ToolResultRequest {
	actor_id?: string | null;
	error_message?: string | null;
	intent_epoch?: number | null;
	result?: {
		[k: string]: unknown;
	} | null;
	success?: boolean;
}
export interface UploadAttachmentRequest {
	content_uri: string;
	sha256: string;
}
export interface WorkflowDefinitionWire {
	phases: string[];
	terminal_phases: string[];
	title: string;
	transitions: {
		[k: string]: string[];
	};
	version: string;
	workflow_id: string;
}
