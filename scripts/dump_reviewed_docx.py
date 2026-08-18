#!/usr/bin/env python3
"""Dump paragraphs + comments + highlights from reviewed .docx files in a dir.

Pure stdlib: zipfile + xml.etree.ElementTree. Output written as UTF-8 text.
"""
import sys
import os
import zipfile
import xml.etree.ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def q(tag):
    return f"{{{W}}}{tag}"


def para_text(p):
    parts = []
    for t in p.iter(q("t")):
        parts.append(t.text or "")
    return "".join(parts)


def para_comment_ids(p):
    return [r.get(q("id")) for r in p.iter(q("commentReference"))]


def para_highlight(p):
    colors = []
    for h in p.iter(q("highlight")):
        v = h.get(q("val"))
        if v and v != "none":
            colors.append(v)
    return sorted(set(colors))


def dump_one(path, out):
    z = zipfile.ZipFile(path)
    names = z.namelist()

    doc_xml = z.read("word/document.xml").decode("utf-8")
    root = ET.fromstring(doc_xml)

    comments = {}
    if "word/comments.xml" in names:
        croot = ET.fromstring(z.read("word/comments.xml").decode("utf-8"))
        for c in croot.iter(q("comment")):
            cid = c.get(q("id"))
            author = c.get(q("author"), "")
            date = c.get(q("date"), "")
            text = para_text(c)
            comments[cid] = {"author": author, "date": date, "text": text}

    out.write(f"FILE: {path}\n")
    out.write(f"comments.xml present: {'word/comments.xml' in names}\n")
    out.write(f"total comments in comments.xml: {len(comments)}\n")
    out.write("=" * 100 + "\n")

    body = root.find(q("body"))
    out.write("--- PARAGRAPHS (with comments & highlights) ---\n")
    pi = 0
    for p in body.iter(q("p")):
        text = para_text(p)
        cids = para_comment_ids(p)
        hl = para_highlight(p)
        pi += 1
        if not text and not cids and not hl:
            continue
        tag = []
        if cids:
            tag.append(f"COMMENT_IDS={cids}")
        if hl:
            tag.append(f"HIGHLIGHT={hl}")
        tstr = text.replace("\n", "\\n")
        prefix = f"[P{pi}] " + (" ".join(tag) + " " if tag else "")
        out.write(f"{prefix}{tstr}\n")

    out.write("\n" + "=" * 100 + "\n")
    out.write("--- COMMENTS CONTENT ---\n")
    for cid in sorted(comments, key=lambda x: int(x)):
        c = comments[cid]
        out.write(f"[comment {cid}] author={c['author']} date={c['date']}\n")
        out.write(c["text"] + "\n")
        out.write("-" * 80 + "\n")
    out.write("\n\n")


def main(directory, out_path):
    with open(out_path, "w", encoding="utf-8") as out:
        for name in sorted(os.listdir(directory)):
            if name.endswith("_reviewed.docx"):
                dump_one(os.path.join(directory, name), out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
