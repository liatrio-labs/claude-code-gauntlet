"""Throwaway #349 red proof: apply one named mutation that reverts a Windows-only fix."""
import pathlib, subprocess, sys

def swap(path, old, new, min_count=1):
    p = pathlib.Path(path)
    s = p.read_text(encoding="utf-8")
    n = s.count(old)
    assert n >= min_count, f"{path}: expected {old!r} at least {min_count}x, found {n}"
    p.write_bytes(s.replace(old, new).encode("utf-8"))
    print(f"mutated {path}: {n}x {old!r} -> {new!r}")

GCR = "scripts/generate_contract_requirements.py"
MUTATIONS = {
    "control": [],
    "b2-display-sep": [("scripts/collect_project_rules.py", 'os.path.relpath(real, self.repo_root).replace(os.sep, "/")', "os.path.relpath(real, self.repo_root)")],
    "c3-posixpath": [(GCR, "posixpath.join(", "os.path.join("), (GCR, "posixpath.normpath(", "os.path.normpath("), (GCR, "posixpath.dirname(", "os.path.dirname(")],
    "d2-gcr-newline": [(GCR, 'open(abs_path, "w", encoding="utf-8", newline="")', 'open(abs_path, "w", encoding="utf-8")')],
    "d1-stdio": [("scripts/script_io.py", '    """Use Python UTF-8 mode stream encodings and LF output at CLI boundaries."""\n', '    """Use Python UTF-8 mode stream encodings and LF output at CLI boundaries."""\n    return\n')],
    "a1-getuid": [("scripts/await_workflow.py", 'getuid = getattr(os, "getuid", None)', "getuid = os.getuid")],
    "e-esm-uri": [("tests/test_assemble_artifacts.py", 'Path(REPO_ROOT, "workflows", "src", "stages.js").as_uri()', 'str(Path(REPO_ROOT, "workflows", "src", "stages.js"))')],
}
name = sys.argv[1]
if name == "gitattributes":
    run = lambda *a: subprocess.run(a, check=True)
    run("git", "rm", "-q", "--cached", ".gitattributes")
    pathlib.Path(".gitattributes").unlink()
    files = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout.split(b"\0")
    for f in filter(None, files):
        pathlib.Path(f.decode("utf-8")).unlink(missing_ok=True)
    run("git", "-c", "core.autocrlf=true", "checkout", "--", ".")
    crlf = b"\r\n" in pathlib.Path("workflows/pipeline.js").read_bytes()
    print("re-checked out without .gitattributes; pipeline.js has CRLF:", crlf)
    assert crlf
else:
    for args in MUTATIONS[name]:
        swap(*args)
