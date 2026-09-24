// Parsing XML minimal : l'enceinte renvoie de petits documents au format stable,
// quelques recherches de sous-chaînes suffisent (pas de bibliothèque XML).
#pragma once
#include <Arduino.h>

namespace xml {

// Décode les entités XML usuelles (&amp; &lt; &gt; &quot; &apos; &#NN; &#xNN;).
inline String decode(const String &s) {
  if (s.indexOf('&') < 0) return s;
  String out;
  out.reserve(s.length());
  for (unsigned i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c != '&') { out += c; continue; }
    int end = s.indexOf(';', i);
    if (end < 0 || end - (int)i > 8) { out += c; continue; }
    String ent = s.substring(i + 1, end);
    if (ent == "amp") out += '&';
    else if (ent == "lt") out += '<';
    else if (ent == "gt") out += '>';
    else if (ent == "quot") out += '"';
    else if (ent == "apos") out += '\'';
    else if (ent.startsWith("#x")) out += (char)strtol(ent.c_str() + 2, nullptr, 16);
    else if (ent.startsWith("#")) out += (char)atoi(ent.c_str() + 1);
    else { out += c; continue; }
    i = end;
  }
  return out;
}

// Échappe une valeur pour l'insérer dans du XML (texte ou attribut).
inline String escape(const String &s) {
  String out;
  out.reserve(s.length() + 16);
  for (unsigned i = 0; i < s.length(); i++) {
    char c = s[i];
    switch (c) {
      case '&': out += F("&amp;"); break;
      case '<': out += F("&lt;"); break;
      case '>': out += F("&gt;"); break;
      case '"': out += F("&quot;"); break;
      case '\'': out += F("&apos;"); break;
      default: out += c;
    }
  }
  return out;
}

// Position du début de la balise ouvrante <tag (suivie d'un espace, '>' ou '/'), à partir de from.
inline int findTag(const String &doc, const char *tag, int from = 0) {
  String open = String('<') + tag;
  int i = from;
  while ((i = doc.indexOf(open, i)) >= 0) {
    char next = doc.length() > i + open.length() ? doc[i + open.length()] : 0;
    if (next == ' ' || next == '>' || next == '/') return i;
    i += open.length();
  }
  return -1;
}

// Valeur de l'attribut attr dans la balise qui commence à tagPos (décodée) ; "" si absent.
inline String attr(const String &doc, int tagPos, const char *name) {
  if (tagPos < 0) return "";
  int end = doc.indexOf('>', tagPos);
  if (end < 0) return "";
  String needle = String(' ') + name + "=\"";   // l'espace évite de confondre source et sourceAccount
  int i = doc.indexOf(needle, tagPos);
  if (i < 0 || i > end) return "";
  i += needle.length();
  int j = doc.indexOf('"', i);
  return j < 0 ? "" : decode(doc.substring(i, j));
}

// Texte du premier élément <tag>...</tag> à partir de from (décodé) ; "" si absent.
inline String text(const String &doc, const char *tag, int from = 0) {
  int i = findTag(doc, tag, from);
  if (i < 0) return "";
  int gt = doc.indexOf('>', i);
  if (gt < 0 || doc[gt - 1] == '/') return "";
  int end = doc.indexOf(String("</") + tag + ">", gt);
  return end < 0 ? "" : decode(doc.substring(gt + 1, end));
}

}  // namespace xml
