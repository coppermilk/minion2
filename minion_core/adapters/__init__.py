# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Adapters: one file per external system; its sole importer.

``files`` is the base adapter (filesystem, naming, quota); other
adapters may import it. No file outside this package imports a vendor
SDK (REQ-ARC-002).
"""
