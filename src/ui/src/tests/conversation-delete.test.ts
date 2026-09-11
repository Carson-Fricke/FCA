import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import ConversationDeleteDialog from '$lib/components/chat/ConversationDeleteDialog.svelte';

describe('ConversationDeleteDialog', () => {
	it('names the conversation and confirms deletion through the modal control', async () => {
		const onConfirm = vi.fn();
		const onClose = vi.fn();
		render(ConversationDeleteDialog, {
			props: { title: 'Momentum breakout tuning', onConfirm, onClose }
		});
		await tick();

		expect(screen.getByTestId('confirm-delete-conversation')).toBeInTheDocument();
		expect(screen.getByText(/Momentum breakout tuning/)).toBeInTheDocument();

		await fireEvent.click(screen.getByTestId('confirm-delete-conversation-button'));
		await tick();
		expect(onConfirm).toHaveBeenCalledTimes(1);
		expect(onClose).not.toHaveBeenCalled();
	});

	it('closes without deleting when cancelled', async () => {
		const onConfirm = vi.fn();
		const onClose = vi.fn();
		render(ConversationDeleteDialog, {
			props: { title: 'Overnight drift', onConfirm, onClose }
		});
		await tick();

		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		await tick();
		expect(onClose).toHaveBeenCalledTimes(1);
		expect(onConfirm).not.toHaveBeenCalled();
	});
});
