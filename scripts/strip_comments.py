#!/usr/bin/env python3
"""Strip Word comments from a reviewed docx → original document.

Removes commentRangeStart/commentRangeEnd/commentReference from document.xml,
drops comments.xml + its relationship + content-type override.
"""
import sys
import zipfile
import re
import xml.etree.ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"


def q(tag, ns=W):
    return f"{{{ns}}}{tag}"


def main(src, dst):
    zin = zipfile.ZipFile(src)
    doc = zin.read("word/document.xml")
    root = ET.fromstring(doc)

    # scrub comment markers from document.xml
    removed = 0
    for el in list(root.iter()):
        if el.tag in (q("commentRangeStart"), q("commentRangeEnd"), q("commentReference")):
            parent = None
            for p in root.iter():
                if el in list(p):
                    parent = p
                    break
            if parent is not None:
                parent.remove(el)
                removed += 1

    rels = {}
    if "word/_rels/document.xml.rels" in zin.namelist():
        rels_root = ET.fromstring(zin.read("word/_rels/document.xml.rels"))
        for rel in rels_root.findall(q("Relationship", PR)):
            if rel.get("Type", "").endswith("/comments"):
                rels_root.remove(rel)
        rels["word/_rels/document.xml.rels"] = ET.tostring(rels_root)

    ct = {}
    if "[Content_Types].xml" in zin.namelist():
        ct_root = ET.fromstring(zin.read("[Content_Types].xml"))
        for ov in ct_root.findall(q("Override", CT)):
            if ov.get("PartName") == "/word/comments.xml":
                ct_root.remove(ov)
        ct["[Content_Types].xml"] = ET.tostring(ct_root)

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "word/comments.xml":
                continue  # drop comments part
            if item.filename == "word/document.xml":
                data = ET.tostring(root, xml_declaration=True, encoding="UTF-8")
            elif item.filename in rels:
                data = rels[item.filename]
            elif item.filename in ct:
                data = ct[item.filename]
            else:
                data = zin.read(item.filename)
            zout.writestr(item, data)
    zin.close()
    print(f"[strip-comments] {src} -> {dst} (removed {removed} comment markers)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
