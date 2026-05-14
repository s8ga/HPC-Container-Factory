# libfakeintel.so - Intel MKL Optimization for AMD CPUs

A tiny shared library to enable Intel MKL's optimized kernels on AMD CPUs with AVX512 support.

## Background

Intel MKL uses CPU vendor detection to decide which code paths to use. On AMD CPUs, it defaults to slower SSE/AVX code paths even when AVX2/AVX512 is available. This library tricks MKL into thinking it's running on an Intel CPU, enabling the faster kernels.

**Reference**: [Intel MKL on AMD Zen](https://danieldk.eu/Intel-MKL-on-AMD-Zen)

## How It Works

MKL 2025.0+ uses two functions for CPU detection:
- `mkl_serv_intel_cpu_true()` - Returns 1 if Intel CPU
- `mkl_serv_get_cpu_true()` - Returns pointer to the above function

This library provides stub implementations that always return "true", causing MKL to use optimized AVX2/AVX512 kernels on AMD CPUs.

## Building

```bash
# Simple build
make

# Build and verify
make check

# Clean up
make clean
```

Or manually:
```bash
gcc -shared -fPIC -O2 -o libfakeintel.so fakeintel.c
```

## Usage

### Standalone
```bash
export LD_PRELOAD=/path/to/libfakeintel.so
cp2k.psmp input.inp
```

### With CP2K Container
The entrypoint script automatically detects AMD CPUs with AVX512 and sets `LD_PRELOAD` accordingly.

## Verification

Check exported symbols:
```bash
nm -D libfakeintel.so | grep mkl_serv
```

Expected output:
```
0000000000001110 T mkl_serv_get_cpu_true
0000000000001100 T mkl_serv_intel_cpu_true
```

## Performance Impact

On AMD Zen CPUs with AVX512:
- **sgemm**: ~3.5x faster (237 GF/s → 851 GF/s in benchmarks)
- **dgemm**: Slight improvement even with native kernels

## License

This is a minimal stub library for enabling performance optimizations. Use at your own discretion and review Intel MKL's license terms.
