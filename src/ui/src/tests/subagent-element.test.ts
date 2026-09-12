import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import SubAgentElement from '$lib/components/chat/SubAgentElement.svelte';
import type { SubAgentTreeNode } from '$lib/subagent';
import type { ExecutionEvent, SubAgentNode, ToolResult } from '$lib/types';

const toolResult: ToolResult = {
	tool: 'run_backtest',
	callId: 'call-1',
	ok: true,
	summary: 'done',
	structured: { run_id: 'run-1' },
	text: '',
	failure: null
};

function event(
	eventType: ExecutionEvent['eventType'],
	payload: Record<string, unknown>,
	cursor: number
): ExecutionEvent {
	return {
		actorId: 'thesis-agent',
		actorScope: 'workflow',
		correlationId: `corr-${cursor}`,
		cursor,
		eventId: `evt-${cursor}`,
		eventType,
		executionId: 'exec-1',
		intentEpoch: 0,
		lateResult: false,
		occurredAt: '2026-09-08T17:14:01Z',
		payload,
		workspaceId: 'alpha-lab'
	};
}

function node(overrides: Partial<SubAgentNode>): SubAgentNode {
	return {
		subAgentId: 'sub-1',
		parentSubAgentId: null,
		phase: 'research',
		role: 'Researcher',
		status: 'running',
		context: { isolated: true, knowledge_cutoff: '2024-01-01', mcp: { enabled: true, tools: ['search'] } },
		acceptsInput: true,
		...overrides
	};
}

function recursiveTree(): SubAgentTreeNode {
	return {
		node: node({ subAgentId: 'sub-1', role: 'Researcher' }),
		events: [event('tool.result', { subAgentId: 'sub-1', toolResult }, 1)],
		children: [
			{
				node: node({
					subAgentId: 'sub-2',
					parentSubAgentId: 'sub-1',
					role: 'Coder',
					context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
					acceptsInput: false
				}),
				events: [],
				children: []
			}
		]
	};
}

describe('SubAgentElement', () => {
	it('renders recursively with scoped thinking and tool results', () => {
		const tree = recursiveTree();
		tree.events = [
			event('sub_agent.output', { subAgentId: 'sub-1', reasoning: 'checking the data' }, 1),
			event('tool.result', { subAgentId: 'sub-1', toolResult }, 2)
		];
		render(SubAgentElement, { props: { node: tree.node, events: tree.events, children: tree.children, workspaceId: 'alpha-lab' } });

		expect(screen.getAllByTestId('sub-agent-element')).toHaveLength(2);
		expect(screen.getByText('Researcher')).toBeInTheDocument();
		expect(screen.getByText('Coder')).toBeInTheDocument();
		expect(screen.getByTestId('tool-result-card')).toBeInTheDocument();
		expect(screen.getByTestId('sub-agent-sub-1-thinking')).toBeInTheDocument();
	});

	it('shows the context badges only when declared', () => {
		const tree = recursiveTree();
		render(SubAgentElement, { props: { node: tree.node, events: [], children: [] } });

		expect(screen.getByTestId('sub-agent-fresh-context')).toHaveTextContent('fresh context');
		expect(screen.getByTestId('sub-agent-cutoff')).toHaveTextContent('cutoff 2024-01-01');
		expect(screen.getByTestId('sub-agent-mcp')).toHaveTextContent('MCP: 1 tools');
	});

	it('renders a composer only for a node that accepts input', () => {
		const tree = recursiveTree();
		render(SubAgentElement, { props: { node: tree.node, events: [], children: tree.children } });

		// Root accepts input; the nested Coder does not, so exactly one composer.
		expect(screen.getAllByTestId('sub-agent-composer')).toHaveLength(1);
	});

	it('omits the composer entirely when the node does not accept input', () => {
		render(SubAgentElement, {
			props: { node: node({ acceptsInput: false }), events: [], children: [] }
		});
		expect(screen.queryByTestId('sub-agent-composer')).toBeNull();
	});

	it('enters a nested sub-agent when its header is clicked', async () => {
		const onEnter = vi.fn();
		const tree = recursiveTree();
		render(SubAgentElement, { props: { node: tree.node, events: [], children: tree.children, onEnter } });

		const enters = screen.getAllByTestId('sub-agent-enter');
		await fireEvent.click(enters[1]);
		expect(onEnter).toHaveBeenCalledWith(
			expect.objectContaining({ subAgentId: 'sub-2', role: 'Coder' })
		);
	});
});
