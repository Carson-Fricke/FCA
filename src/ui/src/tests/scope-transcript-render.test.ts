import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it } from 'vitest';
import ScopeTranscript from '$lib/components/chat/ScopeTranscript.svelte';
import type { ScopeEntry } from '$lib/transcript';

/**
 * A sub-agent scope is never blank: it renders the initial context, the task
 * the agent was given, and the summary/output it produced.
 */
describe('ScopeTranscript sub-agent content', () => {
	const entries: ScopeEntry[] = [
		{
			kind: 'spawn',
			id: 's1',
			at: '2026-09-08T17:14:01Z',
			order: 1,
			spawn: {
				role: 'Thesis research ideation orchestrator',
				phase: 'ideation',
				isolated: true,
				knowledgeCutoff: null,
				mcpTools: []
			}
		},
		{
			kind: 'thinking',
			id: 't1',
			at: '2026-09-08T17:14:02Z',
			order: 2,
			text: 'Draft a falsifiable hypothesis from the operator idea.'
		},
		{
			kind: 'assistant',
			id: 'a1',
			at: '2026-09-08T17:14:03Z',
			order: 3,
			text: 'Hypothesis: momentum underreacts after earnings.',
			actorId: 'thesis-agent',
			needsInput: false
		}
	];

	it('shows the initial context, the prompt/task, and the summary', async () => {
		render(ScopeTranscript, { props: { entries, workspaceId: 'w1' } });
		await tick();

		expect(screen.getByTestId('scope-spawn')).toHaveTextContent('ideation');
		expect(
			screen.getByText('Draft a falsifiable hypothesis from the operator idea.')
		).toBeInTheDocument();
		expect(
			screen.getByText(/Hypothesis: momentum underreacts after earnings\./)
		).toBeInTheDocument();
	});

	it('renders a needs-input question prominently', async () => {
		render(ScopeTranscript, {
			props: {
				entries: [
					{
						kind: 'assistant',
						id: 'q1',
						at: '2026-09-08T17:14:04Z',
						order: 1,
						text: 'State the disagreement/edge and I will retry.',
						actorId: 'thesis-agent',
						needsInput: true
					}
				],
				workspaceId: 'w1'
			}
		});
		await tick();

		expect(screen.getByTestId('scope-needs-input')).toHaveTextContent('needs input');
		expect(screen.getByText('question')).toBeInTheDocument();
	});

	it('shows a typing indicator while the agent is working', async () => {
		render(ScopeTranscript, { props: { entries, workspaceId: 'w1', pending: true } });
		await tick();

		expect(screen.getByTestId('scope-pending')).toBeInTheDocument();
	});

	it('hides the typing indicator when the agent is idle', async () => {
		render(ScopeTranscript, { props: { entries, workspaceId: 'w1', pending: false } });
		await tick();

		expect(screen.queryByTestId('scope-pending')).toBeNull();
	});

	it('renders a falsification report card entry', async () => {
		const falsificationEntry: ScopeEntry = {
			kind: 'falsification',
			id: 'f1',
			at: '2026-09-08T17:14:04Z',
			order: 4,
			data: {
				type: 'falsification_report',
				content: 'Ran 3 falsification trial(s).',
				passedCount: 3,
				falsifiedCount: 0,
				totalCount: 3,
				survived: true,
				trials: [
					{
						testId: 'T01',
						falsifierId: 'f1',
						observable: 'alpha',
						condition: 'gte',
						threshold: 0,
						observed: -0.05,
						falsified: false
					}
				]
			}
		};

		render(ScopeTranscript, {
			props: { entries: [falsificationEntry], workspaceId: 'w1' }
		});
		await tick();

		expect(screen.getByTestId('falsification-report-card')).toBeInTheDocument();
		expect(screen.getByTestId('falsification-badge')).toHaveTextContent('3/3 Trials Survived');
	});
});
