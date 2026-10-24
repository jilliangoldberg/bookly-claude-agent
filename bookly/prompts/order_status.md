# Procedure: Order status
Use when: the customer asks where an order is, when it arrives, or about tracking.

1. Verify the customer if they aren't verified yet.
2. Call list_orders.
3. Pick the order:
   - If the customer named an order id or a book title, use that order.
   - If exactly one order is not fully delivered, assume it's that one and say so.
   - If more than one order is not fully delivered, ask which one, naming each by
     placed date and book titles.
4. Call get_order for the chosen order.
5. Report each shipment separately: what's in it, carrier, tracking number, and the ETA or
   delivered date. For split orders, make clear which book is in which shipment.
6. Preorders: give the release date and say it ships then.
