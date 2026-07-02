"""The bots: one directory per unit, no sibling imports.

Streaming bots are per-file belts drained forever; batch bots
orchestrate adapters directly and hold the lock of BLUEPRINT 8. No
bot imports a sibling bot (REQ-ARC-001).
"""
