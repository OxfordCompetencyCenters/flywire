/*! Flywire Payment Gateway — embed helper.
 *
 *   <div id="payment"></div>
 *   <script src="https://payments.example.ac.uk/static/embed.js"></script>
 *   <script>
 *     FlywireGateway.mount("#payment", paymentUrlFromYourServer, {
 *       onSubmitted: (e) => { window.location = "/orders/123/thanks"; },
 *       onError:     (e) => console.warn(e),
 *       onCancel:    ()  => { window.location = "/basket"; },
 *     });
 *   </script>
 *
 * "onSubmitted" means the payer submitted the form — NOT that money arrived. Fulfil the
 * order from the signed payment.status_changed webhook (or by polling the checkout).
 */
(function () {
  "use strict";

  function mount(target, paymentUrl, options) {
    var opts = options || {};
    var el = typeof target === "string" ? document.querySelector(target) : target;
    if (!el) throw new Error("FlywireGateway.mount: container not found");

    var url = new URL(paymentUrl, window.location.href);
    if (url.protocol !== "https:" && url.hostname !== "localhost" && url.hostname !== "127.0.0.1") {
      throw new Error("FlywireGateway.mount: payment URL must be https");
    }
    url.searchParams.set("embed_origin", window.location.origin);

    var iframe = document.createElement("iframe");
    iframe.src = url.href;
    iframe.title = opts.title || "Payment";
    iframe.setAttribute("allow", "payment");
    iframe.style.cssText = "border:0;width:100%;min-height:" + (opts.minHeight || 640) + "px;";
    el.replaceChildren(iframe);

    function onMessage(event) {
      if (event.origin !== url.origin || event.source !== iframe.contentWindow) return;
      var data = event.data || {};
      switch (data.type) {
        case "flywire-gateway:resize":
          if (typeof data.height === "number" && data.height > 0 && data.height < 10000) {
            iframe.style.height = data.height + "px";
          }
          break;
        case "flywire-gateway:submitted":
          if (opts.onSubmitted) opts.onSubmitted(data);
          break;
        case "flywire-gateway:error":
          if (opts.onError) opts.onError(data);
          break;
        case "flywire-gateway:cancel":
          if (opts.onCancel) opts.onCancel(data);
          break;
      }
    }
    window.addEventListener("message", onMessage);

    return {
      iframe: iframe,
      destroy: function () {
        window.removeEventListener("message", onMessage);
        iframe.remove();
      },
    };
  }

  window.FlywireGateway = { mount: mount };
})();
