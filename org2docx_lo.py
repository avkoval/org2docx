"""org2docx LibreOffice step: refresh indexes (TOC) and fields, save DOCX and optionally PDF.

Used two ways:
  - imported by org2docx.py when its Python has the UNO bridge (import uno), talking to soffice over a socket;
  - copied into a private LibreOffice profile and run inside soffice as a Python macro (main), for Pythons without
    the bridge.  On macOS that is the only way left: LibreOffice's own Python may only be launched by soffice.
    The macro reads its job from the JSON file named by $ORG2DOCX_LO_JOB and writes "ok" or the error to "status".
"""
import json
import os
import traceback

import uno
from com.sun.star.beans import PropertyValue


def prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def refresh_and_save(desktop, docx_in, docx_out, pdf_out):
    doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(str(docx_in)), "_blank", 0, (prop("Hidden", True),))
    if doc is None:
        raise RuntimeError("LibreOffice could not open the intermediate DOCX")
    try:
        for _ in range(2):  # the TOC itself changes pagination
            idx = doc.getDocumentIndexes()
            for i in range(idx.getCount()):
                idx.getByIndex(i).update()
            doc.getTextFields().refresh()
        doc.storeToURL(uno.systemPathToFileUrl(str(docx_out)), (prop("FilterName", "MS Word 2007 XML"),))
        if pdf_out:
            doc.storeToURL(uno.systemPathToFileUrl(str(pdf_out)),
                           (prop("FilterName", "writer_pdf_Export"),
                            prop("FilterData", uno.Any("[]com.sun.star.beans.PropertyValue", tuple([
                                prop("ExportBookmarks", True), prop("ExportLinksRelativeFsys", False),
                                prop("ConvertOOoTargetToPDFTarget", True),
                                prop("ExportBookmarksToPDFDestination", True),
                            ])))))
    finally:
        doc.close(True)


def main(*_args):
    """Macro entry point: vnd.sun.star.script:org2docx_lo.py$main?language=Python&location=user"""
    desktop = XSCRIPTCONTEXT.getDesktop()  # noqa: F821 - provided by the LibreOffice script provider
    job = json.loads(open(os.environ["ORG2DOCX_LO_JOB"], encoding="utf-8").read())
    try:
        refresh_and_save(desktop, job["docx_in"], job["docx_out"], job["pdf_out"])
        status = "ok"
    except Exception:  # noqa: BLE001 - reported back to org2docx.py
        status = traceback.format_exc()
    with open(job["status"], "w", encoding="utf-8") as f:
        f.write(status)
    desktop.terminate()


g_exportedScripts = (main,)
