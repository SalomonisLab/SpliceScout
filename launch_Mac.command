#!/bin/bash
# GEO RNA-seq Pipeline - macOS launcher (double-click from Finder, or run ./launch_Mac.command).
# First run installs the Python packages automatically. Windows users: use launch_Win.bat instead.
#
# First time on a Mac you may need to make it runnable / clear the download quarantine:
#     chmod +x launch_Mac.command
#     xattr -d com.apple.quarantine launch_Mac.command   2>/dev/null
# ...or just right-click the file in Finder and choose Open the first time.

# Run relative to this script's folder, so runs/ lands next to the pipeline code.
cd "$(dirname "$0")" || exit 1

echo "============================================================"
echo "  GEO RNA-seq Pipeline - launcher (macOS)"
echo "  First run installs the Python packages automatically."
echo "============================================================"
echo

pause_close() { echo; read -n 1 -s -r -p "Press any key to close..."; echo; }

# ---- 1) Find a working Python 3 ----
PY=""
if command -v python3 >/dev/null 2>&1 && python3 -c "import sys" >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1 && python -c "import sys; raise SystemExit(0 if sys.version_info[0]==3 else 1)" >/dev/null 2>&1; then
  PY="python"
fi

if [ -z "$PY" ]; then
  echo "Python 3 was not found."
  if command -v brew >/dev/null 2>&1; then
    echo "Installing Python 3 via Homebrew (brew install python)..."
    brew install python
    echo
    echo "Python was installed. Please run launch_Mac.command again."
  else
    echo "Install Python 3, then run launch_Mac.command again:"
    echo "    - from https://www.python.org/downloads/macos/   (ticks the PATH for you), OR"
    echo "    - install Homebrew from https://brew.sh  then:  brew install python"
  fi
  pause_close
  exit 1
fi
echo "Using Python: $("$PY" --version 2>&1)  ($(command -v "$PY"))"
echo

# ---- 2) Ensure required packages (installs only when something is missing) ----
if ! "$PY" -c "import anthropic, openai, openpyxl" >/dev/null 2>&1; then
  echo "Installing required packages (first run only, needs internet)..."
  "$PY" -m ensurepip >/dev/null 2>&1
  "$PY" -m pip install --upgrade pip >/dev/null 2>&1
  if ! "$PY" -m pip install -r requirements.txt; then
    echo
    echo "Package install failed - check your internet connection and try again."
    echo "(If pip reports a permissions error, retry:  $PY -m pip install --user -r requirements.txt )"
    pause_close
    exit 1
  fi
  echo "Done installing."
  echo
fi

# ---- 3) Optional: paramiko enables SSH *password* auth for the cluster upload ----
#      (SSH key / agent auth works fine without it, so this never blocks launch)
if ! "$PY" -c "import paramiko" >/dev/null 2>&1; then
  echo "Installing optional package 'paramiko' (SSH password auth)..."
  "$PY" -m pip install paramiko >/dev/null 2>&1
fi

# ---- 3b) Vendored Plotly for the Plots tab (download once if missing) ----
if [ ! -f "vendor/plotly.min.js" ]; then
  echo "Downloading the Plotly charting library (first run only, for the Plots tab)..."
  mkdir -p vendor
  curl -fsSL -o vendor/plotly.min.js https://cdn.plot.ly/plotly-2.35.2.min.js \
    || echo "Plotly download failed - the Plots tab will need internet once."
fi

# ---- 3c) Decompress vendored reference DB(s) shipped gzip'd (first run only, ~18 MB -> ~100 MB) ----
"$PY" ensure_refs.py

# ---- 4) Launch the web UI (opens your browser) ----
echo "Starting the web UI - a browser window will open shortly."
echo "Keep this window open while you work; close it (or press Ctrl+C) to stop."
echo "TIP: run launch_Mac.command again to start ANOTHER instance for a concurrent project --"
echo "     each one prompts for its own name and gets its own port."
echo
# Name this instance -> its cluster JOB_TAG (namespaces this project's LSF jobs); blank = auto sraN.
read -r -p "Name this instance (e.g. A549, MDAMB231) [blank = auto sra1/sra2/...]: " SPLICESCOUT_INSTANCE
export SPLICESCOUT_INSTANCE
if [ -n "$SPLICESCOUT_INSTANCE" ]; then
  echo "Using instance name \"$SPLICESCOUT_INSTANCE\" as the cluster JOB_TAG."
else
  echo "No name given - this instance will auto-pick the next free tag (sra1, sra2, ...)."
fi
echo
"$PY" server.py

code=$?
if [ "$code" -ne 0 ]; then
  echo
  echo "The server exited with an error (code $code) - see the messages above."
fi
pause_close
