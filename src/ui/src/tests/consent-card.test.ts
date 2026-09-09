import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import ConsentCard from '$lib/components/chat/ConsentCard.svelte';
import { fixtures } from '$lib/fixtures';
import type { ConsentConfirmation, ConsentProposal } from '$lib/types';

const proposal = structuredClone(fixtures.consentProposal.proposal) as ConsentProposal;

describe('ConsentCard (T172 structured confirmation)', () => {
	it('renders the grant proposal verbatim with no free-text confirmation path', async () => {
		const { container } = render(ConsentCard, {
			props: { proposal, onConfirm: vi.fn() }
		});
		await tick();

		expect(screen.getByTestId('consent-card')).toBeInTheDocument();
		expect(screen.getByText(/9f2c1d4b7e8a/)).toBeInTheDocument();
		// environment/action restate appears in both the proposal table and
		// the acknowledgement copy.
		expect(screen.getAllByText('dev').length).toBeGreaterThanOrEqual(2);
		expect(screen.getAllByText('paper').length).toBeGreaterThanOrEqual(2);
		expect(screen.getByText(/max-daily-loss/)).toBeInTheDocument();
		expect(screen.getByText('one-shot')).toBeInTheDocument();

		// Free text confirmation is structurally impossible: no text input,
		// no textarea, nothing that can submit agreement prose.
		expect(container.querySelector('textarea')).toBeNull();
		expect(container.querySelectorAll('input[type="text"]').length).toBe(0);
		expect(container.querySelectorAll('input[type="search"]').length).toBe(0);
	});

	it('only confirms via the structured control restating bundle/environment/action', async () => {
		const onConfirm = vi.fn();
		render(ConsentCard, { props: { proposal, onConfirm } });
		await tick();

		const confirm = screen.getByTestId('consent-confirm');
		expect(confirm).toBeDisabled();

		await fireEvent.click(confirm);
		await tick();
		expect(onConfirm).not.toHaveBeenCalled();

		const checkbox = screen.getByRole('checkbox');
		await fireEvent.click(checkbox);
		await tick();
		expect(confirm).toBeEnabled();
		await fireEvent.click(confirm);
		await tick();

		expect(onConfirm).toHaveBeenCalledTimes(1);
		const payload = onConfirm.mock.calls[0][0] as ConsentConfirmation;
		expect(payload.schema).toBe('fca-thesis-consent-confirmation-v1');
		expect(payload.bundleHash).toBe(proposal.bundleHash);
		expect(payload.environment).toBe(proposal.environment);
		expect(payload.action).toBe(proposal.action);
		expect(payload.confirmationToken).toMatch(/^ctn-/);
	});

	it('keeps the confirm control disabled until the operator acknowledges the exact terms', async () => {
		const onConfirm = vi.fn();
		render(ConsentCard, { props: { proposal, onConfirm } });
		await tick();
		expect(screen.getByText(/I confirm the proposal above exactly/i)).toBeInTheDocument();
		expect(screen.getByText(/Free-text confirmation does not exist/i)).toBeInTheDocument();
	});
});
