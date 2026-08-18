# ============================================================
# WAY TERO — LIVE CHAT MODULE
# Doc Ref: BRD Part 7 §155 (realtime channel), Website Chat
#
# Customer ↔ admin support chat with smart routing. Public endpoints
# (app/modules/chat/api.py) feed the customer-web widget; admin
# endpoints (admin_api.py) feed the admin portal chat board. Presence
# is tracked in Redis so a chat only routes to an online agent.
# ============================================================
