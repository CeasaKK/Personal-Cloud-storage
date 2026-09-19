/*
 * Native kernels for cloudstore (TDD §4.4 and §6).
 *
 *  - GF(2^8) region multiply-accumulate using the split-nibble technique:
 *      c*x = LO[x & 0x0f] ^ HI[x >> 4]
 *    where LO/HI are 16-entry tables, so one 16/32-byte vector table lookup
 *    (NEON vqtbl1q_u8, SSSE3/AVX2 pshufb) multiplies 16/32 bytes at once.
 *  - gf_matmul: out[r] = sum_c coeffs[r][c] * in[c], cache-blocked; used for both
 *    Reed–Solomon encode (parity rows) and decode (inverse rows).
 *  - Rabin fingerprint content-defined chunking (TDD §6).
 *
 * Loaded through ctypes; no Python headers needed. Build: see build.py.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define CS_NEON 1
#elif defined(__AVX2__)
#include <immintrin.h>
#define CS_AVX2 1
#elif defined(__SSSE3__)
#include <tmmintrin.h>
#define CS_SSSE3 1
#endif

/* ---------------- GF(2^8), polynomial 0x11D ---------------- */

static uint8_t GF_EXP[512];
static uint8_t GF_LOG[256];
static int gf_ready = 0;

static void gf_init(void) {
    if (gf_ready) return;
    unsigned x = 1;
    for (int i = 0; i < 255; i++) {
        GF_EXP[i] = (uint8_t)x;
        GF_LOG[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100) x ^= 0x11D;
    }
    for (int i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255];
    gf_ready = 1;
}

static inline uint8_t gf_mul1(uint8_t a, uint8_t b) {
    if (a == 0 || b == 0) return 0;
    return GF_EXP[GF_LOG[a] + GF_LOG[b]];
}

const char *cs_backend_name(void) {
#if defined(CS_NEON)
    return "neon";
#elif defined(CS_AVX2)
    return "avx2";
#elif defined(CS_SSSE3)
    return "ssse3";
#else
    return "scalar";
#endif
}

static void xor_region(uint8_t *dst, const uint8_t *src, size_t n) {
    size_t i = 0;
#if defined(CS_NEON)
    for (; i + 64 <= n; i += 64) {
        uint8x16_t a0 = veorq_u8(vld1q_u8(dst + i), vld1q_u8(src + i));
        uint8x16_t a1 = veorq_u8(vld1q_u8(dst + i + 16), vld1q_u8(src + i + 16));
        uint8x16_t a2 = veorq_u8(vld1q_u8(dst + i + 32), vld1q_u8(src + i + 32));
        uint8x16_t a3 = veorq_u8(vld1q_u8(dst + i + 48), vld1q_u8(src + i + 48));
        vst1q_u8(dst + i, a0);
        vst1q_u8(dst + i + 16, a1);
        vst1q_u8(dst + i + 32, a2);
        vst1q_u8(dst + i + 48, a3);
    }
#elif defined(CS_AVX2)
    for (; i + 32 <= n; i += 32) {
        __m256i d = _mm256_loadu_si256((const __m256i *)(dst + i));
        __m256i s = _mm256_loadu_si256((const __m256i *)(src + i));
        _mm256_storeu_si256((__m256i *)(dst + i), _mm256_xor_si256(d, s));
    }
#elif defined(CS_SSSE3)
    for (; i + 16 <= n; i += 16) {
        __m128i d = _mm_loadu_si128((const __m128i *)(dst + i));
        __m128i s = _mm_loadu_si128((const __m128i *)(src + i));
        _mm_storeu_si128((__m128i *)(dst + i), _mm_xor_si128(d, s));
    }
#else
    for (; i + 8 <= n; i += 8) {
        uint64_t a, b;
        memcpy(&a, dst + i, 8);
        memcpy(&b, src + i, 8);
        a ^= b;
        memcpy(dst + i, &a, 8);
    }
#endif
    for (; i < n; i++) dst[i] ^= src[i];
}

/* dst ^= c * src */
void cs_gf_mul_add(uint8_t *dst, const uint8_t *src, size_t n, uint8_t c) {
    gf_init();
    if (c == 0) return;
    if (c == 1) { xor_region(dst, src, n); return; }
    uint8_t lo[16], hi[16];
    for (int x = 0; x < 16; x++) {
        lo[x] = gf_mul1(c, (uint8_t)x);
        hi[x] = gf_mul1(c, (uint8_t)(x << 4));
    }
    size_t i = 0;
#if defined(CS_NEON)
    uint8x16_t tlo = vld1q_u8(lo), thi = vld1q_u8(hi), m4 = vdupq_n_u8(0x0f);
    for (; i + 32 <= n; i += 32) {
        uint8x16_t s0 = vld1q_u8(src + i), s1 = vld1q_u8(src + i + 16);
        uint8x16_t p0 = veorq_u8(vqtbl1q_u8(tlo, vandq_u8(s0, m4)), vqtbl1q_u8(thi, vshrq_n_u8(s0, 4)));
        uint8x16_t p1 = veorq_u8(vqtbl1q_u8(tlo, vandq_u8(s1, m4)), vqtbl1q_u8(thi, vshrq_n_u8(s1, 4)));
        vst1q_u8(dst + i, veorq_u8(vld1q_u8(dst + i), p0));
        vst1q_u8(dst + i + 16, veorq_u8(vld1q_u8(dst + i + 16), p1));
    }
#elif defined(CS_AVX2)
    __m256i tlo = _mm256_broadcastsi128_si256(_mm_loadu_si128((const __m128i *)lo));
    __m256i thi = _mm256_broadcastsi128_si256(_mm_loadu_si128((const __m128i *)hi));
    __m256i m4 = _mm256_set1_epi8(0x0f);
    for (; i + 32 <= n; i += 32) {
        __m256i s = _mm256_loadu_si256((const __m256i *)(src + i));
        __m256i l = _mm256_shuffle_epi8(tlo, _mm256_and_si256(s, m4));
        __m256i h = _mm256_shuffle_epi8(thi, _mm256_and_si256(_mm256_srli_epi64(s, 4), m4));
        __m256i d = _mm256_loadu_si256((const __m256i *)(dst + i));
        _mm256_storeu_si256((__m256i *)(dst + i), _mm256_xor_si256(d, _mm256_xor_si256(l, h)));
    }
#elif defined(CS_SSSE3)
    __m128i tlo = _mm_loadu_si128((const __m128i *)lo);
    __m128i thi = _mm_loadu_si128((const __m128i *)hi);
    __m128i m4 = _mm_set1_epi8(0x0f);
    for (; i + 16 <= n; i += 16) {
        __m128i s = _mm_loadu_si128((const __m128i *)(src + i));
        __m128i l = _mm_shuffle_epi8(tlo, _mm_and_si128(s, m4));
        __m128i h = _mm_shuffle_epi8(thi, _mm_and_si128(_mm_srli_epi64(s, 4), m4));
        __m128i d = _mm_loadu_si128((const __m128i *)(dst + i));
        _mm_storeu_si128((__m128i *)(dst + i), _mm_xor_si128(d, _mm_xor_si128(l, h)));
    }
#endif
    for (; i < n; i++) dst[i] ^= (uint8_t)(lo[src[i] & 0x0f] ^ hi[src[i] >> 4]);
}

/* Scalar-only variant, exported so benchmarks can show the SIMD speed-up. */
void cs_gf_mul_add_scalar(uint8_t *dst, const uint8_t *src, size_t n, uint8_t c) {
    gf_init();
    if (c == 0) return;
    uint8_t row[256];
    for (int x = 0; x < 256; x++) row[x] = gf_mul1(c, (uint8_t)x);
    for (size_t i = 0; i < n; i++) dst[i] ^= row[src[i]];
}

#define CS_BLOCK 32768

/*
 * out[r] = XOR_c coeffs[r*cols + c] * in[c]   for r in [0, rows)
 * Every buffer is n bytes. Processed in 32 KiB column blocks so the working
 * set of all inputs + outputs for one block stays in L1/L2.
 */
void cs_gf_matmul(const uint8_t *coeffs, int rows, int cols,
                  const uint8_t *const *in, uint8_t *const *out, size_t n) {
    gf_init();
    for (size_t off = 0; off < n; off += CS_BLOCK) {
        size_t len = n - off < CS_BLOCK ? n - off : CS_BLOCK;
        for (int r = 0; r < rows; r++) {
            uint8_t *o = out[r] + off;
            memset(o, 0, len);
            for (int c = 0; c < cols; c++) {
                cs_gf_mul_add(o, in[c] + off, len, coeffs[r * cols + c]);
            }
        }
    }
}

/* Single-parity XOR (RAID-5 style), benchmark baseline. */
void cs_xor_parity(const uint8_t *const *in, int cols, uint8_t *out, size_t n) {
    memset(out, 0, n);
    for (int c = 0; c < cols; c++) xor_region(out, in[c], n);
}

/* ---------------- Rabin fingerprint CDC ---------------- */

#define RABIN_WINDOW 64

typedef struct {
    uint64_t pol;
    int pol_shift;
    uint64_t out_table[256];
    uint64_t mod_table[256];
} rabin_tables;

static int deg64(uint64_t p) {
    for (int i = 63; i >= 0; i--)
        if (p & ((uint64_t)1 << i)) return i;
    return -1;
}

static uint64_t pmod(uint64_t x, uint64_t p) {
    int dp = deg64(p);
    while (deg64(x) >= dp) x ^= p << (deg64(x) - dp);
    return x;
}

static uint64_t append_byte(uint64_t hash, uint8_t b, uint64_t pol) {
    hash <<= 8;
    hash |= b;
    return pmod(hash, pol);
}

void cs_rabin_init(rabin_tables *t, uint64_t pol) {
    t->pol = pol;
    int k = deg64(pol);
    t->pol_shift = k - 8;
    for (int b = 0; b < 256; b++) {
        uint64_t h = append_byte(0, (uint8_t)b, pol);
        for (int i = 0; i < RABIN_WINDOW - 1; i++) h = append_byte(h, 0, pol);
        t->out_table[b] = h;
        uint64_t hb = (uint64_t)b << k;
        t->mod_table[b] = pmod(hb, pol) | hb;
    }
}

size_t cs_rabin_tables_size(void) { return sizeof(rabin_tables); }

/*
 * Return the length of the next chunk starting at buf[0]. `eof` says whether
 * buf ends at end of input; if not, the caller must supply at least max_size
 * bytes. The digest restarts at each chunk start (window zero-filled), rolling
 * begins at min_size - WINDOW so a cut only depends on the 64 bytes before it.
 */
size_t cs_rabin_next(const rabin_tables *t, const uint8_t *buf, size_t n,
                     size_t min_size, size_t max_size, uint64_t mask, int eof) {
    if (n <= min_size) return eof ? n : 0;
    size_t limit = n < max_size ? n : max_size;
    uint8_t window[RABIN_WINDOW];
    memset(window, 0, sizeof(window));
    int wpos = 0;
    uint64_t digest = 0;
    size_t start = min_size > RABIN_WINDOW ? min_size - RABIN_WINDOW : 0;
    for (size_t i = start; i < limit; i++) {
        uint8_t b = buf[i];
        uint8_t out = window[wpos];
        window[wpos] = b;
        wpos = (wpos + 1) % RABIN_WINDOW;
        digest ^= t->out_table[out];
        uint64_t index = digest >> t->pol_shift;
        digest <<= 8;
        digest |= b;
        digest ^= t->mod_table[index & 0xff];
        if (i + 1 >= min_size && (digest & mask) == 0) return i + 1;
    }
    if (limit == max_size) return max_size;
    return eof ? n : 0;
}
