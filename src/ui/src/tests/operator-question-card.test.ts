import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it } from 'vitest';
import ToolResultCard from '$lib/components/chat/ToolResultCard.svelte';
import type { ToolResult } from '$lib/types';

const request: ToolResult = {
	tool: 'request_operator_input',
	datumType: 'operator_question',
	callId: 'call-1',
	ok: true,
	summary: 'Approve the model?',
	structured: {
		subAgentId: 'sa-1',
		question: 'Approve the model?',
		reason: 'the design record is ambiguous'
	},
	text: 'Approve the model?',
	failure: null
};

describe('OperatorQuestionCard', () => {
	it('renders the request box carrying the message and reason', async () => {
		render(ToolResultCard, { props: { result: request, workspaceId: 'w1' } });
		await tick();

		expect(screen.getByTestId('operator-question-card')).toBeInTheDocument();
		expect(screen.getByTestId('operator-question-message')).toHaveTextContent(
			'Approve the model?'
		);
		expect(screen.getByTestId('operator-question-reason')).toHaveTextContent(
			'the design record is ambiguous'
		);
		expect(screen.queryByTestId('generic-tool-card')).toBeNull();
	});

	it('falls back to the tool result summary when no structured question is set', async () => {
		render(ToolResultCard, {
			props: {
				result: {
					...request,
					structured: { subAgentId: 'sa-1' }
				},
				workspaceId: 'w1'
			}
		});
		await tick();

		expect(screen.getByTestId('operator-question-message')).toHaveTextContent(
			'Approve the model?'
		);
		expect(screen.queryByTestId('operator-question-reason')).toBeNull();
	});
});
