"""Echo-Sense assistive device software."""
import os as _os

# The pipeline does many small array operations. Multi-threaded BLAS (OpenBLAS on Linux/ARM) starts
# spinning worker threads for each tiny matrix product; on the Orange Pi that burns cores and adds
# jitter without speeding anything up. Must be set before numpy is imported.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    _os.environ.setdefault(_v, "1")
