"""Adapters: one file per external system; its sole importer.

``files`` is the base adapter (filesystem, naming, quota); other
adapters may import it. No file outside this package imports a vendor
SDK (REQ-ARC-002).
"""
