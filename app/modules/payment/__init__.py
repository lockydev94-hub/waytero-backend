"""Customer online payments (Razorpay).

The module deliberately keeps the gateway behind a small service surface:

* ``GET /public/payments/config`` tells a client whether the platform can take
  money right now. It exposes only the public key id;
* ``POST /payments/orders`` prices the request from the master booking and
  creates a Razorpay order;
* ``POST /payments/verify`` verifies the checkout signature and records the
  money in the same receipt / advance tables used by admin and partner;
* ``POST /payments/webhook`` is the server-to-server safety net.

Doc Ref: Docs/04_API_Documentation/06_PAYMENT_API.md
Doc Ref: BRD Part 6 §155 (wallet, commission and settlement)
"""
