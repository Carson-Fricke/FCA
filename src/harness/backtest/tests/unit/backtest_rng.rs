use super::*;

#[test]
fn matches_python_seed_7_drift_draws() {
    let mut generator = PythonRandom::from_seed(7);
    let draws: Vec<i64> = (0..12).map(|_| generator.randint(-4, 4)).collect();
    assert_eq!(draws, vec![1, -2, 2, -4, -3, 4, -3, 1, -4, 4, -1, -4]);
}

#[test]
fn replays_from_the_same_seed() {
    let left: Vec<u32> = {
        let mut generator = PythonRandom::from_seed(99);
        (0..8).map(|_| generator.gen_u32()).collect()
    };
    let right: Vec<u32> = {
        let mut generator = PythonRandom::from_seed(99);
        (0..8).map(|_| generator.gen_u32()).collect()
    };
    assert_eq!(left, right);
}

#[test]
fn randbelow_stays_in_range() {
    let mut generator = PythonRandom::from_seed(12345);
    for _ in 0..1000 {
        let value = generator.randbelow(9);
        assert!(value < 9);
    }
}
