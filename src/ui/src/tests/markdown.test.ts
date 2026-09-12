import { describe, expect, it } from 'vitest';
import { renderMarkdown, stabilizeStreamingMarkdown } from '$lib/markdown';

describe('renderMarkdown', () => {
	it('renders headings, bold, lists, and inline code', () => {
		const html = renderMarkdown('# Title\n\n**bold** text\n\n- one\n- two\n\n`code`');
		expect(html).toContain('<h1');
		expect(html).toContain('<strong>bold</strong>');
		expect(html).toContain('<ul>');
		expect(html).toContain('<li>one</li>');
		expect(html).toContain('<code>code</code>');
	});

	it('renders fenced code blocks', () => {
		const html = renderMarkdown('```js\nconst x = 1;\n```');
		expect(html).toContain('<pre>');
		expect(html).toContain('const x = 1;');
	});

	it('sanitizes script tags and event handler attributes', () => {
		const html = renderMarkdown(
			'<script>alert(1)</script>\n\n<img src="x" onerror="alert(2)">'
		);
		expect(html).not.toContain('<script');
		expect(html).not.toContain('onerror');
		expect(html).not.toContain('alert(1)');
	});

	it('renders a streaming partial fenced block without throwing', () => {
		const html = renderMarkdown('intro\n\n```py\nx = 1');
		expect(html).toContain('<pre>');
		expect(html).toContain('x = 1');
	});
});

describe('stabilizeStreamingMarkdown', () => {
	it('closes an unterminated code fence', () => {
		const stabilized = stabilizeStreamingMarkdown('text\n```js\nconst x = 1');
		expect(stabilized.endsWith('```')).toBe(true);
		expect(stabilized.match(/```/g)?.length).toBe(2);
	});

	it('leaves an already-balanced fence untouched', () => {
		const balanced = 'text\n```js\nconst x = 1\n```';
		expect(stabilizeStreamingMarkdown(balanced)).toBe(balanced);
	});

	it('leaves markdown without fences untouched', () => {
		const plain = 'no code here';
		expect(stabilizeStreamingMarkdown(plain)).toBe(plain);
	});
});