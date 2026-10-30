"""Token-budgeted text reads; callers own resource selection and authorization.

The fixed cl100k_base tokenizer makes cursors portable across Python and Rust.
It is a read-budget tokenizer, not a claim about a provider's context accounting.
Budgets include JSON string escaping; protocol envelopes remain caller-owned.
"""

import json
from functools import lru_cache

import tiktoken

TOKENIZER = "cl100k_base"
DEFAULT_READ_TOKENS = 16_384
MAX_READ_TOKENS = 65_536


@lru_cache(maxsize=1)
def read_tokenizer() -> tiktoken.Encoding:
    return tiktoken.get_encoding(TOKENIZER)


def count_tokens(text: str) -> int:
    return len(read_tokenizer().encode_ordinary(text))


def text_page(
    text: str, *, token_offset: int = 0, max_tokens: int = DEFAULT_READ_TOKENS
) -> dict[str, object]:
    """Read a whole document when it fits, otherwise return an exact UTF-8 page.

    Large reads target three quarters of the limit, leaving room to absorb the
    last page's tail. Cursor offsets address tokens in the original document;
    they never address characters or a retokenized suffix. No content is lost.
    """
    if type(max_tokens) is not int or not 32 <= max_tokens <= MAX_READ_TOKENS:
        raise ValueError(f"max_tokens must be an integer from 32 to {MAX_READ_TOKENS}")
    codec = read_tokenizer()
    tokens = codec.encode_ordinary(text)
    if type(token_offset) is not int or not 0 <= token_offset <= len(tokens):
        raise ValueError(
            f"token_offset must be between 0 and {len(tokens)}; use nextArguments"
        )
    try:
        codec.decode_bytes(tokens[:token_offset]).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            "token_offset splits a UTF-8 character; use nextArguments"
        ) from error

    def decode(end: int) -> str:
        return codec.decode_bytes(tokens[token_offset:end]).decode("utf-8")

    end = len(tokens)
    content = decode(end)
    cost = count_tokens(json.dumps(content, ensure_ascii=False))
    if cost > max_tokens:
        end = min(end, token_offset + max_tokens * 3 // 4)
        while end > token_offset:
            try:
                content = decode(end)
            except UnicodeDecodeError:
                end -= 1
                continue
            cost = count_tokens(json.dumps(content, ensure_ascii=False))
            if cost <= max_tokens:
                break
            end -= min(
                max(1, (end - token_offset) // 2), max(1, (cost - max_tokens) // 2)
            )
        if end <= token_offset:
            raise ValueError(
                "max_tokens cannot fit the next character; increase the budget"
            )
    return {
        "content": content,
        "tokenizer": TOKENIZER,
        "tokenOffset": token_offset,
        "nextTokenOffset": end if end < len(tokens) else None,
        "totalTokens": len(tokens),
        "tokenCount": cost,
        "complete": token_offset == 0 and end == len(tokens),
        "truncated": token_offset != 0 or end != len(tokens),
    }
