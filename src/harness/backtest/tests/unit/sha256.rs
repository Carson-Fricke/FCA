use super::*;

#[test]
fn known_vectors() {
    assert_eq!(
        digest_hex(b""),
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
    assert_eq!(
        digest_hex(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    assert_eq!(
        digest_hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
    );
}

#[test]
fn streaming_matches_one_shot() {
    let payload: Vec<u8> = (0u16..10_000).map(|value| (value % 251) as u8).collect();
    let expected = digest_hex(&payload);
    for chunk in [1usize, 7, 63, 64, 65, 127, 128, 1000] {
        let mut hasher = Sha256::new();
        for slice in payload.chunks(chunk) {
            hasher.update(slice);
        }
        assert_eq!(hasher.finalize_hex(), expected, "chunk size {chunk}");
    }
}

#[test]
fn million_a_vector() {
    let mut hasher = Sha256::new();
    for _ in 0..1_000_000 {
        hasher.update(b"a");
    }
    assert_eq!(
        hasher.finalize_hex(),
        "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"
    );
}
