import { readFile, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { compile } from 'json-schema-to-typescript';

/** Accept backend exports only: OpenAPI 3.1 components or JSON Schema $defs. */
export async function generateWire(documents) {
	const definitions = {};
	for (const document of documents) {
		if (document.openapi && !document.openapi.startsWith('3.1.')) {
			throw new Error('Use OpenAPI 3.1; OpenAPI 3.0 nullable semantics are not JSON Schema');
		}
		const schemas = document.components?.schemas ?? document.$defs;
		if (!schemas) throw new Error('Backend export must contain components.schemas or $defs');
		for (const name of Object.keys(schemas).sort()) {
			if (name in definitions) throw new Error(`Duplicate backend schema: ${name}`);
			definitions[name] = schemas[name];
		}
	}
	const normalize = (value) => {
		if (Array.isArray(value)) return value.map(normalize);
		if (typeof value === 'string') return value.replace(/^#\/components\/schemas\//, '#/$defs/');
		if (!value || typeof value !== 'object') return value;
		return Object.fromEntries(Object.keys(value).sort().filter((key) => key !== 'title' || typeof value[key] !== 'string').map((key) => [key,
			normalize(value[key])]));
	};
	const schema = normalize({ $schema: 'https://json-schema.org/draft/2020-12/schema', $defs: definitions });
	const names = Object.keys(definitions).sort();
	const types = await compile({ ...schema, type: 'object', additionalProperties: false,
		properties: Object.fromEntries(names.map((name) => [name, { $ref: `#/$defs/${name}` }])), required: names
	}, 'WireTypes', { bannerComment: '', unknownAny: true, style: { singleQuote: true, useTabs: true } });
	return {
		types: '// Generated from backend schema exports. Do not edit.\n' + types,
		schema: JSON.stringify(schema, null, 2) + '\n'
	};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
	const args = process.argv.slice(2);
	const check = args.includes('--check');
	const suppliedPaths = args.filter((arg) => arg !== '--check');
	const paths = suppliedPaths.length ? suppliedPaths : ['backend-wire.json'];
	const result = await generateWire(await Promise.all(paths.map(async (path) => JSON.parse(await readFile(path, 'utf8')))));
	for (const [path, text] of [['src/lib/generated.ts', result.types], ['src/lib/wire-schema.json', result.schema]]) {
		if (check) {
			if (await readFile(path, 'utf8') !== text) throw new Error(`Generated wire artifact is stale: ${path}`);
		} else await writeFile(path, text);
	}
}
