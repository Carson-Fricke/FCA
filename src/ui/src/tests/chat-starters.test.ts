import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import ChatStarters from '$lib/components/chat/ChatStarters.svelte';

describe('ChatStarters', () => {
	it('renders three starters and reports the selected prompt', async () => {
		const onSelect = vi.fn();
		render(ChatStarters, { props: { onSelect } });
		await tick();

		const starters = screen.getAllByTestId('chat-starter');
		expect(starters).toHaveLength(3);

		await fireEvent.click(starters[1]);
		expect(onSelect).toHaveBeenCalledWith('Help me build a new strategy');
	});

	it('disables every starter when disabled', async () => {
		render(ChatStarters, { props: { onSelect: vi.fn(), disabled: true } });
		await tick();

		for (const starter of screen.getAllByTestId('chat-starter')) {
			expect(starter).toBeDisabled();
		}
	});
});
