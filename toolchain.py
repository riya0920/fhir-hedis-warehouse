"""Locate the external Java toolchain, or say clearly that it is absent.

WHY THIS IS A MODULE AND NOT A HARD-CODED PATH
-----------------------------------------------
Three projects recorded "blocked: needs Java" without ever attempting an
install. The fix is not to hard-code the path that happens to work on this
machine -- that just swaps one unexamined assumption for another. It is to make
the dependency EXPLICIT and OPTIONAL:

  * `java_home()` returns the JDK if one can be found, else None
  * every caller SKIPS rather than fails when it returns None

which is the same discipline the reference audits use for `lifelines` and
`pydicom`. A clone of this repository on a machine with no Java still runs
every test; it just runs fewer of them, and says so.

Search order is deliberate: an explicitly-set `JAVA_HOME` wins, because
somebody who set it meant it. Only then does it look in the user-local install
this repository documents in TOOLCHAIN.md, and finally on `PATH`.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess

TOOLS = os.environ.get("HEALTHCARE_HM_TOOLS",
                       os.path.expanduser("~/tools").replace("\\", "/"))


def java_home():
    """Path to a JDK, or None. Never raises."""
    explicit = os.environ.get("JAVA_HOME")
    if explicit and os.path.exists(os.path.join(explicit, "bin")):
        return explicit

    for pattern in ("jdk-*", "jdk*", "*jdk*"):
        for cand in sorted(glob.glob(os.path.join(TOOLS, pattern)),
                           reverse=True):
            if os.path.exists(os.path.join(cand, "bin")):
                return cand

    exe = shutil.which("java")
    if exe:
        # .../bin/java(.exe) -> the JDK root
        return os.path.dirname(os.path.dirname(exe))
    return None


def java_exe():
    home = java_home()
    if not home:
        return None
    for name in ("java.exe", "java"):
        path = os.path.join(home, "bin", name)
        if os.path.exists(path):
            return path
    return None


def java_version():
    """The reported version string, or None if java cannot be run."""
    exe = java_exe()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-version"], capture_output=True,
                             text=True, timeout=120)
        return (out.stderr or out.stdout).splitlines()[0].strip()
    except Exception:
        return None


def jar(name):
    """Path to a downloaded jar (`synthea.jar`, `validator_cli.jar`), or None.

    Returns None rather than raising so a caller can skip. A jar that is
    present but truncated is worse than one that is absent -- it fails deep
    inside the JVM with an unhelpful message -- so a size floor is applied.
    """
    path = os.path.join(TOOLS, name)
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        return path
    return None


def why_not():
    """A sentence explaining what is missing, for a skip reason."""
    if not java_home():
        return ("no JDK found: set JAVA_HOME, or install one under %s "
                "(see TOOLCHAIN.md)" % TOOLS)
    if not java_version():
        return "a JDK was found at %s but will not run" % java_home()
    return ""


if __name__ == "__main__":
    print("JAVA_HOME :", java_home() or "(none)")
    print("version   :", java_version() or "(cannot run)")
    for name in ("synthea.jar", "validator_cli.jar"):
        path = jar(name)
        print("%-18s %s" % (name, path or "(absent)"))
    note = why_not()
    if note:
        print("note      :", note)
