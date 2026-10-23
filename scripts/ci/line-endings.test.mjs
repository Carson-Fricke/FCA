import assert from 'node:assert/strict';
import test from 'node:test';
import { inventory, normalizeLF, violations } from './line-endings.mjs';

test('LF policy checks both local bytes and staged bytes while respecting binary attributes', () => {
  const entries = inventory([
    'i/lf    w/crlf  attr/text=auto eol=lf\tSDK source.rs',
    'i/crlf  w/lf    attr/text=auto eol=lf\tstaged.py',
    'i/lf    w/mixed attr/text=auto eol=lf\tnew.txt',
    'i/lf    w/lf    attr/text=auto eol=lf\tclean.py',
    'i/-text w/-text attr/-text\timage.png',
    'i/crlf  w/crlf  attr/-text\tprotocol.bin',
  ].join('\0') + '\0');
  assert.deepEqual(violations(entries).map(entry => entry.path), ['SDK source.rs', 'staged.py', 'new.txt']);
  assert.deepEqual(violations(entries, true).map(entry => entry.path), ['staged.py']);
});

test('normalization changes CRLF only and preserves UTF-8, lone CR and final-newline presence', () => {
  assert.deepEqual(normalizeLF(Buffer.from('café\r\nnext\nlast\r')), Buffer.from('café\nnext\nlast\r'));
  assert.deepEqual(normalizeLF(Buffer.from('no final newline')), Buffer.from('no final newline'));
});
