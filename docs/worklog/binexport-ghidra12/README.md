# BinExport for Ghidra 12 — the one-line fix

The `bindiff` engine can export through Ghidra instead of IDA. BinDiff 8's installer
ships a BinExport extension for Ghidra, but it was built for Ghidra 10.3, and on
Ghidra 12.2 it has two problems:

1. Ghidra refuses to load it because of the version number. **Fix:** set
   `version=12.2` (your Ghidra's version) in `extension.properties`.
2. It throws a `NullPointerException` in `BinExport2Builder.buildFlowGraphs` on the
   first basic block that is a single instruction with nothing before it. That
   happens in practically every real binary; it did on both jq builds.
   **Fix:** the patch in this directory, which uses the instruction *containing*
   the block's last address instead of the one *before* it.

The newer BinExport release (built for Ghidra 11.0.3) does not help: it calls
`DefinedDataIterator.definedStrings(Program)`, which Ghidra 12 no longer has
(`NoSuchMethodError`).

## Rebuilding the jar (needs only the JDK that Ghidra 12 already needs)

```bash
G=/path/to/ghidra_12.2_PUBLIC
JDK=/path/to/jdk-25
EXT=/path/to/BinDiff/Extra/Ghidra/BinExport      # from the BinDiff 8 installer

# 1. Install the shipped extension and fix its version.
cp -r "$EXT" "$G/Ghidra/Extensions/"
sed -i 's/^version=.*/version=12.2/' "$G/Ghidra/Extensions/BinExport/extension.properties"

# 2. Patch and recompile the one class against Ghidra's own jars.
mkdir bx && cd bx
unzip -q "$EXT/lib/BinExport-src.zip"
patch -p1 < /path/to/MaNishtana/docs/worklog/binexport-ghidra12/BinExport2Builder.patch
CP=$(find "$G/Ghidra" -name '*.jar' | tr '\n' ':')"$EXT/lib/BinExport.jar:$(ls "$EXT"/lib/protobuf-java-*.jar)"
mkdir classes
"$JDK/bin/javac" -nowarn -d classes -cp "$CP" \
    src/main/java/com/google/security/binexport/BinExport2Builder.java

# 3. Replace the class inside the installed jar.
(cd classes && "$JDK/bin/jar" uf "$G/Ghidra/Extensions/BinExport/lib/BinExport.jar" \
    com/google/security/binexport/BinExport2Builder.class)
```

On Windows, the classpath separator is `;` rather than `:`, and the paths go to
`javac` in Windows form (`cygpath -w` in Git Bash).

Check: `python -m manishtana --check-binary-tools -c config.json` should report
`BinExport extension for Ghidra` found and `engine bindiff ready`.

Verified on 2026-10-31 with Ghidra 12.2, Temurin JDK 25 and BinDiff 8. Both sides of
jq 1.7 exported in about 65 s each, and the one-byte patch was reported as exactly
one modified function.
