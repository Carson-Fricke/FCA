<script lang="ts">
	import 'katex/dist/katex.min.css';
	import { renderMarkdown } from '$lib/markdown';

	let {
		text,
		class: className = ''
	}: { text: string; class?: string } = $props();
</script>

<div class="markdown-body text-sm break-words text-slate-800 {className}" data-testid="markdown-body">
	{@html renderMarkdown(text)}
</div>

<style>
	.markdown-body :global(h1),
	.markdown-body :global(h2),
	.markdown-body :global(h3),
	.markdown-body :global(h4),
	.markdown-body :global(h5),
	.markdown-body :global(h6) {
		margin: 0.75rem 0 0.375rem;
		font-weight: 600;
		line-height: 1.3;
	}
	.markdown-body :global(h1) {
		font-size: 1.25rem;
	}
	.markdown-body :global(h2) {
		font-size: 1.125rem;
	}
	.markdown-body :global(h3) {
		font-size: 1rem;
	}
	.markdown-body :global(h4),
	.markdown-body :global(h5),
	.markdown-body :global(h6) {
		font-size: 0.95rem;
	}
	.markdown-body :global(p) {
		margin: 0.5rem 0;
	}
	.markdown-body :global(ul),
	.markdown-body :global(ol) {
		margin: 0.5rem 0;
		padding-left: 1.25rem;
	}
	.markdown-body :global(ul) {
		list-style: disc;
	}
	.markdown-body :global(ol) {
		list-style: decimal;
	}
	.markdown-body :global(li) {
		margin: 0.125rem 0;
	}
	.markdown-body :global(a) {
		color: var(--color-accent);
		text-decoration: underline;
	}
	.markdown-body :global(code) {
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 0.85em;
		background: var(--surface-well);
		border-radius: 0.25rem;
		padding: 0.1rem 0.3rem;
	}
	.markdown-body :global(pre) {
		margin: 0.5rem 0;
		padding: 0.625rem;
		border-radius: 0.5rem;
		background: var(--surface-well);
		overflow-x: auto;
	}
	.markdown-body :global(pre code) {
		background: transparent;
		padding: 0;
		font-size: 0.8rem;
	}
	.markdown-body :global(blockquote) {
		margin: 0.5rem 0;
		padding-left: 0.75rem;
		border-left: 3px solid var(--color-surface-600);
		color: var(--color-slate-500);
	}
	.markdown-body :global(table) {
		width: 100%;
		border-collapse: collapse;
		margin: 0.5rem 0;
		font-size: 0.85em;
	}
	.markdown-body :global(th),
	.markdown-body :global(td) {
		border: 1px solid var(--color-surface-700);
		padding: 0.25rem 0.5rem;
		text-align: left;
	}
	.markdown-body :global(th) {
		background: var(--surface-well);
		font-weight: 600;
	}
	.markdown-body :global(img) {
		max-width: 100%;
	}
	.markdown-body :global(hr) {
		margin: 0.75rem 0;
		border: none;
		border-top: 1px solid var(--color-surface-700);
	}
	.markdown-body :global(*:first-child) {
		margin-top: 0;
	}
	.markdown-body :global(*:last-child) {
		margin-bottom: 0;
	}
	.markdown-body :global(.katex) {
		font-size: 1em;
	}
	.markdown-body :global(.katex-display) {
		margin: 0.5rem 0;
		overflow-x: auto;
		overflow-y: hidden;
	}
	.markdown-body :global(.katex-error) {
		color: var(--color-slate-500);
	}
</style>
