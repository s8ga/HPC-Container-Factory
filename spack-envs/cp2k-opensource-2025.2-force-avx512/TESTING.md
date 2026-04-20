
Use this to test:
python3 /opt/cp2k/tests/do_regtest.py --keepalive --ompthreads 2 --mpiranks 2 --maxtasks $(nproc) --workbasedir /tmp /opt/spack-view/bin/ psmp


Results:

Apptainer> python3 /opt/cp2k/tests/do_regtest.py --keepalive --ompthreads 2 --mpiranks 2 --maxtasks $(nproc) --workbasedir /tmp /opt/spack-view/bin/ psmp
*************************** Testing started ****************************

----------------------------- Settings ---------------------------------
MPI ranks:      2
OpenMP threads: 2
GPU devices:    0
Workers:        5
Timeout [s]:    400
Work base dir:  /tmp/TEST-2026-04-20_17-40-43
MPI exec:       mpiexec -n {N} --bind-to none
Smoke test:     False
Valgrind:       False
Keepalive:      True
Flag slow:      False
Debug:          False
Binary dir:     /opt/spack-view/bin
VERSION:        psmp
Flags:          omp,libint,fftw3,libxc,libgrpp,pexsi,elpa,parallel,scalapack,mpi_f08,cosma,ace,deepmd,xsmm,plumed2,spglib,libdftd4,mctc-lib,tblite,sirius,sirius_vcsqnm,libvori,libbqb,libtorch,libvdwxc,hdf5,trexio,libsmeagol,greenx
------------------------------------------------------------------------

------------------------------- Errors ---------------------------------


------------------------------- Timings --------------------------------
Plot: name="timings", title="Timing Distribution", ylabel="time [s]"
PlotPoint: name="100th_percentile", plot="timings", label="100th %ile", y=19.82, yerr=0.0
PlotPoint: name="99th_percentile", plot="timings", label="99th %ile", y=5.21, yerr=0.0
PlotPoint: name="98th_percentile", plot="timings", label="98th %ile", y=4.33, yerr=0.0
PlotPoint: name="95th_percentile", plot="timings", label="95th %ile", y=3.12, yerr=0.0
PlotPoint: name="90th_percentile", plot="timings", label="90th %ile", y=2.33, yerr=0.0
PlotPoint: name="80th_percentile", plot="timings", label="80th %ile", y=1.44, yerr=0.0

------------------------------- Summary --------------------------------
Number of FAILED  tests 0
Number of WRONG   tests 0
Number of CORRECT tests 4673
Total number of   tests 4673

Summary: correct: 4673 / 4673; 14min
Status: OK

*************************** Testing ended ******************************