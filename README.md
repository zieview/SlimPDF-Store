# SlimPDF Store source

This Store source is provided under GNU AGPL v3.
Company NEWS modules are excluded. Third-party components retain their licenses.

Windows / Python 3.12: install requirements-store-runtime.txt and requirements-build.txt.
Set SLIMPDF_BUILD_EDITION=store, then run python main.py.
Prepare the exact licensed engine builds listed in ENGINE_BUNDLE_NOTICES.md.
Run python scripts/prepare_store_engines.py --ghostscript <GS-root> --tesseract <Tesseract-root>.
Build with python -m PyInstaller SlimPDFStore.spec.
This is a source candidate: dependency corresponding-source review is not complete.
