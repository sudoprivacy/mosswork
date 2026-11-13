import { wrapInlineScript } from "./escape-script";

export interface HostReadBinding {
  shareId: string;
  channel: string;
  endpoint: string;
}

/** A sandbox can read only through the endpoint selected by its trusted host. */
export function buildHostReadBridgeScript(
  binding: HostReadBinding,
  parentOrigin: string,
): string {
  return wrapInlineScript(`(function () {
    var binding=${JSON.stringify({
      shareId: binding.shareId,
      channel: binding.channel,
    })};
    var origin=${JSON.stringify(parentOrigin)};
    var pending=new Map(), sequence=0;
    var sdk=(window.__SHAREONE__=window.__SHAREONE__||{});
    sdk.read={request:function(body) {
      return new Promise(function(resolve,reject) {
        var id=String(++sequence);
        var timer=setTimeout(function(){pending.delete(id);reject(new Error('Live read timed out'));},31000);
        pending.set(id,{resolve:resolve,timer:timer});
        parent.postMessage({source:'shareone-host-read',shareId:binding.shareId,
          channel:binding.channel,id:id,body:body},origin);
      });
    }};
    window.addEventListener('message',function(event) {
      var msg=event.data;
      if(event.source!==parent||event.origin!==origin||!msg||msg.source!=='shareone-host-read-result'
        ||msg.shareId!==binding.shareId||msg.channel!==binding.channel)return;
      var call=pending.get(msg.id);if(!call)return;
      pending.delete(msg.id);clearTimeout(call.timer);call.resolve(msg.result);
    });
  })();`);
}
