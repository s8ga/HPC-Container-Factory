/*
 * Fake Intel CPU detection for MKL on AMD CPUs
 * 
 * This library tricks Intel MKL into using optimized AVX2/AVX512 kernels
 * on AMD Zen CPUs that support these instructions.
 * 
 * Usage: LD_PRELOAD=libfakeintel.so <your_program>
 * 
 * Reference: https://danieldk.eu/Intel-MKL-on-AMD-Zen
 */

// For MKL 2025.0 and later
int mkl_serv_intel_cpu_true(void) {
    return 1;
}

typedef int (*fakeintel_fptr)(void);

fakeintel_fptr mkl_serv_get_cpu_true(void) {
    return &mkl_serv_intel_cpu_true;
}
