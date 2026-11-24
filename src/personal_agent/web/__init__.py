"""The web app (stage L1 "Leitstand"): its own listener, route table, later sign-in and API.

Built step by step after `_intern` design gates G1-G8. The core stays standard library only;
the web module may use `cryptography` (passkeys, web push) once those parts land.
"""
