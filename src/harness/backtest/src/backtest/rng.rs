//! A byte-exact port of CPython's Mersenne Twister (`random.Random`).
//!
//! The synthetic benchmark fixture draws its price drift from
//! `random.Random(configuration.seed).randint(-drift, drift)`. To keep that
//! fixture reproducible across the Python and Rust engines, this module ports
//! CPython's MT19937 seeding (`init_by_array` over little-endian 32-bit seed
//! words) and its `getrandbits`/`randbelow` rejection sampling.

const N: usize = 624;
const M: usize = 397;
const MATRIX_A: u32 = 0x9908_b0df;
const UPPER_MASK: u32 = 0x8000_0000;
const LOWER_MASK: u32 = 0x7fff_ffff;

/// A deterministic CPython-compatible pseudo-random generator.
#[derive(Clone, Debug)]
pub struct PythonRandom {
    state: [u32; N],
    index: usize,
}

impl PythonRandom {
    /// Seed the generator from a non-negative integer, like `random.Random(n)`.
    #[must_use]
    pub fn from_seed(seed: u64) -> Self {
        let mut key: Vec<u32> = Vec::new();
        let mut remaining = seed;
        while remaining > 0 {
            key.push((remaining & 0xffff_ffff) as u32);
            remaining >>= 32;
        }
        if key.is_empty() {
            key.push(0);
        }
        let mut generator = Self {
            state: [0; N],
            index: N,
        };
        generator.init_by_array(&key);
        generator
    }

    fn init_genrand(&mut self, seed: u32) {
        self.state[0] = seed;
        for index in 1..N {
            let previous = self.state[index - 1];
            self.state[index] = 1_812_433_253u32
                .wrapping_mul(previous ^ (previous >> 30))
                .wrapping_add(index as u32);
        }
        self.index = N;
    }

    fn init_by_array(&mut self, key: &[u32]) {
        self.init_genrand(19_650_218);
        let mut i = 1usize;
        let mut j = 0usize;
        let mut k = N.max(key.len());
        while k > 0 {
            let previous = self.state[i - 1];
            self.state[i] = (self.state[i]
                ^ ((previous ^ (previous >> 30)).wrapping_mul(1_664_525)))
            .wrapping_add(key[j])
            .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= N {
                self.state[0] = self.state[N - 1];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
            k -= 1;
        }
        k = N - 1;
        while k > 0 {
            let previous = self.state[i - 1];
            self.state[i] = (self.state[i]
                ^ ((previous ^ (previous >> 30)).wrapping_mul(1_566_083_941)))
            .wrapping_sub(i as u32);
            i += 1;
            if i >= N {
                self.state[0] = self.state[N - 1];
                i = 1;
            }
            k -= 1;
        }
        self.state[0] = UPPER_MASK;
    }

    /// Return the next raw 32-bit MT19937 output.
    pub fn gen_u32(&mut self) -> u32 {
        if self.index >= N {
            self.twist();
        }
        let mut value = self.state[self.index];
        self.index += 1;
        value ^= value >> 11;
        value ^= (value << 7) & 0x9d2c_5680;
        value ^= (value << 15) & 0xefc6_0000;
        value ^= value >> 18;
        value
    }

    fn twist(&mut self) {
        for index in 0..N {
            let value =
                (self.state[index] & UPPER_MASK) | (self.state[(index + 1) % N] & LOWER_MASK);
            let mut next = self.state[(index + M) % N] ^ (value >> 1);
            if value & 1 != 0 {
                next ^= MATRIX_A;
            }
            self.state[index] = next;
        }
        self.index = 0;
    }

    /// Return `k` random bits, matching `Random.getrandbits(k)` for `k <= 32`.
    pub fn getrandbits(&mut self, bits: u32) -> u32 {
        debug_assert!(bits <= 32);
        self.gen_u32() >> (32 - bits)
    }

    /// Return a uniform integer in `[0, bound)`.
    pub fn randbelow(&mut self, bound: u32) -> u32 {
        if bound == 0 {
            panic!("randbelow requires a positive bound");
        }
        if bound == 1 {
            return 0;
        }
        let bits = 32 - (bound - 1).leading_zeros();
        loop {
            let candidate = self.getrandbits(bits);
            if candidate < bound {
                return candidate;
            }
        }
    }

    /// Return a uniform integer in `[low, high]`, like `Random.randint`.
    pub fn randint(&mut self, low: i64, high: i64) -> i64 {
        let span = u32::try_from(high - low + 1).expect("randint span");
        low + i64::from(self.randbelow(span))
    }
}
