"""The one Telegram container: transport only, zero processing IP.

``main`` supervises one ``relay`` belt per media bot; each belt receives
a file (or link) over Telegram and POSTs it to its service over HTTP,
then sends the bytes back. No model, no torch -- the IP lives in the
services (minions/svc/*).
"""
