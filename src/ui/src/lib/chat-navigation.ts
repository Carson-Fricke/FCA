import type { ChatHostNavigation } from './chat-host.svelte';

/** Address-bar adapter; SvelteKit's mount-time page URL is not live navigation state. */
export function conversationNavigation(readUrl: () => URL, replace: (url: URL) => void): ChatHostNavigation {
	return {
		read() {
			const url = readUrl();
			return { chatId: url.searchParams.get('chat'), scope: url.searchParams.get('scope'), hash: url.hash };
		},
		select(chatId) {
			const url = readUrl();
			if (chatId !== null && url.searchParams.get('chat') === chatId) return;
			if (chatId) url.searchParams.set('chat', chatId);
			else url.searchParams.delete('chat');
			url.searchParams.delete('scope');
			url.hash = '';
			replace(url);
		},
		scope(scope) {
			const url = readUrl();
			if ((url.searchParams.get('scope') ?? '') === scope) return;
			if (scope) url.searchParams.set('scope', scope);
			else url.searchParams.delete('scope');
			replace(url);
		}
	};
}
