#!/usr/bin/env bash
set -euo pipefail
paper_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_dir=$(cd -- "$paper_dir/.." && pwd)
pdflatex_bin=${PDFLATEX:-}
if [[ -z "$pdflatex_bin" ]]; then
  pdflatex_bin=$(command -v pdflatex || true)
fi
if [[ -z "$pdflatex_bin" ]]; then
  pdflatex_bin="$source_dir/../.tools/TinyTeX/bin/universal-darwin/pdflatex"
fi
if [[ ! -x "$pdflatex_bin" ]]; then
  echo "pdflatex not found; set PDFLATEX to its executable path." >&2
  exit 1
fi
engine_dir=$(dirname -- "$pdflatex_bin")
bibtex_bin=${BIBTEX:-$engine_dir/bibtex}
cd -- "$source_dir"
mkdir -p build
"$pdflatex_bin" -no-shell-escape -interaction=nonstopmode -halt-on-error \
  -file-line-error -output-directory=build main.tex
"$bibtex_bin" build/main
for pass in 2 3; do
  "$pdflatex_bin" -no-shell-escape -interaction=nonstopmode -halt-on-error \
    -file-line-error -output-directory=build main.tex
done
cp -p build/main.pdf main.pdf
echo "PDF: $source_dir/main.pdf"
