#!/bin/bash

# this is a dirty fix which force the mkl-blacs-openmpi to be built with the latest openmpi, otherwise the old version of 
# mkl-blacs-openmpi will cause some problems when running cp2k with openmpi 
# 

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

source /opt/spack-exe/share/spack/setup-env.sh

spack env activate cp2k-env

MKL_INSTALL_LOCATION=$(spack -e cp2k-env location -i intel-oneapi-mkl)
TRUE_MKLROOT="${MKL_INSTALL_LOCATION}/mkl/latest"

cd ${TRUE_MKLROOT}/share/mkl/interfaces/mklmpi

# 需要将mkl-patch 里面的 patch文件提前移动/复制进去

cp ${SCRIPT_DIR}/mklmpi-impl-fix_v2.patch ./
patch -p1 < mklmpi-impl-fix_v2.patch

# 这个make 会自动写入 挺头疼的 但是我们也不需要老版本的东西了
# ls -lah /opt/spack/linux-x86_64_v3/intel-oneapi-mkl-2025.3.0-cu5p6yv6aolwnbgdwng7y4j6ewfyseuf/mkl/2025.3/lib/
# 同时 obj_intel64_lp64 这个里面也会放置构建产物

make libintel64 MPICC=$MPICC \
    INSTALL_LIBNAME=libmkl_blacs_openmpi_lp64 \
    MKLROOT=$MKLROOT

mpicc -shared -fPIC -Wl,--whole-archive \
    ${TRUE_MKLROOT}/lib/libmkl_blacs_openmpi_lp64.a \
    -Wl,--no-whole-archive -o ${TRUE_MKLROOT}/lib/libmkl_blacs_openmpi_lp64.so.2 \
    -Wl,-soname,libmkl_blacs_openmpi_lp64.so.2 -lmkl_core -lmpi

# 定义目标文件路径
TARGET_LIB="${TRUE_MKLROOT}/lib/libmkl_blacs_openmpi_lp64.so.2"

# 确保文件存在
if [ ! -f "$TARGET_LIB" ]; then
    echo "Error: Target library not found at $TARGET_LIB"
    exit 1
fi

echo "Verifying MKL-MPI Patch application..."
echo "Should print MKL's X2COMM and X4COMM symbols if patch is successful, otherwise it indicates the patch was not applied correctly."


# 检查 X2COMM 块中是否包含 MPI_Comm_f2c 调用
# 如果 awk 找不到符号或者 grep 找不到调用，都会导致返回值为非 0
objdump -d "$TARGET_LIB" | awk -v RS= '/<X2COMM>:/' | grep "MPI_Comm_f2c"
X2_STATUS=$?

# 检查 X4COMM 块中是否包含 MPI_Comm_c2f 调用
objdump -d "$TARGET_LIB" | awk -v RS= '/<X4COMM>:/' | grep "MPI_Comm_c2f"
X4_STATUS=$?

# 逻辑判断：如果 X2_STATUS 或 X4_STATUS 有任何一个不为 0 (即失败)
if [ $X2_STATUS -ne 0 ] || [ $X4_STATUS -ne 0 ]; then
    echo "--------------------------------------------------------"
    echo "Verification FAILED!"
    [ $X2_STATUS -ne 0 ] && echo "  - X2COMM check failed (MPI_Comm_f2c not found or inlined)"
    [ $X4_STATUS -ne 0 ] && echo "  - X4COMM check failed (MPI_Comm_c2f not found or inlined)"
    echo "The library is still using direct pointer casting (unpatched)."
    echo "--------------------------------------------------------"
    exit 1
fi

echo "Verification SUCCESS: Both X2COMM and X4COMM are correctly patched."
echo "Exiting with status 0."

# 脚本继续运行或 graceful 退出
exit 0
