#!/bin/sh
# Compile et lance le test du parsing sur ordinateur (macOS ou Linux, compilateur C++17).
set -e
cd "$(dirname "$0")"
${CXX:-c++} -std=c++17 -Wall -I. -DFIXTURES="\"$(pwd)/fixtures\"" parse_test.cpp -o /tmp/parse_test
/tmp/parse_test
