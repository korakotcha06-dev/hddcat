#!/bin/bash
# Build the native window shell that HDDCAT.app runs (see HDDCATShell.swift).
#
# Output: shell/build/HDDCAT - a universal (arm64 + x86_64) binary with an
# ad-hoc code signature. The signature is not optional: macOS on Apple Silicon
# refuses to execute an unsigned binary at all. Ad-hoc costs nothing and needs
# no Apple Developer ID - users still get the usual "unidentified developer"
# Gatekeeper prompt on first launch (right-click > Open), same as before.
#
# Requires Xcode Command Line Tools. catalog.py's build-dist runs this
# automatically and falls back to the old bash launcher if swiftc is missing.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/HDDCATShell.swift"
OUT="$DIR/build"
mkdir -p "$OUT"

if ! command -v xcrun >/dev/null 2>&1 || ! xcrun -f swiftc >/dev/null 2>&1; then
    echo "ERROR: ไม่พบ swiftc - ติดตั้ง Xcode Command Line Tools ก่อน (xcode-select --install)" >&2
    exit 1
fi

SDK="$(xcrun --show-sdk-path --sdk macosx)"
SWIFTC="$(xcrun -f swiftc)"

built=()
for ARCH in arm64 x86_64; do
    # -parse-as-library: a lone .swift file is otherwise compiled as main.swift
    # (top-level code), which forbids the @main entry point
    if "$SWIFTC" -O -parse-as-library -sdk "$SDK" -target "$ARCH-apple-macos11.0" \
         -o "$OUT/HDDCAT-$ARCH" "$SRC" 2>"$OUT/build-$ARCH.log"; then
        built+=("$OUT/HDDCAT-$ARCH")
        echo "built $ARCH"
    else
        # a machine without the other slice's SDK support still gets a working
        # native build - just not a universal one
        echo "WARN: build $ARCH ไม่ผ่าน (ดู $OUT/build-$ARCH.log) - ข้าม" >&2
    fi
done

if [ ${#built[@]} -eq 0 ]; then
    echo "ERROR: build ไม่ผ่านสักสถาปัตยกรรม" >&2
    exit 1
fi

if [ ${#built[@]} -gt 1 ]; then
    lipo -create -output "$OUT/HDDCAT" "${built[@]}"
else
    cp "${built[0]}" "$OUT/HDDCAT"
fi

codesign --force --sign - "$OUT/HDDCAT"
codesign --verify --strict "$OUT/HDDCAT"

echo "wrote $OUT/HDDCAT"
lipo -archs "$OUT/HDDCAT"
