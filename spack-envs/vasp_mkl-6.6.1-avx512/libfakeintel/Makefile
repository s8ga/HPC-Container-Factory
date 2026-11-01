# Makefile for libfakeintel.so
# Tricks Intel MKL into using optimized kernels on AMD CPUs with AVX512
#
# Reference: https://danieldk.eu/Intel-MKL-on-AMD-Zen
# 
# Usage:
#   make          - Build libfakeintel.so
#   make clean    - Remove build artifacts
#   make check    - Verify exported symbols
#   make help     - Show this help message

# Compiler and flags
CC := gcc
CFLAGS := -shared -fPIC -O2 -Wall -Wextra

# Source and output
SRC := fakeintel.c
TARGET := libfakeintel.so

# Default target
.PHONY: all
all: $(TARGET)

# Build the shared library
$(TARGET): $(SRC)
	@echo "Building $(TARGET)..."
	$(CC) $(CFLAGS) -o $@ $<
	@echo "Build complete: $(TARGET)"
	@ls -lh $(TARGET)

# Verify exported symbols
.PHONY: check
check: $(TARGET)
	@echo "Verifying exported symbols..."
	@nm -D $(TARGET) | grep mkl_serv || (echo "ERROR: Required symbols not found!" && exit 1)
	@echo "Symbol verification passed"

# Clean build artifacts
.PHONY: clean
clean:
	@echo "Cleaning build artifacts..."
	rm -f $(TARGET)
	@echo "Clean complete"

# Show help
.PHONY: help
help:
	@echo "libfakeintel.so - MKL optimization for AMD CPUs"
	@echo ""
	@echo "Targets:"
	@echo "  make          - Build libfakeintel.so"
	@echo "  make clean    - Remove build artifacts"
	@echo "  make check    - Verify exported symbols"
	@echo "  make help     - Show this help message"
	@echo ""
	@echo "Usage:"
	@echo "  This library is pre-built and copied into containers."
	@echo "  It should NOT be installed system-wide."
	@echo ""
	@echo "Container usage:"
	@echo "  The entrypoint.sh script automatically sets LD_PRELOAD"
	@echo "  on AMD CPUs with AVX512 support."
