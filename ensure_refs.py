# -*- coding: utf-8 -*-
"""Auto-decompress vendored reference files that are shipped gzip'd.

The AltAnalyze exon reference (bed_template/altanalyze/refs/Hs/Hs_Ensembl_exon.txt, ~100 MB) is too
large for GitHub's 100 MB per-file limit, so the repo carries only the compressed <name>.txt.gz. This
module recreates the plain-text file the pipeline actually reads.

It is called:
  * on launch, by launch_Win.bat / launch_Mac.command (visible progress on the first run), and again
    from server.main() / tray._start_server() so a direct `python server.py` / `python tray.py` works;
  * defensively by bed_deploy.py right before it uploads the exon ref to the cluster.

Idempotent: a ref is decompressed only when its plain file is missing, so every launch after the first
is just a couple of cheap stat() calls. Decompression streams in 1 MB chunks (never loads the whole
100 MB into memory) and writes atomically (temp + rename) so a killed launch can't leave a truncated
file that later looks complete.
"""
import gzip
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))

# Directories whose *.gz payloads should be auto-expanded to <name> on launch. Add more here if other
# big references get compressed later (psi_template is listed pre-emptively; it simply no-ops if absent).
_REF_DIRS = (
    os.path.join(HERE, "bed_template", "altanalyze", "refs"),
    os.path.join(HERE, "psi_template", "altanalyze", "refs"),
)

_CHUNK = 1024 * 1024   # 1 MB streaming buffer


def _decompress(gz_path, out_path, log):
    """Stream-decompress gz_path -> out_path atomically. Returns True on success."""
    tmp = out_path + ".part"
    try:
        with gzip.open(gz_path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, _CHUNK)
        os.replace(tmp, out_path)               # atomic on Windows + POSIX
        return True
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        if log:
            log("  [refs] FAILED to decompress %s: %s" % (os.path.basename(gz_path), e))
        return False


def ensure_ref_files(log=print):
    """Decompress every <name>.gz under the ref dirs whose plain <name> is missing.

    Returns the number of files decompressed (0 on a warm start, so callers can stay quiet).
    """
    n = 0
    for base in _REF_DIRS:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".gz"):
                    continue
                gz = os.path.join(root, fn)
                out = gz[:-3]                    # strip the trailing ".gz"
                if os.path.exists(out):
                    continue                     # already expanded -> nothing to do
                cmb = os.path.getsize(gz) // (1024 * 1024)
                if log:
                    log("  [refs] decompressing %s (~%d MB) -> %s ..." % (fn, cmb, os.path.basename(out)))
                if _decompress(gz, out, log):
                    n += 1
                    if log:
                        log("  [refs] ready: %s (%d MB)" % (os.path.basename(out),
                                                            os.path.getsize(out) // (1024 * 1024)))
    return n


if __name__ == "__main__":
    k = ensure_ref_files()
    if k:
        print("[refs] %d reference file(s) ready." % k)
