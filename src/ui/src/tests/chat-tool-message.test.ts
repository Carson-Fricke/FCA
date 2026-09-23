import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
import type { ChatMessage, ToolResult, WorkflowExecutionRecord } from '$lib/types';

function message(overrides: Partial<ChatMessage>): ChatMessage {
	return {
		message_id: 'm0',
		chat_id: 'c1',
		workspace_id: 'w1',
		actor_id: 'operator',
		role: 'user',
		body: '',
		state: 'accepted',
		attachment_ids: [],
		created_at: '2026-01-01T00:00:00Z',
		updated_at: null,
		execution_id: null,
		intent_epoch: 0,
		...overrides
	};
}

const toolResult: ToolResult = {
	tool: 'run_backtest',
	callId: 'call-1',
	ok: true,
	summary: 'run_backtest completed',
	structured: { boundary: 'local', exit_code: 0 },
	text: 'ok',
	failure: null
};

describe('ChatTranscript tool-result placement', () => {
	it('renders a tool message inline at its position in the chain', async () => {
		const messages: ChatMessage[] = [
			message({ message_id: 'm1', role: 'user', body: 'run it' }),
			message({
				message_id: 'm2',
				role: 'tool',
				actor_id: 'thesis-agent',
				body: 'run_backtest completed',
				tool_result: toolResult
			}),
			message({
				message_id: 'm3',
				role: 'assistant',
				actor_id: 'thesis-agent',
				body: 'Done.'
			})
		];

		const { container } = render(ChatTranscript, {
			props: {
				messages,
				workspaceId: 'w1',
				onConfirmConsent: vi.fn(),
				onRejectConsent: vi.fn()
			}
		});
		await tick();

		const roles = Array.from(container.querySelectorAll('[data-role]')).map((el) =>
			el.getAttribute('data-role')
		);
		expect(roles).toEqual(['user', 'tool', 'assistant']);

		const card = screen.getByTestId('tool-result-card');
		expect(card).toHaveAttribute('data-tool', 'run_backtest');
	});
});

describe('ChatTranscript tool-result datum-type routing', () => {
	function renderTool(result: ToolResult) {
		return render(ChatTranscript, {
			props: {
				messages: [
					message({
						message_id: 'm2',
						role: 'tool',
						actor_id: 'thesis-agent',
						body: result.summary,
						tool_result: result
					})
				],
				workspaceId: 'w1',
				onConfirmConsent: vi.fn(),
				onRejectConsent: vi.fn()
			}
		});
	}

	it('routes a workflow launch datum to the workflow card instead of a tool card', async () => {
		renderTool({ ...toolResult, datumType: 'workflow_execution' });
		await tick();

		expect(screen.getByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.queryByTestId('generic-tool-card')).toBeNull();
		expect(screen.queryByTestId('backtest-tool-card')).toBeNull();
	});

	it('resolves a datum type that equals a tool name through the datum type path', async () => {
		renderTool({ ...toolResult, tool: 'not_a_real_tool', datumType: 'run_backtest' });
		await tick();

		expect(screen.getByTestId('backtest-tool-card')).toBeInTheDocument();
	});

	it('routes a sub-agent handle datum to the sub-agent card', async () => {
		renderTool({
			...toolResult,
			tool: 'spawn_sub_agent',
			datumType: 'sub_agent_handle',
			structured: {
				sub_agent_id: 'sa-7',
				status: 'spawned',
				kind: 'conversation',
				role: 'research',
				task: 'evaluate the sample'
			}
		});
		await tick();

		expect(screen.getByTestId('sub-agent-card')).toBeInTheDocument();
		expect(screen.queryByTestId('generic-tool-card')).toBeNull();
		expect(screen.queryByTestId('tool-result-card')).toBeNull();
	});

	it('falls back to the generic card for unknown datum types and tools', async () => {
		renderTool({ ...toolResult, tool: 'not_a_real_tool', datumType: 'not_a_real_datum_type' });
		await tick();

		expect(screen.getByTestId('generic-tool-card')).toBeInTheDocument();
	});
});

describe('ChatTranscript live workflow routing', () => {
	it('renders the registry workflow card with live phases and navigation', async () => {
		const liveExecution: WorkflowExecutionRecord = {
			execution_id: 'exec-42',
			chat_id: 'c1',
			trigger_message_id: 'm1',
			workflow_id: 'hypothesis-development',
			workflow_version: '1.0.0',
			requested_by: 'operator',
			state: 'running',
			phase: 'falsification_trials',
			intent_epoch: 0,
			steering_message_ids: [],
			created_at: '2026-01-01T00:00:00Z'
		};
		const workflowResult: ToolResult = {
			...toolResult,
			tool: 'start_workflow',
			datumType: 'workflow_execution',
			summary: 'Hypothesis development started.',
			structured: {
				executionId: 'exec-42',
				workflowId: 'hypothesis-development',
				phase: 'hypothesis_creation',
				state: 'running'
			}
		};
		const onEnterPhase = vi.fn();

		const { container } = render(ChatTranscript, {
			props: {
				messages: [
					message({
						message_id: 'm2',
						role: 'tool',
						actor_id: 'thesis-agent',
						body: workflowResult.summary,
						tool_result: workflowResult
					})
				],
				workspaceId: 'w1',
				execution: liveExecution,
				phases: [
					{ name: 'hypothesis_creation', status: 'done' },
					{ name: 'falsification_trials', status: 'active' }
				],
				attentionPhases: ['falsification_trials'],
				onEnterPhase
			}
		});
		await tick();

		// One route: the standalone workflow card renders through the registry
		// without the generic tool-result wrapper, exactly like the sub-agent
		// handle card.
		expect(screen.queryByTestId('tool-result-card')).toBeNull();
		const card = container.querySelector('[data-testid="workflow-card"]');
		expect(card).not.toBeNull();
		expect(card).toHaveAttribute('data-execution', 'exec-42');

		// Liveness survives the registry route: live attention, active-phase
		// pulse, and phase navigation all come from the transcript context.
		expect(screen.getByTestId('workflow-card-needs-input')).toHaveTextContent(
			'falsification_trials'
		);
		expect(screen.getByTestId('phase-liveness')).toBeInTheDocument();
		const steps = screen.getAllByTestId('phase-step');
		expect(steps.map((step) => step.getAttribute('data-phase'))).toEqual([
			'hypothesis_creation',
			'falsification_trials'
		]);
		await fireEvent.click(steps[0]);
		expect(onEnterPhase).toHaveBeenCalledWith('hypothesis_creation', {
			readOnly: true
		});
		await fireEvent.click(steps[1]);
		expect(onEnterPhase).toHaveBeenCalledWith('falsification_trials', {
			readOnly: false
		});
	});
});
