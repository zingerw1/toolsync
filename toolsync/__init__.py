"""toolsync — scan a tools directory and wire every tool onto PATH.

Package layout:
  config    paths, tunables, shared data tables
  state     InstallResult + JSON state / ignore / aliases files
  fsutil    signatures, chmod, shebang healing, name normalisation
  runners   foreign-artifact interpreters (.jar/.exe/.ps1)
  detect    entry-point detection (python / binary / prebuilt / foreign)
  linking   command-name resolution + everything that writes into BIN_DIR
  verify    runtime verification + build-error -> apt-package mapping
  handlers  per-directory install handlers + the detection pipeline
  report    pre-scan report checks
  cli       argument parsing + sync orchestration
"""

__version__ = "1.0.0"

from .cli import main  # noqa: E402,F401
