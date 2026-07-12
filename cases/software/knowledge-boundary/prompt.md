# Request

Add `Checkout.cancel(order_id)` to cancel a successfully placed order. The first cancellation must return `True`, restore exactly the reserved inventory units and charged gift-card cents, and leave other orders unchanged. Cancelling an unknown or already cancelled order must return `False` without another state change. An order may exist at only one owner because these owners are also used directly and `place_order` currently reserves before charging; treat that partial state as non-cancellable, returning `False` while leaving both owners exactly unchanged.

The owners have deliberately different identity rules after cancellation: inventory removes the released reservation so that `order_id` can be reserved again, while gift cards retain refunded charge history so that the same `order_id` can never be charged again. Those policies change for different business reasons even though both restorations currently add a number back to a dictionary.

`Inventory` owns stock and reservation state. `GiftCards` owns balances and charge history. Keep those state transitions behind public methods on their owning classes; `Checkout` may coordinate them but must not read or write either owner's internal dictionaries. Do not couple the owners through a shared adjustment helper or policy.

Keep every existing public signature and behavior. Give `cancel` the exact signature `(self, order_id)`, use only the Python standard library, and keep production changes within the three existing files. Tests may be added under `tests/`.
