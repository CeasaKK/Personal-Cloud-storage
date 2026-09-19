/* Kernel self-test: every SIMD region kernel must equal the scalar reference byte for byte.
   Built and run by tests/test_native_kernels.py for each ISA the host can execute. */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
const char *cs_backend_name(void);
void cs_gf_mul_add(uint8_t *dst, const uint8_t *src, size_t n, uint8_t c);
void cs_gf_mul_add_scalar(uint8_t *dst, const uint8_t *src, size_t n, uint8_t c);
void cs_gf_matmul(const uint8_t *coeffs, int rows, int cols, const uint8_t *const *in, uint8_t *const *out, size_t n);
int main(void) {
    srand(7);
    size_t sizes[] = {0, 1, 15, 16, 31, 32, 33, 100, 4096, 70001};
    int bad = 0;
    for (int c = 0; c < 256; c++) for (int s = 0; s < 10; s++) {
        size_t n = sizes[s];
        uint8_t *src = malloc(n + 1), *a = malloc(n + 1), *b = malloc(n + 1);
        for (size_t i = 0; i < n; i++) { src[i] = rand(); a[i] = b[i] = rand(); }
        cs_gf_mul_add(a, src, n, (uint8_t)c);
        cs_gf_mul_add_scalar(b, src, n, (uint8_t)c);
        if (memcmp(a, b, n)) bad++;
        free(src); free(a); free(b);
    }
    /* 4+2 encode via matmul vs scalar reconstruction of the same products */
    size_t n = 100003; uint8_t coeff[8] = {0x8e, 0x47, 0xad, 0xd8, 0x47, 0x8e, 0xd8, 0xad};
    uint8_t *in[4], *out[2], *ref[2];
    for (int i = 0; i < 4; i++) { in[i] = malloc(n); for (size_t j = 0; j < n; j++) in[i][j] = rand(); }
    for (int r = 0; r < 2; r++) { out[r] = malloc(n); ref[r] = calloc(n, 1);
        for (int c = 0; c < 4; c++) cs_gf_mul_add_scalar(ref[r], in[c], n, coeff[r * 4 + c]); }
    cs_gf_matmul(coeff, 2, 4, (const uint8_t *const *)in, out, n);
    for (int r = 0; r < 2; r++) if (memcmp(out[r], ref[r], n)) bad++;
    printf("%s: %s\n", cs_backend_name(), bad ? "MISMATCH" : "all kernels match scalar reference");
    return bad != 0;
}
