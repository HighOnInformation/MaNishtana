"""Layer 2 of binary comparison: which functions changed, using external engines.

Public interface:
    ENGINES                                     every engine name this version knows
    FunctionChange   namedtuple(name_a, name_b, status, similarity, views, pseudo_diff)
    discover(binary_settings) -> tools          {tool: path or None}
    runnable(tools) -> [engine]                 engines whose tools were all found
    describe_tools(tools) -> str                the --check-binary-tools report
    diff_functions(old, new, engines, tools, binary_settings, run=None) -> FunctionDiff
    FunctionDiff     namedtuple(engines, changes, notes)   engines = the ones that succeeded
    decompiled_diff(changes, limit) -> str | None

Each engine is an adapter that turns one tool's native output into FunctionChange:

    diaphora   IDA (idat) exports each side with diaphora.py, then diaphora.py diffs
               the two exports; results and pseudo-code are read from SQLite.
    bindiff    BinExport writes each side (IDA plugin, else the Ghidra extension),
               bindiff matches them; matches are read from the .BinDiff SQLite and
               the function lists from the .BinExport protobufs, because BinDiff
               records matched functions only and added/removed are the rest.

`status` is "modified", "added" or "removed". `views` is {engine: status} for every
engine that ran, with "unchanged" for an engine that matched the function and found
no difference — so two engines that disagree are visible rather than averaged away.

The seam is `run(argv, cwd, env, timeout) -> (exit code or None, output text)`.
`diff_functions` never launches a process itself except through it, so the whole
layer is testable with a fake that writes the files a real engine would. This is
the same pattern as the chat seam in llm.py.

Failure policy, as everywhere in this package: a missing tool or a failing engine
becomes a note and the run continues with whatever the other engines found.
Binaries are always copied into a private temporary directory first, because IDA
writes its database next to the input and the input is someone else's tree.
"""

import collections
import difflib
import glob
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile

ENGINES = ("diaphora", "bindiff")

FunctionChange = collections.namedtuple(
    "FunctionChange", "name_a name_b status similarity views pseudo_diff"
)

# `engines` lists only the engines that succeeded, so an empty `changes` with a
# non-empty `engines` means "no function changed", never "nothing could run".
FunctionDiff = collections.namedtuple("FunctionDiff", "engines changes notes")

# Tools each engine needs, as alternatives: the engine can run when every tool of
# any one alternative was found. Hex-Rays is optional for Diaphora: without it the
# matching still works, there is just no pseudo-code to diff.
_NEEDS = {
    "diaphora": [("idat", "diaphora")],
    "bindiff": [("bindiff", "idat", "binexport_ida"), ("bindiff", "ghidra", "binexport_ghidra")],
}

_TOOL_LABELS = (
    ("idat", "IDA, text mode (idat64 / idat)"),
    ("hexrays", "Hex-Rays decompiler plugin"),
    ("diaphora", "Diaphora (diaphora.py)"),
    ("python", "Python for Diaphora's diff step"),
    ("bindiff", "BinDiff (bindiff)"),
    ("binexport_ida", "BinExport plugin for IDA"),
    ("ghidra", "Ghidra (analyzeHeadless)"),
    ("binexport_ghidra", "BinExport extension for Ghidra"),
    ("java", "JDK for Ghidra (binary.java)"),
)

# BinExport2 protobuf field numbers (binexport2.proto): BinExport2.call_graph = 8,
# CallGraph.vertex = 1, Vertex.address = 1, .type = 2, .mangled_name = 3,
# .demangled_name = 4. Type NORMAL = 0; library, imported and thunk functions are
# not the binary's own code and are left out of added/removed.
_BX_CALL_GRAPH, _BX_VERTEX = 8, 1
_BX_NORMAL = 0

# Headless Ghidra cannot call an exporter on its own; this post-script does.
_GHIDRA_BINEXPORT_SCRIPT = """\
// Written by MaNishtana: export currentProgram with BinExport to the first argument.
import java.io.File;
import com.google.security.binexport.BinExportExporter;
import ghidra.app.script.GhidraScript;

public class ManishtanaBinExport extends GhidraScript {
  @Override
  public void run() throws Exception {
    File out = new File(getScriptArgs()[0]);
    BinExportExporter exporter = new BinExportExporter();
    if (!exporter.export(out, currentProgram, currentProgram.getMemory(), monitor)) {
      throw new RuntimeException("BinExport failed for " + currentProgram.getName());
    }
  }
}
"""

_STATUS_ORDER = {"removed": 0, "added": 1, "modified": 2}


def discover(binary):
    """Find every tool: config path first, then environment, then PATH."""
    tools = {}
    tools["idat"] = _find_ida(binary.get("ida"), os.environ.get("IDADIR"))
    tools["hexrays"] = _find_hexrays(tools["idat"])
    tools["diaphora"] = _find_file(binary.get("diaphora"), os.environ.get("DIAPHORA_DIR"),
                                   "diaphora.py")
    tools["python"] = binary.get("python") or sys.executable
    tools["bindiff"] = _find_bindiff(binary.get("bindiff"), os.environ.get("BINDIFF_DIR"))
    tools["binexport_ida"] = _find_binexport_ida(tools["idat"])
    tools["ghidra"] = _find_ghidra(binary.get("ghidra"), os.environ.get("GHIDRA_INSTALL_DIR"))
    tools["binexport_ghidra"] = _find_binexport_ghidra(tools["ghidra"])
    # A JDK for Ghidra only. Ghidra 12 needs Java 25+, often newer than the box's
    # default; this goes to JAVA_HOME for Ghidra runs and changes nothing else.
    tools["java"] = binary.get("java") or None
    return tools


def runnable(tools):
    return [name for name in ENGINES if not _missing(name, tools)]


def describe_tools(tools):
    lines = ["Binary comparison tools", ""]
    width = max(len(label) for _key, label in _TOOL_LABELS)
    for key, label in _TOOL_LABELS:
        if key not in tools:
            continue
        found = tools[key]
        lines.append("  %-*s  %s" % (width, label, found if found else "not found"))
    lines.append("")
    engines = runnable(tools)
    for name in ENGINES:
        missing = _missing(name, tools)
        state = "ready" if not missing else "missing: %s" % ", ".join(missing)
        lines.append("  engine %-10s %s" % (name, state))
    lines.append("")
    lines.append("Runnable: %s" % (", ".join(engines) if engines else "none — layer 1 only"))
    if tools.get("diaphora") and not tools.get("hexrays"):
        lines.append("Note: no Hex-Rays plugin found — Diaphora will match functions but "
                     "produce no decompiled diff.")
    return "\n".join(lines) + "\n"


def diff_functions(old, new, engines, tools, binary, run=None):
    """Run each engine on one binary pair and merge what they found."""
    run = run or _run
    timeout = binary.get("timeout_sec", 1800)
    found, notes = {}, []
    work = tempfile.mkdtemp(prefix="manishtana-bin-")
    try:
        for name in engines:
            adapter = _ADAPTERS[name]
            engine_dir = os.path.join(work, name)
            os.makedirs(engine_dir)
            try:
                changes, problem = adapter(old, new, tools, run, engine_dir, timeout)
            except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
                changes, problem = None, "%s failed: %s" % (name, exc)
            if problem:
                notes.append(problem)
            if changes is not None:
                found[name] = changes
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ran = [name for name in ENGINES if name in found]
    return FunctionDiff(engines=ran, changes=_merge(found), notes=notes)


def decompiled_diff(changes, limit):
    """The pseudo-code diffs of the most changed functions, as one unified diff.

    Lowest similarity first, capped at `limit` functions, so a large binary sends
    the model its biggest changes rather than its first ones alphabetically.
    """
    with_code = [change for change in changes if change.pseudo_diff]
    if not with_code:
        return None
    with_code.sort(key=lambda c: (c.similarity if c.similarity is not None else 0.0,
                                  c.name_a or c.name_b or ""))
    parts = [change.pseudo_diff.rstrip("\n") + "\n" for change in with_code[:limit]]
    if len(with_code) > limit:
        parts.append("# %d more changed functions not shown\n" % (len(with_code) - limit))
    return "".join(parts)


# --- internals: discovery ----------------------------------------------------


def _missing(engine, tools):
    """The tools still missing for the nearest alternative; empty means ready."""
    best = None
    for alternative in _NEEDS[engine]:
        missing = [tool for tool in alternative if not tools.get(tool)]
        if best is None or len(missing) < len(best):
            best = missing
    return best


def _find_ida(given, env_dir):
    """idat64 before idat: before IDA 9, plain idat is the 32-bit build."""
    names = ["idat64", "idat"]
    if given:
        if os.path.isfile(given):
            return os.path.abspath(given)
        found = _in_dir(given, names)
        if found:
            return found
    if env_dir:
        found = _in_dir(env_dir, names)
        if found:
            return found
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        roots = glob.glob(r"C:\Program Files\IDA*") + glob.glob(r"C:\Program Files (x86)\IDA*")
        for root in sorted(roots, reverse=True):
            found = _in_dir(root, names)
            if found:
                return found
    return None


def _in_dir(directory, names):
    for name in names:
        for candidate in (name, name + ".exe"):
            path = os.path.join(directory, candidate)
            if os.path.isfile(path):
                return os.path.abspath(path)
    return None


def _find_hexrays(idat):
    """The x86/x64 decompiler plugin next to IDA (hexx64 in IDA 7+, hexrays before)."""
    if not idat:
        return None
    plugins = os.path.join(os.path.dirname(idat), "plugins")
    try:
        names = sorted(os.listdir(plugins))
    except OSError:
        return None
    for name in names:
        if name.lower().startswith(("hexx64", "hexrays")):
            return os.path.join(plugins, name)
    return None


def _find_bindiff(given, env_dir):
    places = [given, env_dir]
    if os.name == "nt":
        places.append(r"C:\Program Files\BinDiff")
    else:
        places.append("/opt/bindiff")
    for place in places:
        if not place:
            continue
        if os.path.isfile(place):
            return os.path.abspath(place)
        for sub in ("", "bin"):
            found = _in_dir(os.path.join(place, sub), ["bindiff"])
            if found:
                return found
    return shutil.which("bindiff")


def _find_binexport_ida(idat):
    """binexport12_ida64.dll / .so in IDA's plugins, or the user plugin directory."""
    places = []
    if idat:
        places.append(os.path.join(os.path.dirname(idat), "plugins"))
    if os.environ.get("IDAUSR"):
        places.extend(os.path.join(p, "plugins") for p in os.environ["IDAUSR"].split(os.pathsep))
    if os.name == "nt" and os.environ.get("APPDATA"):
        places.append(os.path.join(os.environ["APPDATA"], "Hex-Rays", "IDA Pro", "plugins"))
    places.append(os.path.join(os.path.expanduser("~"), ".idapro", "plugins"))
    for place in places:
        try:
            names = sorted(os.listdir(place))
        except OSError:
            continue
        for name in names:
            low = name.lower()
            if low.startswith("binexport") and "_ida" in low:
                return os.path.join(place, name)
    return None


def _find_ghidra(given, env_dir):
    """A Ghidra install directory, recognised by support/analyzeHeadless."""
    for place in (given, env_dir):
        if place and _headless(place):
            return os.path.abspath(place)
    return None


def _headless(ghidra):
    script = "analyzeHeadless.bat" if os.name == "nt" else "analyzeHeadless"
    path = os.path.join(ghidra, "support", script)
    return path if os.path.isfile(path) else None


def _find_binexport_ghidra(ghidra):
    """The BinExport extension, in Ghidra's own Extensions or the user's."""
    if not ghidra:
        return None
    places = [os.path.join(ghidra, "Ghidra", "Extensions", "BinExport")]
    home = os.path.expanduser("~")
    for pattern in (os.path.join(os.environ.get("APPDATA", home), "ghidra", "*", "Extensions"),
                    os.path.join(home, ".config", "ghidra", "*", "Extensions"),
                    os.path.join(home, ".ghidra", ".ghidra_*", "Extensions")):
        places.extend(os.path.join(found, "BinExport") for found in glob.glob(pattern))
    for place in places:
        if glob.glob(os.path.join(place, "lib", "BinExport*.jar")):
            return place
    return None


def _find_file(given, env_dir, filename):
    for place in (given, env_dir):
        if not place:
            continue
        if os.path.isfile(place):
            return os.path.abspath(place)
        candidate = os.path.join(place, filename)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


# --- internals: running ------------------------------------------------------


def _run(argv, cwd, env, timeout):
    """The production seam: run a process, never raise, capture everything.

    stdin is empty on purpose. Ghidra's launcher asks for a JDK path when it cannot
    find one, then waits on "Press any key"; with an inherited console that is a
    hang until the timeout, with an empty stdin it fails at once with its message.
    """
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timed out after %ds" % timeout
    except OSError as exc:
        return None, str(exc)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _env_for(tools, ghidra=False):
    env = dict(os.environ)
    if ghidra and tools.get("java"):
        env["JAVA_HOME"] = tools["java"]
    return env


def _stage(path, directory):
    """Copy one side into the engine's private directory and return the copy."""
    os.makedirs(directory, exist_ok=True)
    copy = os.path.join(directory, os.path.basename(path))
    shutil.copyfile(path, copy)
    return copy


def _tail(text, lines=3):
    return " | ".join((text or "").strip().splitlines()[-lines:])


# --- internals: Diaphora -----------------------------------------------------


def _diaphora(old, new, tools, run, work, timeout):
    """Export both sides with IDA + diaphora.py, diff the exports, read the result.

    Command lines are Diaphora's own, from its test harness (tester/tester.py):
    export is `idat -A -B -S<diaphora.py> -L<log> <binary>` driven by environment
    variables; the diff runs diaphora.py outside IDA on the two SQLite exports.
    """
    script = tools["diaphora"]
    # IDA splits -S on spaces to pass script arguments; a quoted path survives.
    script_arg = '-S"%s"' % script if " " in script else "-S" + script
    exports = {}
    for side, path in (("old", old), ("new", new)):
        copy = _stage(path, os.path.join(work, side))
        export = os.path.join(work, side + ".sqlite")
        log = os.path.join(work, side + ".log")
        env = dict(os.environ)
        env.update({"DIAPHORA_AUTO": "1", "DIAPHORA_EXPORT_FILE": export,
                    "DIAPHORA_LOG_PRINT": "1", "PYTHONWARNINGS": "ignore"})
        if tools.get("hexrays"):
            env["DIAPHORA_USE_DECOMPILER"] = "1"
        code, output = run([tools["idat"], "-A", "-B", script_arg, "-L" + log, copy],
                           os.path.dirname(copy), env, timeout)
        if code != 0 or not os.path.isfile(export):
            return None, "diaphora: IDA export of the %s side failed (%s) %s" % (
                side, "exit %s" % code if code is not None else output, _tail(output))
        exports[side] = export

    result = os.path.join(work, "diff.sqlite")
    env = dict(os.environ)
    env.update({"DIAPHORA_LOG_PRINT": "1", "PYTHONWARNINGS": "ignore"})
    code, output = run([tools["python"], script, exports["old"], exports["new"], "-o", result],
                       work, env, timeout)
    if code != 0 or not os.path.isfile(result):
        return None, "diaphora: diff step failed (%s) %s" % (
            "exit %s" % code if code is not None else output, _tail(output))
    return _read_diaphora(result, exports["old"], exports["new"]), None


def _read_diaphora(result, export_old, export_new):
    """Diaphora's result database to FunctionChange, pseudo-code from the exports.

    `results` holds matched pairs with a ratio, 1.0 meaning identical; `unmatched`
    holds functions found on one side only ("primary" is the old binary,
    "secondary" the new one).
    """
    changes = []
    con = sqlite3.connect(result)
    try:
        matched = con.execute("select name, name2, ratio from results").fetchall()
        unmatched = con.execute("select type, name from unmatched").fetchall()
    finally:
        con.close()
    code_old = _pseudocode(export_old)
    code_new = _pseudocode(export_new)
    for name, name2, ratio in matched:
        similarity = float(ratio)
        if similarity >= 1.0:
            continue
        changes.append(FunctionChange(name, name2, "modified", similarity,
                                      {"diaphora": "modified"},
                                      _unified(name, name2, code_old.get(name),
                                               code_new.get(name2))))
    for kind, name in unmatched:
        if kind == "primary":
            changes.append(FunctionChange(name, None, "removed", None,
                                          {"diaphora": "removed"},
                                          _unified(name, None, code_old.get(name), None)))
        elif kind == "secondary":
            changes.append(FunctionChange(None, name, "added", None, {"diaphora": "added"},
                                          _unified(None, name, None, code_new.get(name))))
    return changes


def _pseudocode(export):
    """{function name: pseudo-code} from a Diaphora export; empty without Hex-Rays."""
    con = sqlite3.connect(export)
    try:
        rows = con.execute(
            "select name, pseudocode from functions where pseudocode is not null "
            "and pseudocode != ''").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    code = {}
    for name, text in rows:
        code.setdefault(name, text)
    return code


def _unified(name_a, name_b, text_a, text_b):
    if text_a is None and text_b is None:
        return None
    lines_a = (text_a or "").splitlines(True)
    lines_b = (text_b or "").splitlines(True)
    diff = "".join(difflib.unified_diff(
        lines_a, lines_b,
        "a/" + name_a if name_a else "/dev/null",
        "b/" + name_b if name_b else "/dev/null",
        n=3))
    return diff or None


# --- internals: BinDiff ------------------------------------------------------


def _bindiff(old, new, tools, run, work, timeout):
    """Export both sides with BinExport, match them with bindiff, read the result.

    IDA's plugin is preferred when both are present: BinExport for Ghidra is
    documented as experimental. IDA command line from BinExport's README:
    `idat -A -OBinExportModule:<out> -OBinExportAutoAction:BinExportBinary <file>`.
    """
    use_ida = bool(tools.get("idat") and tools.get("binexport_ida"))
    exports = {}
    for side, path in (("old", old), ("new", new)):
        copy = _stage(path, os.path.join(work, side))
        export = os.path.join(work, side + ".BinExport")
        if use_ida:
            argv = [tools["idat"], "-A", "-OBinExportModule:" + export,
                    "-OBinExportAutoAction:BinExportBinary", copy]
            via = "IDA"
        else:
            script_dir = os.path.join(work, "ghidra_scripts")
            if not os.path.isdir(script_dir):
                os.makedirs(script_dir)
                with open(os.path.join(script_dir, "ManishtanaBinExport.java"), "w") as handle:
                    handle.write(_GHIDRA_BINEXPORT_SCRIPT)
            project = os.path.join(work, side + "_project")
            os.makedirs(project)
            argv = [_headless(tools["ghidra"]), project, "p", "-import", copy,
                    "-scriptPath", script_dir, "-postScript", "ManishtanaBinExport.java",
                    export, "-deleteProject"]
            via = "Ghidra"
        code, output = run(argv, os.path.dirname(copy), _env_for(tools, ghidra=not use_ida),
                           timeout)
        if code != 0 or not os.path.isfile(export):
            return None, "bindiff: BinExport via %s failed for the %s side (%s) %s" % (
                via, side, "exit %s" % code if code is not None else output, _tail(output))
        exports[side] = export

    out_dir = os.path.join(work, "out")
    os.makedirs(out_dir)
    code, output = run([tools["bindiff"], "--primary=" + exports["old"],
                        "--secondary=" + exports["new"], "--output_dir=" + out_dir,
                        "--output_format=bin"], work, dict(os.environ), timeout)
    results = glob.glob(os.path.join(out_dir, "*.BinDiff"))
    if code != 0 or not results:
        return None, "bindiff: matching failed (%s) %s" % (
            "exit %s" % code if code is not None else output, _tail(output))
    return _read_bindiff(results[0], exports["old"], exports["new"]), None


def _read_bindiff(result, export_old, export_new):
    """Matched pairs from the .BinDiff database; the unmatched rest from the exports."""
    con = sqlite3.connect(result)
    try:
        rows = con.execute(
            "select address1, name1, address2, name2, similarity from function").fetchall()
    finally:
        con.close()
    funcs_old = _binexport_functions(export_old)
    funcs_new = _binexport_functions(export_new)
    changes = []
    for address1, name1, address2, name2, similarity in rows:
        similarity = float(similarity)
        if similarity >= 1.0:
            continue
        changes.append(FunctionChange(funcs_old.get(address1, name1), funcs_new.get(address2, name2),
                                      "modified", similarity, {"bindiff": "modified"}, None))
    matched_old = set(row[0] for row in rows)
    matched_new = set(row[2] for row in rows)
    for address, name in sorted(funcs_old.items()):
        if address not in matched_old:
            changes.append(FunctionChange(name, None, "removed", None, {"bindiff": "removed"}, None))
    for address, name in sorted(funcs_new.items()):
        if address not in matched_new:
            changes.append(FunctionChange(None, name, "added", None, {"bindiff": "added"}, None))
    return changes


def _binexport_functions(path):
    """{address: name} for the binary's own functions in a .BinExport protobuf.

    A minimal protobuf reader is enough: only the call graph's vertices are needed,
    and the standard library has no protobuf module. Unnamed functions get IDA's
    sub_ADDRESS convention so both engines name them the same way.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    functions = {}
    for field, value in _proto_fields(data):
        if field != _BX_CALL_GRAPH:
            continue
        for sub, vertex in _proto_fields(value):
            if sub != _BX_VERTEX:
                continue
            fields = dict(_proto_fields(vertex))
            if fields.get(2, _BX_NORMAL) != _BX_NORMAL or 1 not in fields:
                continue
            address = fields[1]
            name = fields.get(4) or fields.get(3)
            name = name.decode("utf-8", "replace") if name else "sub_%X" % address
            functions[address] = name
    return functions


def _proto_fields(data):
    """Yield (field number, value) from one protobuf message, top level only.

    Varints come back as int, length-delimited fields as bytes; fixed 32/64-bit
    fields as int. Groups (wire types 3 and 4) are obsolete and not expected.
    """
    at, end = 0, len(data)
    while at < end:
        key, at = _varint(data, at)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, at = _varint(data, at)
        elif wire == 2:
            length, at = _varint(data, at)
            value = data[at:at + length]
            at += length
        elif wire == 1:
            value = struct.unpack_from("<Q", data, at)[0]
            at += 8
        elif wire == 5:
            value = struct.unpack_from("<I", data, at)[0]
            at += 4
        else:
            raise ValueError("unsupported protobuf wire type %d" % wire)
        yield field, value


def _varint(data, at):
    result, shift = 0, 0
    while True:
        if at >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[at]
        at += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, at
        shift += 7


# --- internals: merging ------------------------------------------------------


def _merge(found):
    """One entry per function across engines, with each engine's view kept.

    Keyed by the old name, else the new one. An engine that ran and does not list a
    function matched it as identical, so its view is "unchanged" — that is what
    makes a disagreement visible. The merged status is the first engine's (in
    ENGINES order) that saw a change; similarity is the lowest any engine gave,
    the conservative reading.
    """
    merged = collections.OrderedDict()
    ran = [name for name in ENGINES if name in found]
    for engine in ran:
        for change in found[engine]:
            key = change.name_a or change.name_b
            current = merged.get(key)
            if current is None:
                merged[key] = change._replace(views=dict(change.views))
                continue
            views = dict(current.views)
            views.update(change.views)
            similarity = current.similarity
            if change.similarity is not None:
                similarity = (change.similarity if similarity is None
                              else min(similarity, change.similarity))
            merged[key] = current._replace(
                similarity=similarity, views=views,
                pseudo_diff=current.pseudo_diff or change.pseudo_diff,
                name_b=current.name_b or change.name_b)
    result = []
    for change in merged.values():
        views = dict((engine, change.views.get(engine, "unchanged")) for engine in ran)
        result.append(change._replace(views=views))
    result.sort(key=lambda c: (_STATUS_ORDER[c.status],
                               c.similarity if c.similarity is not None else 0.0,
                               c.name_a or c.name_b or ""))
    return result


_ADAPTERS = {
    "diaphora": _diaphora,
    "bindiff": _bindiff,
}
