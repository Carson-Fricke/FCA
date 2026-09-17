import DOMPurify from 'dompurify';
import katex from 'katex';
import { marked } from 'marked';
import type { MarkedExtension, RendererExtension, TokenizerExtension, Tokens } from 'marked';

const RENDER_OPTIONS = {
	throwOnError: false,
	strict: false,
	output: 'htmlAndMathml'
} as const;

// KaTeX's collision-free delimiters: `\(...\)` for inline math and `\[...\]`
// for display. `$$...$$` is accepted for display math too. A bare `$` is never
// treated as math, so currency and ranges like `$1,560` render as plain text.
const INLINE_PATTERN = /^\\\(([\s\S]+?)\\\)/;
const DISPLAY_PATTERNS = [/^\\\[([\s\S]+?)\\\]/, /^\$\$([\s\S]+?)\$\$/];

// marked keys extension renderers by the token type, so each token type must
// match its extension name.
const inlineMath: TokenizerExtension & RendererExtension = {
	name: 'inlineMath',
	level: 'inline',
	start(source: string) {
		const index = source.indexOf('\\(');
		return index === -1 ? undefined : index;
	},
	tokenizer(source: string) {
		const match = INLINE_PATTERN.exec(source);
		if (!match) return undefined;
		return { type: 'inlineMath', raw: match[0], text: match[1] };
	},
	renderer(token: Tokens.Generic) {
		return katex.renderToString(token.text as string, {
			...RENDER_OPTIONS,
			displayMode: false
		});
	}
};

const blockMath: TokenizerExtension & RendererExtension = {
	name: 'blockMath',
	level: 'block',
	start(source: string) {
		let earliest = -1;
		for (const opener of ['\\[', '$$']) {
			const index = source.indexOf(opener);
			if (index !== -1 && (earliest === -1 || index < earliest)) earliest = index;
		}
		return earliest === -1 ? undefined : earliest;
	},
	tokenizer(source: string) {
		for (const pattern of DISPLAY_PATTERNS) {
			const match = pattern.exec(source);
			if (match) return { type: 'blockMath', raw: match[0], text: match[1] };
		}
		return undefined;
	},
	renderer(token: Tokens.Generic) {
		return (
			katex.renderToString(token.text as string, {
				...RENDER_OPTIONS,
				displayMode: true
			}) + '\n'
		);
	}
};

marked.use({ extensions: [inlineMath, blockMath] } satisfies MarkedExtension);

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
	return DOMPurify.sanitize(html, {
		USE_PROFILES: { html: true, mathMl: true }
	});
}

function escapeHtml(source: string): string {
	return source
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&#39;');
}