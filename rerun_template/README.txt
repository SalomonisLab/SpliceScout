SpliceScout RE-RUN / RE-SCORE kit (built by rerun_deploy.py)
============================================================
Brings a better PSI plan or a changed concordance to a cell line whose cluster chain already ran, without redoing
what is already done. build_summary.json says what this kit was built from; rerun.env names its folders and job tags.

FULL RE-RUN (rerun_submit.sh) -- unzip into <run root>/rerun/ on the cluster, then on the LSF submit host:
    DRY_RUN=1 bash <run root>/rerun/rerun_submit.sh      # plan status, changes nothing
    bash <run root>/rerun/rerun_submit.sh
  download -> STAR -> BED for exactly the samples of the new plan (rerun_plan_runs.tsv) that have no BED yet, then the
  new PSI (psi_bundle.zip) and concordance (concordance_bundle.zip) stages in NEW folders. Samples whose runs are
  still downloaded but nested (<acc>/<acc>.sra[.gz]) are unpacked by an LSF job array (recover_nested.sh) instead of
  being downloaded again. Everything the script moves or replaces is kept in <run root>/rerun_backup_<time>/.

RE-SCORE (rescore_submit.sh) -- unzip into <run root>/rescore/, then:
    DRY_RUN=1 bash <run root>/rescore/rescore_submit.sh
    bash <run root>/rescore/rescore_submit.sh
  concordance only, over the existing finished PSI stage, into a NEW folder.

PROMOTION (promote_launch.sh, armed by either submit script when rerun.env sets FINAL_CONC_DIR): once the new
concordance has finished, the new results move into the ORIGINAL folders (FINAL_PSI_DIR / FINAL_CONC_DIR); their
previous contents are kept as <folder>.old_<date> -- nothing is deleted. It never promotes a STALLED concordance.
Log: <kit>/promote.log ; done when <kit>/PROMOTED.txt exists.

Files: rerun_submit.sh | rescore_submit.sh (drivers), recover_nested.sh (unpack job), promote_launch.sh,
       rerun.env, rerun_plan_runs.tsv (label / run / study of every planned run), download/ star/ bed/ (current
       stage scripts the driver installs), bed_launch.sh (STAR -> BED hand-off), psi_bundle.zip, concordance_bundle.zip.
Full write-up: DEVELOPER_GUIDE.md, "Targeted re-runs".
