import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import ChatComposer from '$lib/components/chat/ChatComposer.svelte';

describe('ChatComposer keyboard', () => {
	it('sends on Enter and keeps Shift+Enter for newlines', async () => {
		const onSubmit = vi.fn();
		render(ChatComposer, { props: { onSubmit } });
		await tick();

		const box = screen.getByRole('textbox');
		await fireEvent.input(box, { target: { value: 'hello' } });
		await tick();

		await fireEvent.keyDown(box, { key: 'Enter', shiftKey: true });
		expect(onSubmit).not.toHaveBeenCalled();

		await fireEvent.keyDown(box, { key: 'Enter' });
		expect(onSubmit).toHaveBeenCalledTimes(1);
		expect(onSubmit.mock.calls[0][0].body).toBe('hello');
	});
});
