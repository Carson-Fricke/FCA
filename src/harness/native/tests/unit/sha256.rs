use super::Sha256;

fn digest(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[test]
fn matches_nist_vectors() {
    assert_eq!(
        digest(b""),
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
    assert_eq!(
        digest(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    assert_eq!(
        digest(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
    );
}

#[test]
fn streaming_matches_one_shot() {
    let data = vec![0x5au8; 1000];
    let mut hasher = Sha256::new();
    for chunk in data.chunks(7) {
        hasher.update(chunk);
    }
    let streamed = hasher.finalize();
    let mut one_shot = Sha256::new();
    one_shot.update(&data);
    assert_eq!(streamed, one_shot.finalize());
}
