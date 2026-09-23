import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import SubAgentHandleCard from '$lib/components/chat/tool-results/SubAgentHandleCard.svelte';
import type { ToolResult } from '$lib/types';

const spawnResult: ToolResult = {
	tool: 'spawn_sub_agent',
	callId: 'call-spawn-1',
	ok: true,
	summary: 'Sub-agent spawned; it runs asynchronously.',
	datumType: 'sub_agent_handle',
	structured: {
		sub_agent_id: 'sa-42',
		status: 'spawned',
		kind: 'one_shot',
		role: 'regime-research',
		task: 'Audit the SPX volatility regime'
	},
	text: 'Sub-agent spawned; it runs asynchronously.',
	failure: null
};

describe('SubAgentHandleCard', () => {
	it('renders the handle with role, task, and status from the datum', () => {
		render(SubAgentHandleCard, { props: { result: spawnResult, workspaceId: 'w1' } });

		const card = screen.getByTestId('sub-agent-card');
		expect(card).toHaveAttribute('data-sub-agent', 'sa-42');
		expect(card).toHaveAttribute('data-status', 'spawned');
		expect(screen.getByTestId('sub-agent-card-status')).toHaveTextContent('spawned');
		expect(screen.getByTestId('sub-agent-card-task')).toHaveTextContent(
			'Audit the SPX volatility regime'
		);
		expect(screen.getByText('regime-research')).toBeInTheDocument();
		expect(screen.getByText('one shot')).toBeInTheDocument();
		expect(screen.getByText('sa-42')).toBeInTheDocument();
	});

	it('falls back to the generic identity when the datum carries no handle', () => {
		render(SubAgentHandleCard, {
			props: {
				result: { ...spawnResult, structured: {} },
				workspaceId: 'w1'
			}
		});

		expect(screen.getByText('sub-agent')).toBeInTheDocument();
		expect(screen.queryByTestId('sub-agent-card-task')).toBeNull();
	});

	it('stays on the surface when clicked without a live view', async () => {
		render(SubAgentHandleCard, { props: { result: spawnResult, workspaceId: 'w1' } });

		await fireEvent.click(screen.getByTestId('sub-agent-card'));
		expect(screen.getByTestId('sub-agent-card')).toBeInTheDocument();
	});
});
