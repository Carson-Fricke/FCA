import DOMPurify from 'dompurify';
import { marked } from 'marked';

export function stabilizeStreamingMarkdown(source: string): string {
	const fences = source.match(/^```/gm)?.length ?? 0;
	return fences % 2 === 1 ? `${source}\n\`\`\`` : source;
}

export function renderMarkdown(source: string): string {
	if (typeof window === 'undefined') return escapeHtml(source);
	const html = marked.parse(stabilizeStreamingMarkdown(source), {
		gfm: true,
		breaks: true,
		async: false
	});
	return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}

function escapeHtml(source: string): string {
	return source
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&#39;');
}
