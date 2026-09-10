/**
 * Client-side boundary feedback for custom screen scripts.
 *
 * Mirrors the light end of the compilation boundary in
 * `src/thesis/screening/programs.py`: exactly one `def screen(ctx)`,
 * imports restricted to `math`/`statistics` without aliases, no other
 * top-level statements, and no dunder attribute access — plus a balanced
 * bracket scan so obvious syntax breakage surfaces before a round trip.
 * Server-side rejections (ProgramViolationError / FilterError) stay
 * authoritative; this is a fast pre-flight, not a parser.
 */

export interface ScriptIssue {
	line: number | null;
	message: string;
	severity: 'error' | 'warning';
}

export const ALLOWED_MODULES: readonly string[] = ['math', 'statistics'];

export const SCREEN_TEMPLATE = `def screen(ctx):
    survivors = []
    for symbol in ctx.available_symbols():
        facts = ctx.fact_sheet(symbol)
        close = facts.get("last_close")
        if close is None:
            continue
        survivors.append({"symbol": symbol, "last_close": str(close)})
    return survivors
`;

export function lintScreenScript(source: string): ScriptIssue[] {
	const text = source.replace(/\r\n/g, '\n');
	if (!text.trim()) {
		return [
			{ line: null, message: 'screener source must be a non-empty string', severity: 'error' }
		];
	}

	const issues: ScriptIssue[] = [];

	// Balanced brackets, skipping # comments and single-line strings.
	const stack: { ch: string; line: number }[] = [];
	let inString: string | null = null;
	let line = 1;
	for (let i = 0; i < text.length; i++) {
		const ch = text[i];
		if (ch === '\n') {
			line++;
			inString = null;
			continue;
		}
		if (inString !== null) {
			if (ch === '\\') i++;
			else if (ch === inString) inString = null;
			continue;
		}
		if (ch === '#') {
			while (i < text.length && text[i] !== '\n') i++;
			i--;
			continue;
		}
		if (ch === '"' || ch === "'") {
			inString = ch;
			continue;
		}
		if (ch === '(' || ch === '[' || ch === '{') {
			stack.push({ ch, line });
		} else if (ch === ')' || ch === ']' || ch === '}') {
			const open = stack.pop();
			if (!open || !matches(open.ch, ch)) {
				issues.push({
					line,
					message: `unbalanced '${ch}'`,
					severity: 'error'
				});
				stack.length = 0;
			}
		}
	}
	if (stack.length) {
		const open = stack[stack.length - 1];
		issues.push({
			line: open.line,
			message: `unbalanced '${open.ch}' — check bracket pairing from this line`,
			severity: 'error'
		});
	}

	// Line-oriented structure checks (top level only, column 0).
	const lines = text.split('\n');
	let screenDefs = 0;
	let otherDefs = 0;
	let hasReturn = false;
	for (let index = 0; index < lines.length; index++) {
		const raw = lines[index];
		const lineNo = index + 1;
		const code = raw.replace(/#.*$/, '');
		if (/^\s*return\b/.test(code)) hasReturn = true;

		if (/\.\s*__\w+__/.test(code)) {
			issues.push({
				line: lineNo,
				message: 'dunder attribute access is not permitted in custom screeners',
				severity: 'error'
			});
		}

		const defMatch = /^def\s+(\w+)\s*\(/.exec(code);
		if (defMatch) {
			if (defMatch[1] === 'screen') screenDefs++;
			else otherDefs++;
			continue;
		}

		const importMatch = /^import\s+([\w.]+)(\s+as\s+\w+)?/.exec(code);
		if (importMatch) {
			const root = importMatch[1].split('.')[0];
			if (!ALLOWED_MODULES.includes(root)) {
				issues.push({
					line: lineNo,
					message: `import of '${importMatch[1]}' is outside the allowlist (${ALLOWED_MODULES.join(', ')})`,
					severity: 'error'
				});
			}
			if (importMatch[2]) {
				issues.push({
					line: lineNo,
					message: 'import aliases are not permitted in custom screeners',
					severity: 'error'
				});
			}
			continue;
		}
		const fromMatch = /^from\s+([\w.]+)\s+import\s+(.+)$/.exec(code);
		if (fromMatch) {
			const root = fromMatch[1].split('.')[0];
			if (!ALLOWED_MODULES.includes(root)) {
				issues.push({
					line: lineNo,
					message: `import from '${fromMatch[1]}' is outside the allowlist (${ALLOWED_MODULES.join(', ')})`,
					severity: 'error'
				});
			}
			if (/\bas\b/.test(fromMatch[2])) {
				issues.push({
					line: lineNo,
					message: 'import aliases are not permitted in custom screeners',
					severity: 'error'
				});
			}
			continue;
		}

		if (/^[A-Za-z_@]/.test(code)) {
			issues.push({
				line: lineNo,
				message: `top-level statement is not permitted in a custom screener (imports and one screen(ctx) function only)`,
				severity: 'error'
			});
		}
	}

	if (screenDefs === 0) {
		issues.push({
			line: null,
			message: 'the module must define a screen(ctx) function',
			severity: 'error'
		});
	} else if (screenDefs > 1 || otherDefs > 0) {
		issues.push({
			line: null,
			message: 'the module must define exactly one screen(ctx) function',
			severity: 'error'
		});
	}
	if (!hasReturn) {
		issues.push({
			line: null,
			message: 'screen(ctx) must return a list of dicts',
			severity: 'warning'
		});
	}

	return issues;
}

function matches(open: string, close: string): boolean {
	return (
		(open === '(' && close === ')') ||
		(open === '[' && close === ']') ||
		(open === '{' && close === '}')
	);
}
