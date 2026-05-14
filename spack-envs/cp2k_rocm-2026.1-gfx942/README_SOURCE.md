# Source Credit

This environment is derived from:

- https://github.com/amd/InfinityHub-CI
- Path: cp2k/docker/cp2k_environment

Upstream license:

- MIT License
- Copyright (c) 2023 Advanced Micro Devices, Inc.

Local adaptations in this repository:

1. Keep ROCm base compatibility at 7.2.1.
2. Keep local install layout (`/opt/spack`, `/tmp/spack-build`) instead of upstream `/opt/cp2k-dist`.
3. Install OpenMPI/UCX/UCC through Spack specs instead of Dockerfile source build.
4. Keep HIP stack as ROCm externals (discovery-first, no forced HIP package downloads).
5. Enable DLAF via Spack (`dla-future-fortran` and `cp2k+dlaf`).
6. Add a custom `ucc` package override to pass explicit `--with-rocm-arch=--offload-arch=<gfx*>` on GPU-less build hosts.