// Imitation minimale de la classe String d'Arduino, pour compiler parse.h / xmlmini.h sur
// ordinateur (test/host/run.sh). Seules les méthodes utilisées par ces en-têtes existent.
#pragma once
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <string>

#define F(x) (x)

class String {
 public:
  std::string s;
  String() {}
  String(const char *c) : s(c ? c : "") {}
  String(const std::string &x) : s(x) {}
  explicit String(char c) : s(1, c) {}
  explicit String(int n) : s(std::to_string(n)) {}
  unsigned length() const { return s.size(); }
  const char *c_str() const { return s.c_str(); }
  char operator[](unsigned i) const { return i < s.size() ? s[i] : 0; }
  int indexOf(char c, unsigned from = 0) const { auto p = s.find(c, from); return p == std::string::npos ? -1 : (int)p; }
  int indexOf(const String &x, unsigned from = 0) const { auto p = s.find(x.s, from); return p == std::string::npos ? -1 : (int)p; }
  int indexOf(const char *x, unsigned from = 0) const { return indexOf(String(x), from); }
  String substring(unsigned a) const { return a >= s.size() ? String() : String(s.substr(a)); }
  String substring(unsigned a, unsigned b) const {
    if (b > s.size()) b = s.size();
    return a >= b ? String() : String(s.substr(a, b - a));
  }
  bool startsWith(const char *p) const { return s.rfind(p, 0) == 0; }
  bool startsWith(const String &p) const { return s.rfind(p.s, 0) == 0; }
  long toInt() const { return atol(s.c_str()); }
  void toUpperCase() { std::transform(s.begin(), s.end(), s.begin(), ::toupper); }
  bool equalsIgnoreCase(const String &o) const {
    String a = *this, b = o; a.toUpperCase(); b.toUpperCase(); return a.s == b.s;
  }
  void reserve(unsigned n) { s.reserve(n); }
  String &operator+=(const String &o) { s += o.s; return *this; }
  String &operator+=(const char *o) { s += o; return *this; }
  String &operator+=(char c) { s += c; return *this; }
  bool operator==(const String &o) const { return s == o.s; }
  bool operator==(const char *o) const { return s == o; }
  bool operator!=(const String &o) const { return s != o.s; }
  bool operator!=(const char *o) const { return s != o; }
};
inline String operator+(const String &a, const String &b) { return String(a.s + b.s); }
inline String operator+(const String &a, const char *b) { return String(a.s + b); }
inline String operator+(const char *a, const String &b) { return String(std::string(a) + b.s); }
inline String operator+(const String &a, char c) { return String(a.s + c); }
