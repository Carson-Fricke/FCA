import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import GateCard from '$lib/components/chat/GateCard.svelte';
import type { WorkflowGate } from '$lib/types';

const gate: WorkflowGate = {
	gateId: 'gate-1',
	executionId: 'exec-1',
	phase: 'activation',
	status: 'blocked',
	reason: 'Capital bound 250000 exceeds the policy bound of 100000'
};

describe('GateCard', () => {
	it('renders the gate reason verbatim', () => {
		render(GateCard, { props: { gate } });
		expect(screen.getByTestId('gate-reason')).toHaveTextContent(
			'Capital bound 250000 exceeds the policy bound of 100000'
		);
	});

	it('resolves by approving the plan', async () => {
		const onResolve = vi.fn();
		render(GateCard, { props: { gate, onResolve } });
		await fireEvent.click(screen.getByTestId('gate-approve'));
		expect(onResolve).toHaveBeenCalledWith({ kind: 'approve_plan' });
	});

	it('resolves with a disagreement note', async () => {
		const onResolve = vi.fn();
		render(GateCard, { props: { gate, onResolve } });
		await fireEvent.input(screen.getByTestId('gate-disagreement-note'), {
			target: { value: 'The bound is justified by the mandate' }
		});
		await fireEvent.click(screen.getByTestId('gate-disagreement-submit'));
		expect(onResolve).toHaveBeenCalledWith({
			kind: 'disagreement',
			note: 'The bound is justified by the mandate'
		});
	});

	it('resolves with an accept-or-return verdict', async () => {
		const onResolve = vi.fn();
		render(GateCard, { props: { gate, onResolve } });
		await fireEvent.change(screen.getByTestId('gate-verdict-select'), {
			target: { value: 'return' }
		});
		await fireEvent.click(screen.getByTestId('gate-verdict-submit'));
		expect(onResolve).toHaveBeenCalledWith({ kind: 'verdict', verdict: 'return' });
	});

	it('shows a resolved gate without resolution controls', () => {
		render(GateCard, {
			props: {
				gate: { ...gate, status: 'resolved', resolution: { kind: 'approve_plan' } }
			}
		});
		expect(screen.getByTestId('gate-resolved')).toHaveTextContent('Resolved');
		expect(screen.queryByTestId('gate-resolution')).toBeNull();
	});
});
